"""Проверки согласованности манифестов приложения без кластера."""

from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

BASE = Path(__file__).resolve().parents[1] / "infrastructure/kubernetes"


@pytest.mark.parametrize("app,port", [("backend", 8000), ("frontend", 8501)])
def test_deployment_update_and_probes(app, port):
    deployment = yaml.safe_load((BASE / "base" / f"{app}-deployment.yaml").read_text())
    spec = deployment["spec"]
    assert spec["replicas"] >= 2
    assert spec["strategy"]["rollingUpdate"] == {"maxSurge": 1, "maxUnavailable": 0}
    pod = spec["template"]["spec"]
    container = pod["containers"][0]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"]
    assert container["ports"][0]["containerPort"] == port
    assert all(
        p in container for p in ["startupProbe", "readinessProbe", "livenessProbe"]
    )
    if app == "backend":
        assert container["readinessProbe"]["httpGet"]["path"] == "/ready"
        assert container["livenessProbe"]["httpGet"]["path"] == "/live"
        assert {v["valueFrom"]["secretKeyRef"]["name"] for v in container["env"]} == {
            "s3-credentials"
        }
        assert {v["name"] for v in pod["volumes"]} >= {"models", "tmp"}


def test_services_match_pod_labels_and_named_port():
    for service in yaml.safe_load_all((BASE / "base/services.yaml").read_text()):
        app = service["metadata"]["name"]
        deployment = yaml.safe_load(
            (BASE / "base" / f"{app}-deployment.yaml").read_text()
        )
        assert (
            service["spec"]["selector"]
            == deployment["spec"]["template"]["metadata"]["labels"]
        )
        assert service["spec"]["ports"][0]["targetPort"] == "http"


def test_credentials_not_in_configmaps():
    for cm in yaml.safe_load_all((BASE / "base/configmaps.yaml").read_text()):
        assert not ({"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"} & cm["data"].keys())


@pytest.mark.parametrize("environment", ["local", "staging", "production"])
def test_overlays_render(environment):
    kubectl = shutil.which("kubectl")
    if not kubectl:
        pytest.skip("kubectl is needed to render Kustomize overlays")
    rendered = subprocess.check_output(
        [kubectl, "kustomize", str(BASE / "overlays" / environment)], text=True
    )
    resources = list(yaml.safe_load_all(rendered))
    assert all(
        r["metadata"].get("namespace") == "mlops"
        for r in resources
        if r["kind"] != "Namespace"
    )
    assert sum(r["kind"] == "Deployment" for r in resources) == 2
    assert not any(r["kind"] == "Secret" for r in resources)
    ingress = next(r for r in resources if r["kind"] == "Ingress")
    paths = ingress["spec"]["rules"][0]["http"]["paths"]
    assert {p["path"]: p["backend"]["service"]["name"] for p in paths} == {
        "/": "frontend",
        "/predict": "backend",
        "/health": "backend",
    }
    if environment == "local":
        assert "REPLACE_" not in rendered
        assert any(
            r["kind"] == "ConfigMap"
            and r.get("data", {}).get("DVC_REMOTE_URL") == "/remote"
            for r in resources
        )
