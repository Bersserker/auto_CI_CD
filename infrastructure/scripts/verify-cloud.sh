#!/usr/bin/env bash
# Run after apply, kubeconfig setup and policy installation. Fails on missing evidence.
set -euo pipefail
umask 077
infra_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
environment=${1:?Usage: verify-cloud.sh staging|production}
case "$environment" in staging|production) ;; *) exit 2 ;; esac
env_dir="$infra_root/environments/$environment"
python3 - "$env_dir/.terraform/terraform.tfstate" "$environment" <<'PYBACKEND'
import json, sys
with open(sys.argv[1]) as f:
    backend = json.load(f)['backend']
assert backend['type'] == 's3', 'Remote S3 backend is required'
assert backend['config']['bucket'], 'State bucket must be configured'
assert backend['config']['key'] == sys.argv[2] + '/terraform.tfstate', 'Incorrect environment state key'
PYBACKEND
evidence_dir="$infra_root/.evidence/$environment"
mkdir -p "$evidence_dir"
terraform -chdir="$env_dir" output -json > "$evidence_dir/outputs.json"
cluster_id=$(terraform -chdir="$env_dir" output -raw kubernetes_cluster_id)
yc managed-kubernetes cluster get --id "$cluster_id" --format json > "$evidence_dir/cluster.json"
# Verify the active kubeconfig endpoint belongs to this Terraform cluster.
server=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
internal=$(terraform -chdir="$env_dir" output -raw kubernetes_internal_endpoint)
external=$(terraform -chdir="$env_dir" output -raw kubernetes_external_endpoint)
if [[ "$server" != "$internal" && "$server" != "$external" ]]; then
  echo 'kubectl context does not match this environment' >&2
  exit 1
fi
kubectl wait --for=condition=Ready nodes --all --timeout=10m
kubectl get nodes -o json > "$evidence_dir/nodes.json"
kubectl -n mlops get networkpolicy -o json > "$evidence_dir/policies.json"
python3 - "$evidence_dir" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
cluster = json.loads((root / 'cluster.json').read_text())
assert cluster['status'] == 'RUNNING', cluster.get('status')
assert cluster['health'] == 'HEALTHY', cluster.get('health')
nodes = json.loads((root / 'nodes.json').read_text())['items']
assert nodes, 'No Kubernetes nodes'
assert all(any(c['type']=='Ready' and c['status']=='True' for c in n['status']['conditions']) for n in nodes)
assert any(n['metadata']['labels'].get('workload') == 'cpu' for n in nodes), 'No CPU nodes'
assert any(int(n['status']['allocatable'].get('nvidia.com/gpu', '0')) > 0 for n in nodes), 'No allocatable GPU'
policies = json.loads((root / 'policies.json').read_text())['items']
assert {'default-deny', 'allow-dns', 'allow-api'} <= {p['metadata']['name'] for p in policies}
print('Cloud cluster, CPU/GPU nodes and policy presence verified')
PY
# 0 = no changes, 2 = changes remain; both errors and non-idempotence fail this script.
terraform -chdir="$env_dir" plan -input=false -detailed-exitcode -out="$evidence_dir/recheck.tfplan" > "$evidence_dir/plan.txt"
echo "Evidence saved in $evidence_dir. Run test-network-policy.sh for traffic enforcement."
