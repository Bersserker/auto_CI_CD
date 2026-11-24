#!/usr/bin/env bash
# Явный context обязателен. Секрет создается отдельно до развертывания.
set -euo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
environment=${1:?Usage: deploy_kubernetes.sh local|staging|production CONTEXT}
kube_context=${2:?Specify kubectl context}
case "$environment" in local|staging|production) ;; *) exit 2 ;; esac
rendered=$(mktemp)
trap 'rm -f "$rendered"' EXIT
kubectl kustomize "$project_root/infrastructure/kubernetes/overlays/$environment" > "$rendered"
if rg -q 'REPLACE_' "$rendered"; then
  echo 'Заполните image registry/tag, имя бакета и домен в overlay перед deploy.' >&2
  exit 1
fi
kubectl --context "$kube_context" get ingressclass traefik >/dev/null
kubectl --context "$kube_context" -n mlops get secret s3-credentials >/dev/null
if [[ "$environment" != local ]]; then
  tls_type=$(kubectl --context "$kube_context" -n mlops get secret credit-tls -o jsonpath='{.type}')
  [[ "$tls_type" == kubernetes.io/tls ]] || { echo 'Требуется TLS Secret credit-tls.' >&2; exit 1; }
fi
kubectl --context "$kube_context" apply --dry-run=server -f "$rendered" >/dev/null
kubectl --context "$kube_context" apply -f "$rendered"
kubectl --context "$kube_context" -n mlops rollout status deployment/backend --timeout=15m
kubectl --context "$kube_context" -n mlops rollout status deployment/frontend --timeout=5m
kubectl --context "$kube_context" -n mlops get deployments,services,ingress
