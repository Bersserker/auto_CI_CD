#!/usr/bin/env bash
set -euo pipefail
infra_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
echo "Applying NetworkPolicy to current kubectl context: $(kubectl config current-context)"
kubectl apply -f "$infra_root/kubernetes/base/network-policies.yaml"
kubectl -n mlops get networkpolicy
