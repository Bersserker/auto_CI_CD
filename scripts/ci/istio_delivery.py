"""Transactional blue/green and weighted delivery of both components through Istio."""

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import ssl
import subprocess
import threading
import time
from urllib.request import Request, urlopen
import uuid

import yaml

from scripts.ci.release import request_payload

NAMESPACE = "mlops"
STATE = "mlops-release-state"
ROOT = Path(__file__).resolve().parents[2]


class Rejected(RuntimeError):
    """A delivery gate failed; the candidate must not advance."""


class Cluster:
    def __init__(self, context):
        if not context:
            raise ValueError("An explicit Kubernetes context is required")
        self.prefix = ["kubectl", "--context", context]
        self.context = context
        self.version = None

    def run(self, *args, payload=None, timeout=120):
        return subprocess.check_output(
            [*self.prefix, *args],
            text=True,
            input=json.dumps(payload) if payload is not None else None,
            timeout=timeout,
        )

    def get(self, kind, name):
        raw = self.run(
            "-n", NAMESPACE, "get", kind, name, "--ignore-not-found", "-o", "json"
        )
        return json.loads(raw) if raw.strip() else None

    def apply(self, items, dry_run=False):
        args = ["apply", "-f", "-"]
        if dry_run:
            args.append("--dry-run=server")
        self.run(*args, payload={"apiVersion": "v1", "kind": "List", "items": items})

    def read_state(self):
        item = self.get("configmap", STATE)
        self.version = item["metadata"]["resourceVersion"] if item else None
        return json.loads(item["data"]["state.json"]) if item else None

    def write_state(self, state):
        metadata = {"name": STATE, "namespace": NAMESPACE}
        if self.version:
            metadata["resourceVersion"] = self.version
        item = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": metadata,
            "data": {"state.json": json.dumps(state)},
        }
        saved = json.loads(
            self.run(
                "replace" if self.version else "create",
                "-f",
                "-",
                "-o",
                "json",
                payload=item,
            )
        )
        self.version = saved["metadata"]["resourceVersion"]

    def wait(self, slot, timeout="15m"):
        for app in ["backend", "frontend"]:
            self.run(
                "-n",
                NAMESPACE,
                "rollout",
                "status",
                f"deployment/{app}-{slot}",
                f"--timeout={timeout}",
                timeout=1000,
            )

    @contextmanager
    def forward(self, service, port, namespace=NAMESPACE):
        # Drain output continuously: a full pipe otherwise blocks kubectl under traffic.
        process = subprocess.Popen(
            [
                *self.prefix,
                "-n",
                namespace,
                "port-forward",
                f"service/{service}",
                f":{port}",
                "--address=127.0.0.1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        ports = []
        ready = threading.Event()

        def drain():
            for line in process.stdout:
                match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", line)
                if match:
                    ports.append(int(match[1]))
                    ready.set()

        thread = threading.Thread(target=drain, daemon=True)
        thread.start()
        try:
            if not ready.wait(30):
                raise Rejected(f"Cannot forward service/{service}")
            yield f"http://127.0.0.1:{ports[0]}"
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            thread.join(timeout=2)


def slot_resources(items, slot, release):
    """Clone the tested environment's pod templates, isolating selectors and configuration."""
    if slot not in {"blue", "green"}:
        raise ValueError("Invalid slot")
    result = []
    for original in items:
        kind = original["kind"]
        name = original["metadata"]["name"]
        if kind not in {"Deployment", "ConfigMap", "Service"} or name not in {
            "backend",
            "frontend",
            "backend-config",
            "frontend-config",
        }:
            continue
        item = deepcopy(original)
        item["metadata"]["name"] = f"{name}-{slot}"
        item["metadata"].setdefault("labels", {})["mlops/slot"] = slot
        if kind == "ConfigMap" and name == "frontend-config":
            item["data"]["API_BASE_URL"] = f"http://backend-{slot}:8000"
        if kind == "Service":
            item["metadata"].pop("annotations", None)
            item["spec"]["selector"] = {"app": name, "slot": slot}
        if kind == "Deployment":
            spec = item["spec"]
            spec["selector"]["matchLabels"] = {"app": name, "slot": slot}
            template = spec["template"]
            template["metadata"]["labels"].update(app=name, slot=slot)
            # The Gateway owns traffic routing; no sidecars or ambient enrollment.
            template["metadata"]["labels"]["sidecar.istio.io/inject"] = "false"
            template["metadata"].setdefault("annotations", {}).pop(
                "sidecar.istio.io/inject", None
            )
            container = template["spec"]["containers"][0]
            for reference in container.get("envFrom", []):
                if "configMapRef" in reference:
                    reference["configMapRef"]["name"] += f"-{slot}"
        result.append(item)
    configs = [r for r in result if r["kind"] == "ConfigMap"]
    digest = hashlib.sha256(json.dumps(configs, sort_keys=True).encode()).hexdigest()
    for deployment in [r for r in result if r["kind"] == "Deployment"]:
        template = deployment["spec"]["template"]
        template["metadata"]["annotations"].update(
            {"mlops/config-sha256": digest, "mlops/release": release["sha"]}
        )
    if len(result) != 6:
        raise ValueError("Expected two Deployments, two Services and two ConfigMaps")
    return result


def shared_resources(cluster, domain):
    items = list(
        yaml.safe_load_all(
            cluster.run("kustomize", str(ROOT / "infrastructure/kubernetes/istio"))
        )
    )
    gateway = next(i for i in items if i["kind"] == "Gateway")
    gateway["spec"]["servers"][0]["hosts"] = [domain]
    return items


def virtual_service(domain, weights, slots, generation=None):
    if sum(weights.values()) != 100 or any(w < 0 for w in weights.values()):
        raise ValueError("Traffic weights must sum to 100")
    generation = generation or uuid.uuid4().hex
    routes = []
    for app, port in [("backend", 8000), ("frontend", 8501)]:
        route = {
            "name": app,
            "headers": {"response": {"set": {"x-mlops-route": generation}}},
            "route": [
                {
                    "destination": {
                        "host": f"{app}.mlops.svc.cluster.local",
                        "subset": slot,
                        "port": {"number": port},
                    },
                    "weight": weight,
                    "headers": {
                        "response": {
                            "set": {
                                "x-mlops-slot": slot,
                                "x-mlops-release": slots[slot]["sha"],
                            }
                        }
                    },
                }
                for slot, weight in weights.items()
                if weight
            ],
        }
        if app == "backend":
            route["match"] = [
                {"uri": {"exact": path}}
                for path in ["/health", "/predict", "/ready", "/live"]
            ]
            # No automatic retries: do not hide application errors from the release gate.
            route["timeout"] = "5s"
        routes.append(route)
    return {
        "apiVersion": "networking.istio.io/v1",
        "kind": "VirtualService",
        "metadata": {"name": "credit-scoring", "namespace": NAMESPACE},
        "spec": {"hosts": [domain], "gateways": ["credit-scoring"], "http": routes},
    }


class Probe:
    def __init__(
        self,
        domain,
        base_url=None,
        ca_file=None,
        count=30,
        p95_limit=1.0,
        max_latency=5.0,
        interval=1.0,
    ):
        self.domain = domain
        self.base_url = (base_url or f"https://{domain}").rstrip("/")
        if not self.base_url.startswith("https://"):
            raise ValueError("Gateway probes require HTTPS")
        if count < 30 or not 0 < p95_limit <= max_latency or interval < 0:
            raise ValueError("Require >=30 samples and 0 < p95 <= maximum latency")
        self.context = ssl.create_default_context(cafile=ca_file)
        self.count, self.p95_limit, self.max_latency, self.interval = (
            count,
            p95_limit,
            max_latency,
            interval,
        )

    def request(self, path, stage, *, base_url=None):
        payload = json.dumps(request_payload()).encode() if path == "/predict" else None
        request = Request(
            f"{base_url or self.base_url}{path}",
            data=payload,
            headers={
                "Host": self.domain,
                "Content-Type": "application/json",
                "X-Release-Stage": stage,
            },
        )
        start = time.monotonic()
        with (getattr(self, "opener", None) or urlopen)(
            request, context=self.context, timeout=self.max_latency
        ) as response:
            body = response.read()
            elapsed = time.monotonic() - start
            if response.status != 200:
                raise Rejected(f"HTTP {response.status} from {path} at {stage}")
            if elapsed > self.max_latency:
                raise Rejected(f"Request exceeded {self.max_latency}s at {stage}")
            headers = dict(response.headers.items())
        if path == "/predict":
            result = json.loads(body)
            probability = result.get("default_probability")
            if (
                type(result.get("prediction")) is not int
                or result["prediction"] not in {0, 1}
                or not isinstance(probability, (int, float))
                or not math.isfinite(probability)
                or not 0 <= probability <= 1
            ):
                raise Rejected("Invalid prediction contract")
        elif path == "/health" and json.loads(body).get("status") != "ok":
            raise Rejected("Model is not healthy")
        return elapsed, {k.lower(): v for k, v in headers.items()}

    def synchronized(self, generation, weights):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                headers = [
                    self.request(path, "routing-sync")[1]
                    for path in ["/health", "/_stcore/health"]
                ]
                if all(
                    h.get("x-mlops-route") == generation
                    and h.get("x-mlops-slot") in weights
                    for h in headers
                ):
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise Rejected(
            "Istio did not acknowledge the requested route through the Gateway"
        )

    def measure(self, stage, weights, slots, generation):
        latencies, hits = [], {
            app: {slot: 0 for slot in weights} for app in ["backend", "frontend"]
        }
        # Give small random traffic shares extra samples without accepting an
        # unobserved destination or making the release wait indefinitely.
        for cycle in range(self.count * 3):
            for path, app in [
                ("/health", "backend"),
                ("/predict", "backend"),
                ("/_stcore/health", "frontend"),
            ]:
                elapsed, headers = self.request(path, stage)
                slot = headers.get("x-mlops-slot")
                if (
                    headers.get("x-mlops-route") != generation
                    or slot not in weights
                    or headers.get("x-mlops-release") != slots[slot]["sha"]
                ):
                    raise Rejected(
                        "Response came from an unexpected route, slot or release"
                    )
                hits[app][slot] += 1
                latencies.append(elapsed)
            time.sleep(self.interval)
            if cycle + 1 >= self.count and all(
                count for app in hits.values() for count in app.values()
            ):
                break
        # A configured percentage is insufficient: each routed slot must actually be observed.
        if any(not count for app in hits.values() for count in app.values()):
            raise Rejected(
                "Not all weighted destinations were observed; extend sampling"
            )
        p95 = sorted(latencies)[math.ceil(len(latencies) * 0.95) - 1]
        if p95 > self.p95_limit:
            raise Rejected(f"p95={p95:.3f}s exceeds {self.p95_limit}s")
        return {
            "stage": stage,
            "weights": weights,
            "requests": len(latencies),
            "errors": 0,
            "p95_seconds": p95,
            "max_seconds": max(latencies),
            "observed": hits,
        }


class Delivery:
    def __init__(self, cluster, probe, evidence_file=None, readiness_timeout="15m"):
        self.cluster, self.probe = cluster, probe
        self.evidence_file, self.readiness_timeout = evidence_file, readiness_timeout
        self.report = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "steps": [],
        }

    def evidence(self, **fields):
        self.report.update(fields)
        if self.evidence_file:
            path = Path(self.evidence_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.report, indent=2) + "\n")

    def route(self, weights, slots, stage):
        self.report.setdefault("route_changes", []).append(
            {"stage": stage, "weights": weights}
        )
        self.evidence()
        route = virtual_service(self.probe.domain, weights, slots)
        self.cluster.apply([route], dry_run=True)
        self.cluster.apply([route])
        generation = route["spec"]["http"][0]["headers"]["response"]["set"][
            "x-mlops-route"
        ]
        self.probe.synchronized(generation, weights)
        result = self.probe.measure(stage, weights, slots, generation)
        self.report["steps"].append(result)
        self.evidence()
        return result

    def preview(self, slot):
        self.cluster.wait(slot, self.readiness_timeout)
        for app, port, paths in [
            ("backend", 8000, ["/health", "/predict"]),
            ("frontend", 8501, ["/_stcore/health"]),
        ]:
            with self.cluster.forward(f"{app}-{slot}", port) as base_url:
                for path in paths:
                    self.probe.request(path, "preview", base_url=base_url)
        # Submit the actual Streamlit form against that slot's backend Service.
        code = """from streamlit.testing.v1 import AppTest
app = AppTest.from_file('src/frontend/app.py').run(timeout=30)
assert not app.exception, app.exception
next(b for b in app.button if b.label == 'Рассчитать вероятность дефолта').click().run(timeout=30)
assert not app.exception and not app.error, app.error
assert app.metric, 'No model prediction'
"""
        self.cluster.run(
            "-n",
            NAMESPACE,
            "exec",
            f"deployment/frontend-{slot}",
            "--",
            "python",
            "-c",
            code,
        )

    def recover(self, state, reason):
        # Refresh resourceVersion after an ambiguous write/network failure.
        self.cluster.read_state()
        active = state.get("active")
        if not active:
            self.evidence(
                status="failed-bootstrap", error=reason, rollback="unavailable"
            )
            # An unsuccessful first release has no healthy baseline. Stop publishing it.
            self.cluster.run(
                "-n",
                NAMESPACE,
                "delete",
                "virtualservice",
                "credit-scoring",
                "--ignore-not-found",
            )
            state["pending"] = None
            self.cluster.write_state(state)
            return
        try:
            self.route({active: 100}, state["slots"], "automatic-rollback")
        except Exception as error:
            # Keep pending state: the manual rollback command can retry after connectivity is restored.
            self.evidence(
                status="recovery-required", error=reason, rollback_error=str(error)
            )
            raise
        state["pending"] = None
        self.cluster.write_state(state)
        self.evidence(status="rejected", error=reason, rollback="passed", active=active)

    def deploy(self, mode, release, items, allow_initial=False):
        if mode not in {"blue-green", "canary"}:
            raise ValueError("Unknown deployment strategy")
        state = self.cluster.read_state()
        if state and state.get("pending"):
            raise Rejected(
                "Interrupted delivery found; run rollback to restore the active slot first"
            )
        if not state or not state.get("active"):
            if not allow_initial:
                raise Rejected(
                    "No baseline: set ALLOW_INITIAL_DEPLOY=true for the first release"
                )
            state = {"active": None, "previous": None, "pending": None, "slots": {}}
            candidate = "blue"
            # Refuse silent migration from rolling Deployments with different traffic ownership.
            if any(
                self.cluster.get("deployment", name) for name in ["backend", "frontend"]
            ):
                raise Rejected(
                    "Rolling Deployments exist; use a separate cluster/namespace or the documented migration"
                )
            shared = shared_resources(self.cluster, self.probe.domain)
            self.cluster.apply(shared, dry_run=True)
            self.cluster.apply(shared)
        else:
            self.route({state["active"]: 100}, state["slots"], "baseline")
            candidate = "green" if state["active"] == "blue" else "blue"
        if state["previous"] == candidate:
            state["previous"] = None
        # Write intent before changing the inactive slot; resourceVersion provides optimistic locking.
        state["pending"] = {"slot": candidate, "release": release, "mode": mode}
        self.cluster.write_state(state)
        candidate_slots = {**state["slots"], candidate: release}
        try:
            resources = slot_resources(items, candidate, release)
            self.cluster.apply(resources, dry_run=True)
            self.cluster.apply(resources)
            self.preview(candidate)
            self.report["steps"].append(
                {"stage": "preview", "slot": candidate, "status": "passed"}
            )
            if not state["active"]:
                weights = [100]
            else:
                weights = [100] if mode == "blue-green" else policy()["canary_weights"]
            for weight in weights:
                distribution = {candidate: weight}
                if weight != 100:
                    distribution[state["active"]] = 100 - weight
                self.route(distribution, candidate_slots, f"{mode}-{weight}")
            old_active = state["active"]
            promoted = deepcopy(state)
            promoted.update(
                active=candidate,
                previous=old_active,
                slots=candidate_slots,
                pending=None,
            )
            self.cluster.write_state(promoted)
            self.evidence(
                status="promoted",
                mode=mode,
                active=candidate,
                previous=old_active,
                release=release,
            )
        except BaseException as error:
            # SIGTERM is converted to an exception so ordinary cancellation can restore traffic.
            self.recover(state, str(error))
            raise

    def rollback(self):
        state = self.cluster.read_state()
        if not state or not state.get("active"):
            raise Rejected("No healthy active release to restore")
        target = state["active"] if state.get("pending") else state.get("previous")
        if not target:
            raise Rejected("No previous slot; bootstrap cannot be rolled back")
        self.cluster.wait(target, self.readiness_timeout)
        try:
            self.route({target: 100}, state["slots"], "manual-rollback")
            restored = deepcopy(state)
            if not state.get("pending"):
                restored["previous"] = state["active"]
            restored.update(active=target, pending=None)
            self.cluster.write_state(restored)
        except BaseException as error:
            self.recover(state, str(error))
            raise
        self.evidence(status="rolled-back", active=target)


