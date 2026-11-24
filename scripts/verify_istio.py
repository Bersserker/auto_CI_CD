"""Exercise the real delivery controller against a disposable kind cluster."""

import argparse
from contextlib import contextmanager
from copy import deepcopy
from http.client import HTTPSConnection
import json
from pathlib import Path
import socket
from urllib.parse import urlsplit
from urllib.request import urlopen

import yaml

from scripts.ci.istio_delivery import ROOT, Cluster, Delivery, Probe

# This fault fixture is mounted only by the local demonstration, never by workflows.
FAULT_APP = """import sys
sys.path.insert(0, '/app')
from src.api.app import app as backend
import os
from starlette.responses import JSONResponse
async def app(scope, receive, send):
    headers = dict(scope.get('headers', []))
    if (scope.get('path') == '/predict'
            and headers.get(b'x-release-stage', b'').decode() == os.environ['DEMO_FAIL_STAGE']):
        await JSONResponse({'detail': 'intentional local release fault'}, status_code=503)(scope, receive, send)
    else:
        await backend(scope, receive, send)
"""


def release(number, version):
    return {
        "sha": f"{number:040x}",
        "run_id": f"local-{number}",
        "backend": f"credit-backend:stage4-{version}",
        "frontend": "credit-frontend:local",
    }


def items_for(cluster, candidate, version, fault=None, unready=False):
    items = list(
        yaml.safe_load_all(
            cluster.run(
                "kustomize", str(ROOT / "infrastructure/kubernetes/overlays/local")
            )
        )
    )
    for item in items:
        if item["kind"] == "Deployment":
            name = item["metadata"]["name"]
            pod = item["spec"]["template"]["spec"]
            container = pod["containers"][0]
            container["image"] = candidate[name]
            if name == "backend":
                pod["volumes"] = [
                    v for v in pod["volumes"] if v["name"] != "fixture-lock"
                ]
                container["volumeMounts"] = [
                    v for v in container["volumeMounts"] if v["name"] != "fixture-lock"
                ]
                next(v for v in pod["volumes"] if v["name"] == "local-remote")[
                    "hostPath"
                ]["path"] = f"/mlops-fixture/{version}/remote"
                if fault:
                    container["args"] = [
                        "uvicorn",
                        "fault_app:app",
                        "--app-dir",
                        "/demo",
                        "--host",
                        "0.0.0.0",
                        "--port",
                        "8000",
                    ]
                    container["env"].append({"name": "DEMO_FAIL_STAGE", "value": fault})
                    container["volumeMounts"].append(
                        {"name": "demo-fault", "mountPath": "/demo", "readOnly": True}
                    )
                    pod["volumes"].append(
                        {
                            "name": "demo-fault",
                            "configMap": {"name": "local-demo-fault"},
                        }
                    )
                if unready:
                    container["env"].append(
                        {"name": "DVC_PULL", "value": "invalid-local-test"}
                    )
    return items


