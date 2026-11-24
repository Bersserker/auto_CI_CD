#!/usr/bin/env python3
"""Static NetworkPolicy contract check; live enforcement needs test-network-policy.sh."""
from pathlib import Path
import yaml

manifest = Path(__file__).resolve().parents[1] / "kubernetes/base/network-policies.yaml"
documents = list(yaml.safe_load_all(manifest.read_text()))
assert documents[0]["kind"] == "Namespace" and documents[0]["metadata"]["name"] == "mlops"
policies = {d["metadata"]["name"]: d for d in documents[1:]}
assert len(policies) == 6
for policy in policies.values():
    assert policy["apiVersion"] == "networking.k8s.io/v1"
    assert policy["kind"] == "NetworkPolicy"
    assert policy["metadata"]["namespace"] == "mlops"
    spec = policy["spec"]
    for direction in ("ingress", "egress"):
        for rule in spec.get(direction, []):
            assert rule.get("ports"), "Rules must restrict ports"
            peers = rule.get("from", rule.get("to", []))
            assert peers and all(peers), "Unrestricted peers are forbidden"
default = policies["default-deny"]["spec"]
assert default["podSelector"] == {}
assert set(default["policyTypes"]) == {"Ingress", "Egress"}
assert not default.get("ingress") and not default.get("egress")
dns = policies["allow-dns"]["spec"]["egress"][0]
assert {(p["protocol"], p["port"]) for p in dns["ports"]} == {("TCP", 53), ("UDP", 53)}
assert dns["to"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "kube-system"
assert dns["to"][0]["podSelector"]["matchLabels"]["k8s-app"] == "kube-dns"
api = policies["allow-api"]["spec"]
assert api["podSelector"]["matchLabels"] == {"app": "backend"}
assert api["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8000}]
assert api["ingress"][0]["from"][0]["podSelector"]["matchLabels"] == {"app": "frontend"}
egress = policies["backend-https-egress"]["spec"]["egress"][0]
assert egress["ports"] == [{"protocol": "TCP", "port": 443}]
assert "169.254.0.0/16" in egress["to"][0]["ipBlock"]["except"]
print("NetworkPolicy static contracts: PASS (6 policies; runtime enforcement not tested)")
