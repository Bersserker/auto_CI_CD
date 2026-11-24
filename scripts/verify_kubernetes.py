"""Проверка Service/Ingress, UI и rolling update в явно выбранном Kubernetes context."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--request-file", required=True, type=Path)
    parser.add_argument("--host", default="credit.local")
    parser.add_argument("--check-rollout", action="store_true")
    parser.add_argument("--check-failed-rollout", action="store_true")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.request_file.read_text())
    base = ["kubectl", "--context", args.context]

    def kubectl(*parts, **kwargs):
        return subprocess.run(
            base + list(parts), check=True, capture_output=True, text=True, **kwargs
        ).stdout

    def pods():
        return json.loads(kubectl("-n", "mlops", "get", "pods", "-o", "json"))["items"]

    def identities():
        return {
            p["metadata"]["uid"]
            for p in pods()
            if not p["metadata"].get("deletionTimestamp")
            and p["metadata"]["labels"].get("app") in {"frontend", "backend"}
        }

    for app in ["backend", "frontend"]:
        kubectl(
            "-n", "mlops", "rollout", "status", f"deployment/{app}", "--timeout=15m"
        )
    # Форма в настоящем frontend pod вызывает API по Service/DNS без HTTP-mock.
    ui_probe = """from streamlit.testing.v1 import AppTest
app = AppTest.from_file('src/frontend/app.py').run(timeout=30)
assert not app.exception, app.exception
next(b for b in app.button if b.label == 'Рассчитать вероятность дефолта').click().run(timeout=30)
assert not app.exception and not app.error, app.error
assert app.metric, 'No prediction displayed'
print('Streamlit form -> backend Service: PASS')
"""
    print(
        kubectl(
            "-n", "mlops", "exec", "deployment/frontend", "--", "python", "-c", ui_probe
        ).strip()
    )
    # Нулевой локальный порт позволяет избежать конфликтов на хосте.
    forward = subprocess.Popen(
        base
        + [
            "-n",
            "ingress-system",
            "port-forward",
            "service/traefik",
            ":80",
            "--address=127.0.0.1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    report = {"context": args.context, "service_ui": "passed"}
    try:
        port = None
        # port-forward печатает порт только после успешного соединения с API.
        for line in forward.stdout:
            match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", line)
            if match:
                port = int(match[1])
                break
            if forward.poll() is not None:
                break
        if port is None:
            raise RuntimeError("Ingress port-forward did not start")

        # kubectl пишет строку на каждое соединение. Непрочитанный PIPE
        # заполняется под нагрузкой и блокирует сам port-forward.
        def drain_forward_logs():
            for _ in forward.stdout:
                pass

        drain = threading.Thread(target=drain_forward_logs, daemon=True)
        drain.start()
        url = f"http://127.0.0.1:{port}"

        def request(path, data=None):
            headers = {"Host": args.host}
            body = None
            if data is not None:
                body = json.dumps(data).encode()
                headers["Content-Type"] = "application/json"
            with urlopen(
                Request(url + path, data=body, headers=headers), timeout=5
            ) as response:
                assert response.status == 200
                return response.read()

        result = None
        for attempt in range(30):
            try:
                assert json.loads(request("/health"))["status"] == "ok"
                result = json.loads(request("/predict", payload))
                assert result["prediction"] in [0, 1]
                assert 0 <= result["default_probability"] <= 1
                assert b"<html" in request("/").lower()
                assert request("/_stcore/health").strip() == b"ok"
                break
            except Exception:
                if attempt == 29:
                    raise
                time.sleep(1)
        report["ingress"] = {
            "health": "passed",
            "predict": "passed",
            "frontend": "passed",
            "prediction": result,
        }
        print("Ingress /, /health, /predict and frontend health: PASS")
        errors = []
        sample_count = [0]
        stop = threading.Event()

        def traffic():
            while not stop.is_set():
                try:
                    request("/health")
                    request("/predict", payload)
                    sample_count[0] += 1
                except Exception as exc:
                    errors.append(type(exc).__name__)
                stop.wait(0.2)

        if args.check_rollout or args.check_failed_rollout:
            worker = threading.Thread(target=traffic, daemon=True)
            worker.start()
            try:
                if args.check_rollout:
                    before = identities()
                    kubectl(
                        "-n",
                        "mlops",
                        "rollout",
                        "restart",
                        "deployment/backend",
                        "deployment/frontend",
                    )
                    for app in ["backend", "frontend"]:
                        kubectl(
                            "-n",
                            "mlops",
                            "rollout",
                            "status",
                            f"deployment/{app}",
                            "--timeout=10m",
                        )
                    after = identities()
                    assert after and before.isdisjoint(
                        after
                    ), "Some old pods still exist after rollout"
                    report["rolling_update"] = "passed"
                if args.check_failed_rollout:
                    if not args.context.startswith("kind-mlops-stage3"):
                        raise RuntimeError(
                            "Negative rollout test is restricted to local test clusters"
                        )
                    patch = {
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [
                                        {
                                            "name": "backend",
                                            "env": [
                                                {
                                                    "name": "DVC_PULL",
                                                    "value": "invalid-test-value",
                                                }
                                            ],
                                        }
                                    ]
                                }
                            }
                        }
                    }
                    kubectl(
                        "-n",
                        "mlops",
                        "patch",
                        "deployment",
                        "backend",
                        "--type=strategic",
                        "-p",
                        json.dumps(patch),
                    )
                    try:
                        failed = False
                        for _ in range(90):
                            d = json.loads(
                                kubectl(
                                    "-n",
                                    "mlops",
                                    "get",
                                    "deployment",
                                    "backend",
                                    "-o",
                                    "json",
                                )
                            )
                            assert (
                                d["status"].get("availableReplicas", 0)
                                >= d["spec"]["replicas"]
                            )
                            for pod in pods():
                                if pod["metadata"]["labels"].get("app") != "backend":
                                    continue
                                env = pod["spec"]["containers"][0].get("env", [])
                                if any(
                                    v.get("name") == "DVC_PULL"
                                    and v.get("value") == "invalid-test-value"
                                    for v in env
                                ):
                                    states = pod["status"].get("containerStatuses", [])
                                    failed = any(
                                        c.get("restartCount", 0) > 0
                                        or c.get("state", {})
                                        .get("terminated", {})
                                        .get("exitCode", 0)
                                        != 0
                                        for c in states
                                    )
                            if failed:
                                break
                            time.sleep(1)
                        assert failed, "Bad revision did not fail as expected"
                        report["unready_revision_keeps_old_replicas"] = "passed"
                    finally:
                        kubectl("-n", "mlops", "rollout", "undo", "deployment/backend")
                        kubectl(
                            "-n",
                            "mlops",
                            "rollout",
                            "status",
                            "deployment/backend",
                            "--timeout=10m",
                        )
            finally:
                stop.set()
                worker.join(timeout=10)
            assert (
                sample_count[0] > 0 and not errors
            ), f"Traffic failed during rollout: {errors}"
            report["traffic_samples"] = sample_count[0]
            report["traffic_errors"] = errors
            print(
                f"Rolling update traffic: PASS ({sample_count[0]} request pairs, no errors)"
            )
        report["deployments"] = json.loads(
            kubectl("-n", "mlops", "get", "deployments", "-o", "json")
        )
        if args.evidence:
            args.evidence.parent.mkdir(parents=True, exist_ok=True)
            args.evidence.write_text(json.dumps(report, indent=2))
    finally:
        forward.terminate()
        try:
            forward.wait(timeout=10)
        except subprocess.TimeoutExpired:
            forward.kill()
            forward.wait()


if __name__ == "__main__":
    main()