@contextmanager
def tunnel_request(request, context, timeout):
    """Preserve TLS SNI while connecting to a localhost kubectl tunnel."""
    url = urlsplit(request.full_url)
    if url.scheme == "http":
        with urlopen(request, timeout=timeout) as response:
            yield response
        return
    if url.hostname != "127.0.0.1":
        raise ValueError("Test transport supports only the local tunnel")
    connection = HTTPSConnection(
        "credit.local", port=url.port, context=context, timeout=timeout
    )
    connection._create_connection = lambda *a, **kw: socket.create_connection(
        ("127.0.0.1", url.port), timeout
    )
    try:
        connection.request(
            request.get_method(),
            url.path,
            body=request.data,
            headers=dict(request.header_items()),
        )
        with connection.getresponse() as response:
            yield response
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--certificate", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.context.startswith("kind-mlops-strategies-"):
        parser.error(
            "Fault injection is allowed only in the disposable strategies test cluster"
        )
    cluster = Cluster(args.context)
    directory = args.evidence_dir
    directory.mkdir(parents=True, exist_ok=True)
    cluster.apply(
        [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "local-demo-fault", "namespace": "mlops"},
                "data": {"fault_app.py": FAULT_APP},
            }
        ]
    )
    summary = []
    with cluster.forward(
        "istio-ingressgateway", 443, namespace="istio-system"
    ) as forwarded:
        probe = Probe(
            "credit.local",
            base_url=forwarded.replace("http://", "https://"),
            ca_file=args.certificate,
            count=60,
            interval=0.02,
        )

        probe.opener = tunnel_request

        def scenario(
            name,
            mode,
            number,
            version,
            expected_slot,
            probability,
            fault=None,
            unready=False,
        ):
            candidate = release(number, version)
            delivery = Delivery(
                cluster,
                probe,
                directory / f"{name}.json",
                readiness_timeout="30s" if unready else "5m",
            )
            before = deepcopy(cluster.read_state())
            failed = fault is not None or unready
            try:
                delivery.deploy(
                    mode,
                    candidate,
                    items_for(cluster, candidate, version, fault, unready),
                    allow_initial=True,
                )
            except Exception:
                if not failed:
                    raise
            else:
                if failed:
                    raise AssertionError(
                        "The controller accepted an intentionally broken candidate"
                    )
            state = cluster.read_state()
            assert state["active"] == expected_slot and state["pending"] is None, state
            if failed:
                assert (
                    state["slots"][state["active"]] == before["slots"][before["active"]]
                )
                assert delivery.report["rollback"] == "passed", delivery.report
                if fault:
                    assert "HTTP 503" in delivery.report["error"], delivery.report
                    assert any(
                        change["stage"] == fault
                        for change in delivery.report["route_changes"]
                    ), delivery.report
                if fault == "canary-50":
                    assert {"canary-10", "canary-25"} <= {
                        step["stage"] for step in delivery.report["steps"]
                    }
            _, headers = probe.request("/predict", "final-check")
            assert headers["x-mlops-slot"] == expected_slot
            # Also verify the model result really changed, not just route metadata.
            from urllib.request import Request

            from scripts.ci.release import request_payload

            request = Request(
                f"{probe.base_url}/predict",
                data=json.dumps(request_payload()).encode(),
                headers={"Host": "credit.local", "Content-Type": "application/json"},
            )
            with probe.opener(request, context=probe.context, timeout=5) as response:
                result = json.load(response)
            assert result["default_probability"] == probability, result
            summary.append(
                {
                    "scenario": name,
                    "status": "passed",
                    "active": state["active"],
                    "probability": probability,
                    "route_changes": delivery.report.get("route_changes", []),
                    "expected_rejection": failed,
                }
            )
            (directory / "result.json").write_text(json.dumps(summary, indent=2) + "\n")
            print(
                f"PASS: {name}, active={expected_slot}, probability={probability}",
                flush=True,
            )

        scenario("bootstrap", "blue-green", 1, "v1", "blue", 0.5)
        scenario("blue-green", "blue-green", 2, "v2", "green", 0.75)
        # Reject a Ready candidate AFTER routing 100% of traffic to it.
        scenario(
            "blue-green-failed",
            "blue-green",
            3,
            "v1",
            "green",
            0.75,
            fault="blue-green-100",
        )
        scenario("canary", "canary", 4, "v1", "blue", 0.5)
        # 10% and 25% pass; a real candidate HTTP 503 at 50% must restore blue.
        scenario("canary-failed", "canary", 5, "v2", "blue", 0.5, fault="canary-50")
        scenario("unready-candidate", "blue-green", 6, "v2", "blue", 0.5, unready=True)
        # Re-establish a successful second slot, then demonstrate operator rollback.
        scenario("before-manual-rollback", "blue-green", 7, "v2", "green", 0.75)
        delivery = Delivery(cluster, probe, directory / "manual-rollback.json")
        delivery.rollback()
        assert cluster.read_state()["active"] == "blue"
        summary.append(
            {"scenario": "manual-rollback", "status": "passed", "active": "blue"}
        )
        (directory / "result.json").write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
