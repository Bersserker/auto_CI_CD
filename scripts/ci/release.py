"""Render an immutable release, monitor HTTPS and restore the previous manifests."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.request import Request, urlopen

import yaml

NAMESPACE = "mlops"
PREVIOUS = "mlops-previous-release"


def kubectl(*args, payload=None):
    return subprocess.check_output(
        ["kubectl", *args],
        input=json.dumps(payload) if payload is not None else None,
        text=True,
    )


def apply(items, dry_run=False):
    args = ["apply", "-f", "-"]
    if dry_run:
        args.append("--dry-run=server")
    return kubectl(*args, payload={"apiVersion": "v1", "kind": "List", "items": items})


def sanitize(item):
    result = deepcopy(item)
    result.pop("status", None)
    metadata = result["metadata"]
    for field in [
        "uid",
        "resourceVersion",
        "generation",
        "creationTimestamp",
        "managedFields",
    ]:
        metadata.pop(field, None)
    metadata.get("annotations", {}).pop(
        "kubectl.kubernetes.io/last-applied-configuration", None
    )
    return result


def render(environment, release, domain, bucket, pull_secret=""):
    if environment not in {"staging", "production"}:
        raise ValueError("Unknown environment")
    if (
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain)
        or "." not in domain
    ):
        raise ValueError("APP_DOMAIN must be a DNS hostname")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ValueError("ARTIFACTS_BUCKET must be an S3 bucket name")
    if pull_secret and not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", pull_secret):
        raise ValueError("Invalid REGISTRY_PULL_SECRET")
    source = kubectl("kustomize", f"infrastructure/kubernetes/overlays/{environment}")
    items = list(yaml.safe_load_all(source))
    for item in items:
        if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "backend-config":
            item["data"]["DVC_REMOTE_URL"] = f"s3://{bucket}/dvc"
        if item["kind"] == "Ingress":
            item["spec"]["rules"][0]["host"] = domain
            item["spec"]["tls"][0]["hosts"] = [domain]
    config_digest = hashlib.sha256(
        json.dumps(
            [i for i in items if i["kind"] == "ConfigMap"], sort_keys=True
        ).encode()
    ).hexdigest()
    for item in items:
        if item["kind"] == "Deployment":
            name = item["metadata"]["name"]
            template = item["spec"]["template"]
            template["metadata"].setdefault("annotations", {}).update(
                {"mlops/config-sha256": config_digest, "mlops/release": release["sha"]}
            )
            template["spec"]["containers"][0]["image"] = release[name]
            if pull_secret:
                template["spec"]["imagePullSecrets"] = [{"name": pull_secret}]
    if "REPLACE_" in json.dumps(items):
        raise ValueError("Unresolved manifest placeholders")
    return items


def wait():
    for name in ["backend", "frontend"]:
        kubectl(
            "-n", NAMESPACE, "rollout", "status", f"deployment/{name}", "--timeout=15m"
        )


def request_payload():
    payload = {"limit_bal": 20000, "sex": 2, "education": 2, "marriage": 1, "age": 30}
    payload.update(
        {k: 0 for k in ["pay_0", "pay_2", "pay_3", "pay_4", "pay_5", "pay_6"]}
    )
    payload.update({f"bill_amt{i}": 0 for i in range(1, 7)})
    payload.update({f"pay_amt{i}": 0 for i in range(1, 7)})
    return payload


def monitor(domain, count=20, probe_token=None):
    # No HTTP fallback or disabled certificate validation.
    for _ in range(count):
        for path, payload in [
            ("/health", None),
            ("/predict", request_payload()),
            ("/_stcore/health", None),
        ]:
            suffix = (
                f"?mlops_canary={probe_token}"
                if probe_token and path == "/predict"
                else ""
            )
            request = Request(
                f"https://{domain}{path}{suffix}",
                data=json.dumps(payload).encode() if payload else None,
                headers={"Content-Type": "application/json"},
            )
            start = time.monotonic()
            with urlopen(request, timeout=5) as response:
                body = response.read()
                if response.status != 200 or time.monotonic() - start > 5:
                    raise ValueError("Smoke test status/latency threshold exceeded")
            if path == "/health" and json.loads(body).get("status") != "ok":
                raise ValueError("API is not healthy")
            if path == "/predict":
                result = json.loads(body)
                if (
                    result.get("prediction") not in (0, 1)
                    or not 0 <= result.get("default_probability", -1) <= 1
                ):
                    raise ValueError("Invalid prediction contract")
        time.sleep(1)
    print(
        f"Monitor passed: {count} health/predict/UI requests, no errors, latency <=5s"
    )


def snapshot(items):
    previous = []
    for item in items:
        if item["kind"] == "Namespace":
            continue
        raw = kubectl(
            "-n",
            NAMESPACE,
            "get",
            item["kind"],
            item["metadata"]["name"],
            "--ignore-not-found",
            "-o",
            "json",
        )
        if raw.strip():
            previous.append(sanitize(json.loads(raw)))
    return previous


def remember(items):
    # No Secret objects; small app manifest snapshot fits in a ConfigMap.
    apply(
        [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": PREVIOUS, "namespace": NAMESPACE},
                "data": {"manifests.json": json.dumps(items)},
            }
        ]
    )


def restore(items):
    apps = {i["metadata"]["name"] for i in items if i["kind"] == "Deployment"}
    if apps != {"backend", "frontend"}:
        raise ValueError(
            "No complete previous release; initial deployment requires diagnosis"
        )
    apply(items)
    wait()


def main():
    mode = os.environ["RELEASE_MODE"]
    domain = os.environ["APP_DOMAIN"]
    if mode == "rollback":
        stored = json.loads(
            kubectl("-n", NAMESPACE, "get", "configmap", PREVIOUS, "-o", "json")
        )
        previous = json.loads(stored["data"]["manifests.json"])
        current = snapshot(previous)
        restore(previous)
        monitor(domain)
        remember(current)
        return
    release = json.loads(Path(os.environ["RELEASE_FILE"]).read_text())
    items = render(
        os.environ["RELEASE_ENVIRONMENT"],
        release,
        domain,
        os.environ["ARTIFACTS_BUCKET"],
        os.getenv("REGISTRY_PULL_SECRET", ""),
    )
    kubectl("get", "ingressclass", "traefik")
    tls = json.loads(
        kubectl("-n", NAMESPACE, "get", "secret", "credit-tls", "-o", "json")
    )
    if tls.get("type") != "kubernetes.io/tls":
        raise ValueError("Configure TLS Secret credit-tls first")
    # Never log Secrets, including kubectl's JSON response above.
    apply(items, dry_run=True)
    previous = snapshot(items)
    deploy(items, previous, domain)


def deploy(items, previous, domain):
    try:
        apply(items)
        wait()
        monitor(domain)
    except Exception:
        print("Release failed; restoring the previous application manifests")
        restore(previous)
        monitor(domain, count=3)
        raise
    remember(previous)


if __name__ == "__main__":
    main()
