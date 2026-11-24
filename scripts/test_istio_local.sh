#!/usr/bin/env bash
# Reproducible, isolated Istio delivery demonstration without Yandex credentials.
set -euo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_root"
for executable in kind kubectl docker curl istioctl openssl; do command -v "$executable" >/dev/null; done
python_bin=${PYTHON_BIN:-"$project_root/.venv/bin/python"}
workspace=$(mktemp -d /tmp/mlops-strategies.XXXXXX)
cluster_name="mlops-strategies-$$-${RANDOM}"
context="kind-$cluster_name"
evidence_dir="$project_root/infrastructure/.evidence/stage4"
mkdir -p "$evidence_dir"
for scenario in bootstrap blue-green blue-green-failed canary canary-failed unready-candidate before-manual-rollback manual-rollback result; do
  rm -f -- "$evidence_dir/$scenario.json"
done
rm -f -- "$evidence_dir/pods.txt" "$evidence_dir/events.txt" "$evidence_dir/routes.yaml" "$evidence_dir/analysis.txt"
export KUBECONFIG="$workspace/kubeconfig"
created=0
cleanup() {
  status=$?
  trap - EXIT
  set +e
  if [[ "$created" == 1 ]]; then
    kubectl --context "$context" get pods -A -o wide > "$evidence_dir/pods.txt" 2>&1
    kubectl --context "$context" -n mlops get events --sort-by=.lastTimestamp > "$evidence_dir/events.txt" 2>&1
    kubectl --context "$context" -n mlops get virtualservice credit-scoring -o yaml > "$evidence_dir/routes.yaml" 2>&1
    if [[ ${KEEP_CLUSTER:-0} == 1 ]]; then
      echo "Cluster retained: $cluster_name; KUBECONFIG=$KUBECONFIG; workspace=$workspace"
    else
      kind delete cluster --name "$cluster_name"
      rm -rf -- "$workspace"
    fi
  else
    rm -rf -- "$workspace"
  fi
  exit "$status"
}
trap cleanup EXIT
"$python_bin" -m scripts.prepare_local_model --directory "$workspace/fixture/v1"
"$python_bin" -m scripts.prepare_local_model --directory "$workspace/fixture/v2" --positive-samples 3
docker build -t credit-backend:dvc .
docker build -f src/frontend/Dockerfile -t credit-frontend:local .
cat > "$workspace/Dockerfile.demo" <<'DOCKER'
FROM credit-backend:dvc
ARG DEMO_VERSION
LABEL org.opencontainers.image.version=$DEMO_VERSION
COPY --chown=app:app dvc.yaml dvc.lock /app/
DOCKER
for version in v1 v2; do
  docker build -f "$workspace/Dockerfile.demo" --build-arg "DEMO_VERSION=$version" -t "credit-backend:stage4-$version" "$workspace/fixture/$version/publisher"
done
system_images=(quay.io/calico/calico:v3.33.0 quay.io/calico/third-party-cni-plugins:v3.33.0 quay.io/calico/node:v3.33.0 istio/pilot:1.32.0 istio/proxyv2:1.32.0)
for image in "${system_images[@]}"; do docker pull --platform linux/amd64 "$image"; done
docker image save --platform linux/amd64 --output "$workspace/system-images.tar" "${system_images[@]}"
cat > "$workspace/kind.yaml" <<YAML
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
networking:
  disableDefaultCNI: true
  podSubnet: 192.168.0.0/16
nodes:
  - role: control-plane
    extraMounts:
      - hostPath: $workspace/fixture
        containerPath: /mlops-fixture
        readOnly: true
  - role: worker
    labels:
      workload: cpu
    extraMounts:
      - hostPath: $workspace/fixture
        containerPath: /mlops-fixture
        readOnly: true
YAML
while IFS= read -r existing_cluster; do
  [[ "$existing_cluster" != "$cluster_name" ]] || { echo 'Test cluster name already exists.' >&2; exit 1; }
done < <(kind get clusters)
created=1
kind create cluster --name "$cluster_name" --kubeconfig "$KUBECONFIG" --config "$workspace/kind.yaml" \
  --image kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f
kind load image-archive "$workspace/system-images.tar" --name "$cluster_name"
curl -fLsS --retry 3 https://raw.githubusercontent.com/projectcalico/calico/v3.33.0/manifests/calico.yaml -o "$workspace/calico.yaml"
kubectl --context "$context" apply --server-side -f "$workspace/calico.yaml"
kind load docker-image credit-backend:stage4-v1 credit-backend:stage4-v2 credit-frontend:local --name "$cluster_name"
kubectl --context "$context" wait --for=condition=Ready nodes --all --timeout=10m
"$python_bin" - "$workspace/operator.yaml" <<'PY'
from pathlib import Path
import sys, yaml
operator = yaml.safe_load(Path('infrastructure/kubernetes/istio/operator.yaml').read_text())
operator['spec']['components']['ingressGateways'][0]['k8s']['service']['type'] = 'ClusterIP'
Path(sys.argv[1]).write_text(yaml.safe_dump(operator))
PY
istioctl --context "$context" install -f "$workspace/operator.yaml" -y --readiness-timeout=10m
kubectl --context "$context" -n istio-system rollout status deployment/istio-ingressgateway --timeout=5m
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=credit.local' \
  -addext 'subjectAltName=DNS:credit.local,IP:127.0.0.1' -keyout "$workspace/tls.key" -out "$workspace/tls.crt" >/dev/null 2>&1
kubectl --context "$context" -n istio-system create secret tls credit-tls --cert "$workspace/tls.crt" --key "$workspace/tls.key"
kubectl --context "$context" create namespace mlops
AWS_ACCESS_KEY_ID=local-test-only AWS_SECRET_ACCESS_KEY=local-test-only \
  "$python_bin" scripts/create_kubernetes_secret.py --context "$context"
"$python_bin" -m scripts.verify_istio --context "$context" --certificate "$workspace/tls.crt" --evidence-dir "$evidence_dir"
istioctl --context "$context" analyze -n mlops > "$evidence_dir/analysis.txt" 2>&1
printf 'Istio delivery demonstration passed; evidence: %s\n' "$evidence_dir"
