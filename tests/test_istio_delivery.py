"""Delivery transactions and release gates; actual Envoy traffic is exercised in kind."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from scripts.ci.istio_delivery import (
    Delivery,
    Probe,
    Rejected,
    slot_resources,
    virtual_service,
)


class MemoryCluster:
    def __init__(self):
        self.state = {
            "active": "blue",
            "previous": "green",
            "pending": None,
            "slots": {"blue": {"sha": "old"}, "green": {"sha": "older"}},
        }
        self.routes = []
        self.fail_commit = False

    def read_state(self):
        return deepcopy(self.state)

    def write_state(self, state):
        if self.fail_commit and state["active"] == "green":
            self.fail_commit = False
            raise RuntimeError("Commit connection lost")
        self.state = deepcopy(state)

    def apply(self, items, dry_run=False):
        if not dry_run and items[0]["kind"] == "VirtualService":
            self.routes.append(deepcopy(items[0]))

    def wait(self, *args):
        pass


class FakeProbe:
    domain = "credit.example.org"

    def __init__(self, failed_stage=None):
        self.failed_stage = failed_stage

    def synchronized(self, *args):
        pass

    def measure(self, stage, weights, *args):
        if stage == self.failed_stage:
            raise Rejected("Real candidate HTTP 503")
        return {"stage": stage, "weights": weights}


def inputs():
    result = []
    for app in ["backend", "frontend"]:
        result.extend(
            [
                {
                    "kind": "ConfigMap",
                    "metadata": {"name": app + "-config"},
                    "data": {},
                },
                {
                    "kind": "Service",
                    "metadata": {"name": app},
                    "spec": {"selector": {"app": app}},
                },
                {
                    "kind": "Deployment",
                    "metadata": {"name": app},
                    "spec": {
                        "selector": {"matchLabels": {"app": app}},
                        "template": {
                            "metadata": {"labels": {"app": app}},
                            "spec": {
                                "containers": [
                                    {
                                        "envFrom": [
                                            {"configMapRef": {"name": app + "-config"}}
                                        ]
                                    }
                                ]
                            },
                        },
                    },
                },
            ]
        )
    return result


def controller(cluster, failed_stage=None):
    instance = Delivery(cluster, FakeProbe(failed_stage))
    instance.preview = lambda *_: None
    return instance


@pytest.mark.parametrize(
    "mode,expected", [("blue-green", [100]), ("canary", [10, 25, 50, 100])]
)
def test_both_components_promote_and_the_previous_slot_remains_available(
    mode, expected
):
    cluster = MemoryCluster()
    delivery = controller(cluster)
    delivery.deploy(mode, {"sha": "new"}, inputs())
    assert cluster.state["active"] == "green"
    assert cluster.state["previous"] == "blue"
    assert cluster.state["slots"]["blue"]["sha"] == "old"
    assert cluster.state["pending"] is None
    changes = delivery.report["route_changes"][1:]
    assert [change["weights"]["green"] for change in changes] == expected
    for route in cluster.routes:
        backend, frontend = route["spec"]["http"]
        assert [d["weight"] for d in backend["route"]] == [
            d["weight"] for d in frontend["route"]
        ]


@pytest.mark.parametrize(
    "mode,stage", [("blue-green", "blue-green-100"), ("canary", "canary-50")]
)
def test_http_failure_returns_all_traffic_to_the_baseline_and_fails_the_run(
    mode, stage
):
    cluster = MemoryCluster()
    delivery = controller(cluster, stage)
    with pytest.raises(Rejected, match="503"):
        delivery.deploy(mode, {"sha": "bad"}, inputs())
    assert cluster.state["active"] == "blue" and cluster.state["pending"] is None
    # The prior inactive slot was overwritten, so it is no longer a valid rollback target.
    assert cluster.state["previous"] is None
    assert delivery.report["rollback"] == "passed"
    assert delivery.report["route_changes"][-1]["weights"] == {"blue": 100}


def test_failed_state_commit_rolls_back_to_the_original_active_slot():
    cluster = MemoryCluster()
    cluster.fail_commit = True
    delivery = controller(cluster)
    with pytest.raises(RuntimeError, match="Commit connection lost"):
        delivery.deploy("blue-green", {"sha": "new"}, inputs())
    assert cluster.state["active"] == "blue"
    assert delivery.report["route_changes"][-1]["weights"] == {"blue": 100}


def test_pending_transaction_blocks_new_delivery_and_manual_rollback_recovers_it():
    cluster = MemoryCluster()
    cluster.state["pending"] = {"slot": "green"}
    delivery = controller(cluster)
    with pytest.raises(Rejected, match="Interrupted delivery"):
        delivery.deploy("blue-green", {"sha": "new"}, inputs())
    delivery.rollback()
    assert cluster.state["active"] == "blue" and cluster.state["pending"] is None
    assert delivery.report["status"] == "rolled-back"


def test_operator_rollback_switches_both_components_to_previous_release():
    cluster = MemoryCluster()
    delivery = controller(cluster)
    delivery.rollback()
    assert cluster.state["active"] == "green"
    assert cluster.state["previous"] == "blue"


def test_rollback_commit_failure_keeps_state_and_gateway_on_the_same_baseline():
    cluster = MemoryCluster()
    cluster.fail_commit = True
    delivery = controller(cluster)
    with pytest.raises(RuntimeError, match="Commit connection lost"):
        delivery.rollback()
    assert cluster.state["active"] == "blue"
    assert delivery.report["route_changes"][-1]["weights"] == {"blue": 100}


def test_slot_configuration_is_isolated_from_the_active_slot():
    original = inputs()
    result = slot_resources(original, "green", {"sha": "candidate"})
    assert original == inputs()
    frontend = next(
        r for r in result if r["metadata"]["name"] == "frontend-config-green"
    )
    assert frontend["data"]["API_BASE_URL"] == "http://backend-green:8000"
    for deployment in [r for r in result if r["kind"] == "Deployment"]:
        pod = deployment["spec"]["template"]
        assert pod["metadata"]["labels"]["slot"] == "green"
        assert pod["metadata"]["labels"]["sidecar.istio.io/inject"] == "false"
        assert "sidecar.istio.io/inject" not in pod["metadata"]["annotations"]
        assert pod["spec"]["containers"][0]["envFrom"][0]["configMapRef"][
            "name"
        ].endswith("-green")


@pytest.mark.parametrize(
    "failure", ["p95", "missing-candidate", "wrong-release", "http"]
)
def test_release_gate_rejects_bad_metrics_or_routing(failure):
    probe = Probe("credit.example.org", count=30, interval=0)

    def request(path, stage):
        if failure == "http":
            raise Rejected("HTTP 503")
        headers = {
            "x-mlops-route": "generation",
            "x-mlops-slot": "blue",
            "x-mlops-release": "wrong" if failure == "wrong-release" else "good",
        }
        return (2.0 if failure == "p95" else 0.001), headers

    probe.request = request
    weights = (
        {"blue": 100} if failure != "missing-candidate" else {"blue": 90, "green": 10}
    )
    with pytest.raises(Rejected):
        probe.measure(
            "gate",
            weights,
            {"blue": {"sha": "good"}, "green": {"sha": "candidate"}},
            "generation",
        )


def test_https_and_finite_prediction_contract_are_enforced(monkeypatch):
    with pytest.raises(ValueError, match="HTTPS"):
        Probe("credit.example.org", base_url="http://credit.example.org")

    class Response:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self):
            return json.dumps(
                {"prediction": 1, "default_probability": float("nan")}
            ).encode()

    monkeypatch.setattr(
        "scripts.ci.istio_delivery.urlopen", lambda *a, **kw: Response()
    )
    with pytest.raises(Rejected, match="prediction contract"):
        Probe("credit.example.org").request("/predict", "gate")


def test_istio_manifests_render_without_rolling_workloads_or_traefik_routes():
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        pytest.skip("kubectl required")
    path = Path(__file__).resolve().parents[1] / "infrastructure/kubernetes/istio"
    items = list(
        yaml.safe_load_all(
            subprocess.check_output([kubectl, "kustomize", str(path)], text=True)
        )
    )
    assert not any(i["kind"] in {"Deployment", "Ingress", "ConfigMap"} for i in items)
    assert sum(i["kind"] == "DestinationRule" for i in items) == 2
    assert any(i["kind"] == "Gateway" for i in items)
    for rule in [i for i in items if i["kind"] == "DestinationRule"]:
        assert {s["name"] for s in rule["spec"]["subsets"]} == {"blue", "green"}


def test_incomplete_or_negative_weight_distributions_are_rejected():
    for weights in [{"blue": 90}, {"blue": 110, "green": -10}]:
        with pytest.raises(ValueError):
            virtual_service("credit.example.org", weights, {})