def policy():
    data = yaml.safe_load(
        (ROOT / "infrastructure/kubernetes/istio/release-policy.yaml").read_text()
    )
    if (
        data["canary_weights"] != sorted(set(data["canary_weights"]))
        or data["canary_weights"][-1] != 100
        or data["canary_weights"][0] <= 0
    ):
        raise ValueError("Canary steps must increase from >0 to 100")
    return data


def interrupted(signum, _frame):
    raise Rejected(f"Delivery interrupted by signal {signum}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupted)
    cluster = Cluster(args.context)
    criteria = policy()
    probe = Probe(
        os.environ["APP_DOMAIN"],
        count=criteria["samples"],
        interval=criteria["interval_seconds"],
        p95_limit=criteria["p95_seconds"],
        max_latency=criteria["max_request_seconds"],
    )
    delivery = Delivery(cluster, probe, os.getenv("RELEASE_EVIDENCE"))
    mode = os.environ["RELEASE_MODE"]
    if mode == "rollback":
        delivery.rollback()
        return
    from scripts.ci.release import render

    release = json.loads(Path(os.environ["RELEASE_FILE"]).read_text())
    items = render(
        os.environ["RELEASE_ENVIRONMENT"],
        release,
        probe.domain,
        os.environ["ARTIFACTS_BUCKET"],
        os.getenv("REGISTRY_PULL_SECRET", ""),
    )
    # Credentials are installed ahead of time, but TLS lives beside the Istio gateway workload.
    cluster.run(
        "get",
        "crd",
        "virtualservices.networking.istio.io",
        "destinationrules.networking.istio.io",
    )
    cluster.run(
        "-n",
        "istio-system",
        "rollout",
        "status",
        "deployment/istio-ingressgateway",
        "--timeout=5m",
        timeout=330,
    )
    tls = json.loads(
        cluster.run("-n", "istio-system", "get", "secret", "credit-tls", "-o", "json")
    )
    if tls.get("type") != "kubernetes.io/tls":
        raise Rejected("Configure istio-system/credit-tls first")
    delivery.deploy(mode, release, items, os.getenv("ALLOW_INITIAL_DEPLOY") == "true")


if __name__ == "__main__":
    main()
