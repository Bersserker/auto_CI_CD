"""Изолированный E2E стенд с ONNX-моделью (Docker + готовый backend image)."""

import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def get(url, data=None, auth=None):
    body = None if data is None else json.dumps(data).encode()
    request = Request(url, data=body, headers={"Content-Type": "application/json"})
    if auth is not None:
        request.add_header(
            "Authorization", "Basic " + base64.b64encode(auth.encode()).decode()
        )
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def wait_for(check, description, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                print(f"PASS: {description}", flush=True)
                return
        except (OSError, KeyError, ValueError):
            pass
        time.sleep(5)
    raise RuntimeError(f"Timeout: {description}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep dashboards and data after the experiment",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=120,
        help="Seconds of successful traffic before incidents",
    )
    args = parser.parse_args()
    if args.warmup < 0:
        parser.error("--warmup must be nonnegative")
    environment = dict(os.environ, GRAFANA_ADMIN_PASSWORD="isolated-smoke-test")
    compose = [
        "docker",
        "compose",
        "-p",
        "scoring-observability-test",
        "-f",
        str(ROOT / "docker-compose.monitoring.yml"),
        "-f",
        str(ROOT / "docker-compose.monitoring-demo.yml"),
    ]

    def run(*arguments):
        subprocess.run([*compose, *arguments], check=True, env=environment)

    def alerts():
        return {
            a["labels"]["alertname"]
            for a in get("http://localhost:9093/api/v2/alerts")
            if a["status"]["state"] == "active"
        }

    def query(expression):
        return get(
            "http://localhost:9090/api/v1/query?" + urlencode({"query": expression})
        )["data"]["result"]

    # Only the dedicated test project's volumes are removed on exit.
    try:
        run("up", "-d")
        wait_for(
            lambda: get("http://localhost:8000/ready")["status"] == "ok",
            "API ready",
        )
        from src.api.models import CreditRequest

        payload = {name: 0 for name in CreditRequest.model_fields}
        payload.update(sex=1, age=30, limit_bal=100000)
        assert get("http://localhost:8000/predict", payload)["prediction"] in (0, 1)
        wait_for(
            lambda: query(
                'scoring_predictions_total{model_version="onnx-local-experiment"}'
            ),
            "prediction scraped",
        )
        wait_for(lambda: query("node_memory_MemTotal_bytes"), "host metrics")
        wait_for(
            lambda: query("container_memory_working_set_bytes"), "container metrics"
        )
        assert get("http://localhost:3000/api/health")["database"] == "ok"
        for uid in ("scoring-system", "scoring-logs"):
            wait_for(
                lambda: get(
                    f"http://localhost:3000/api/dashboards/uid/{uid}",
                    auth="admin:isolated-smoke-test",
                )["dashboard"]["panels"],
                f"dashboard {uid} provisioned",
            )
        print(f"Generating successful traffic for {args.warmup} seconds", flush=True)
        for _ in range(args.warmup):
            assert get("http://localhost:8000/predict", payload)["prediction"] in (0, 1)
            time.sleep(1)
        run("exec", "-T", "app", "touch", "/tmp/scoring-fail")
        print("Injecting synthetic inference errors for 180 seconds", flush=True)
        for _ in range(180):
            try:
                get("http://localhost:8000/predict", payload)
            except HTTPError as error:
                assert error.code == 503
            time.sleep(1)
        wait_for(
            lambda: {"ScoringErrors", "ScoringLogErrors"} <= alerts(),
            "metric and log alerts delivered",
        )
        run("exec", "-T", "app", "rm", "/tmp/scoring-fail")
        assert get("http://localhost:8000/predict", payload)["prediction"] in (0, 1)
        run("stop", "app")
        wait_for(lambda: "ScoringDown" in alerts(), "API down alert delivered")
        run("start", "app")
        wait_for(
            lambda: get("http://localhost:8000/ready")["status"] == "ok",
            "API recovered",
        )

        def recovered():
            assert get("http://localhost:8000/predict", payload)["prediction"] in (0, 1)
            return not ({"ScoringErrors", "ScoringLogErrors", "ScoringDown"} & alerts())

        wait_for(
            recovered,
            "all injected alerts resolved",
            timeout=420,
        )
    finally:
        if args.keep:
            print(
                "Стенд сохранён: http://localhost:3000, admin / isolated-smoke-test",
                flush=True,
            )
            print("Остановка: make monitoring-experiment-down", flush=True)
        else:
            run("down", "-v")


if __name__ == "__main__":
    main()
