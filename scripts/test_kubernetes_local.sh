#!/usr/bin/env bash
# Полная локальная проверка этапа 3. Удаляет только созданный ею тестовый кластер.
set -euo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_root"
for executable in kind kubectl docker curl rg; do command -v "$executable" >/dev/null; done
python_bin=${PYTHON_BIN:-"$project_root/.venv/bin/python"}
workspace=$(mktemp -d /tmp/mlops-stage3.XXXXXX)
cluster_name="mlops-stage3-$$"
evidence_dir="$project_root/infrastructure/.evidence/stage3"
mkdir -p "$evidence_dir"
export KUBECONFIG="$workspace/kubeconfig"
context="kind-$cluster_name"
created=0
cleanup() {
  status=$?
  trap - EXIT
  set +e
  if [[ "$created" == 1 ]]; then
    kubectl --context "$context" get pods -A -o wide > "$evidence_dir/pods.txt" 2>&1
    kubectl --context "$context" -n mlops get events --sort-by=.lastTimestamp > "$evidence_dir/events.txt" 2>&1
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
"$python_bin" -m scripts.prepare_local_model --directory "$workspace/fixture"
docker build -t credit-backend:dvc .
docker build -f src/frontend/Dockerfile -t credit-frontend:local .
# Загружаем внешние образы через Docker хоста; узлам не нужен повторный pull.
system_images=(quay.io/calico/calico:v3.33.0 quay.io/calico/third-party-cni-plugins:v3.33.0 quay.io/calico/node:v3.33.0 traefik:v3.6.2)
for image in "${system_images[@]}"; do
  pulled=0
  for attempt in 1 2 3; do
    if docker pull --platform linux/amd64 "$image"; then pulled=1; break; fi
    sleep 2
  done
  [[ "$pulled" == 1 ]] || exit 1
done
# Явная платформа исключает отсутствующие blobs других архитектур в OCI index.
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
# Без --wait: до установки CNI узлы ожидаемо NotReady.
# Не меняем kubeconfig пользователя: все credentials остаются в workspace.
created=1
kind create cluster --name "$cluster_name" --kubeconfig "$KUBECONFIG" --config "$workspace/kind.yaml" \
  --image kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f
kind load image-archive "$workspace/system-images.tar" --name "$cluster_name"
curl -fLsS https://raw.githubusercontent.com/projectcalico/calico/v3.33.0/manifests/calico.yaml -o "$workspace/calico.yaml"
kubectl --context "$context" apply --server-side -f "$workspace/calico.yaml"
kind load docker-image credit-backend:dvc credit-frontend:local --name "$cluster_name"
kubectl --context "$context" wait --for=condition=Ready nodes --all --timeout=10m
kubectl --context "$context" -n kube-system rollout status daemonset/calico-node --timeout=10m
kubectl --context "$context" apply -f infrastructure/kubernetes/local/traefik.yaml
kubectl --context "$context" -n ingress-system rollout status deployment/traefik --timeout=5m
kubectl --context "$context" create namespace mlops
kubectl --context "$context" -n mlops create configmap local-dvc-lock \
  --from-file=dvc.yaml="$workspace/fixture/publisher/dvc.yaml" \
  --from-file=dvc.lock="$workspace/fixture/publisher/dvc.lock"
AWS_ACCESS_KEY_ID=local-test-only AWS_SECRET_ACCESS_KEY=local-test-only \
  "$python_bin" scripts/create_kubernetes_secret.py --context "$context"
bash scripts/deploy_kubernetes.sh local "$context"
"$python_bin" -m scripts.verify_kubernetes --context "$context" \
  --request-file "$workspace/fixture/request.json" --check-rollout --check-failed-rollout \
  --evidence "$evidence_dir/result.json"
# Проверка реального действия NetworkPolicy в отдельном временном namespace.
kubectl config use-context "$context" >/dev/null
PROBE_IMAGE=credit-frontend:local bash infrastructure/scripts/test-network-policy.sh
printf 'Local Kubernetes test passed; evidence: %s\n' "$evidence_dir"
