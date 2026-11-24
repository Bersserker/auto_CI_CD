"""Delivery failure paths are tested without cloud credentials or a cluster."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.ci import release
from scripts.ci.fetch_release import validate_release

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
DIGEST = "b" * 64


def images():
    return {
        "sha": SHA,
        "run_id": "123",
        "backend": f"ghcr.io/owner/project/credit-backend@sha256:{DIGEST}",
        "frontend": f"ghcr.io/owner/project/credit-frontend@sha256:{DIGEST}",
    }


def test_only_the_tested_repository_digests_are_accepted():
    candidate = images()
    run = {"head_sha": SHA, "id": 123}
    validate_release(candidate, run, "Owner/Project")
    for replacement in [
        "ghcr.io/attacker/model@sha256:" + DIGEST,
        "credit-backend:latest",
    ]:
        with pytest.raises(ValueError):
            validate_release(
                {**candidate, "backend": replacement}, run, "Owner/Project"
            )
    with pytest.raises(ValueError):
        validate_release({**candidate, "sha": "c" * 40}, run, "Owner/Project")


def test_failed_rollout_restores_both_apps_and_still_fails_the_job(monkeypatch):
    operations = []
    previous = [
        {"kind": "Deployment", "metadata": {"name": name}}
        for name in ["backend", "frontend"]
    ]
    monkeypatch.setattr(
        release, "apply", lambda items: operations.append(("apply", items))
    )
    attempts = iter([RuntimeError("candidate readiness failed"), None])

    def wait():
        result = next(attempts)
        if result:
            raise result

    monkeypatch.setattr(release, "wait", wait)
    monkeypatch.setattr(
        release, "monitor", lambda *a, **kw: operations.append(("monitor",))
    )
    monkeypatch.setattr(
        release,
        "remember",
        lambda *_: pytest.fail("Failed release must not replace rollback snapshot"),
    )
    with pytest.raises(RuntimeError, match="readiness failed"):
        release.deploy([{"candidate": True}], previous, "credit.example.org")
    assert operations[1] == ("apply", previous)
    assert operations[2] == ("monitor",)


def test_first_deployment_does_not_claim_to_have_a_rollback():
    with pytest.raises(ValueError, match="No complete previous release"):
        release.restore([])


def test_sanitized_snapshot_has_no_server_fields_and_keeps_configuration():
    live = {
        "kind": "ConfigMap",
        "metadata": {
            "name": "backend-config",
            "uid": "uid",
            "resourceVersion": "3",
            "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "old"},
        },
        "data": {"DVC_REMOTE_URL": "s3://old-bucket/dvc"},
        "status": {},
    }
    copy = deepcopy(live)
    clean = release.sanitize(live)
    assert live == copy
    assert "status" not in clean and "uid" not in clean["metadata"]
    assert clean["data"]["DVC_REMOTE_URL"] == "s3://old-bucket/dvc"


def test_monitor_checks_the_real_api_contract(monkeypatch):
    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self):
            return json.dumps(self.body).encode()

    invalid = False

    def urlopen(request, **kwargs):
        if request.full_url.endswith("/predict"):
            return Response(
                {"prediction": 0, "default_probability": 1.1 if invalid else 0.5}
            )
        return Response({"status": "ok"})

    monkeypatch.setattr(release, "urlopen", urlopen)
    monkeypatch.setattr(release.time, "sleep", lambda *_: None)
    release.monitor("credit.example.org", count=1)
    invalid = True
    with pytest.raises(ValueError, match="prediction contract"):
        release.monitor("credit.example.org", count=1)


@pytest.mark.parametrize("environment,replicas", [("staging", 2), ("production", 3)])
def test_release_renders_real_overlays_with_digests_and_restart_hash(
    monkeypatch, environment, replicas
):
    import shutil

    if shutil.which("kubectl") is None:
        pytest.skip("kubectl is needed to render real overlays")
    monkeypatch.chdir(ROOT)
    items = release.render(
        environment, images(), "credit.example.org", "credit-artifacts", "ghcr-pull"
    )
    assert "REPLACE_" not in json.dumps(items)
    deployments = [item for item in items if item["kind"] == "Deployment"]
    assert {d["metadata"]["name"] for d in deployments} == {"backend", "frontend"}
    for deployment in deployments:
        assert deployment["spec"]["replicas"] == replicas
        template = deployment["spec"]["template"]
        assert (
            template["spec"]["containers"][0]["image"]
            == images()[deployment["metadata"]["name"]]
        )
        assert template["spec"]["imagePullSecrets"] == [{"name": "ghcr-pull"}]
    changed = release.render(
        environment, images(), "credit.example.org", "different-bucket"
    )
    changed_deployment = next(item for item in changed if item["kind"] == "Deployment")
    assert (
        deployments[0]["spec"]["template"]["metadata"]["annotations"][
            "mlops/config-sha256"
        ]
        != changed_deployment["spec"]["template"]["metadata"]["annotations"][
            "mlops/config-sha256"
        ]
    )
