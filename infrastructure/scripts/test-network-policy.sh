#!/usr/bin/env bash
# Traffic test in a disposable namespace on the active cluster; no app changes.
set -euo pipefail
probe_namespace="network-policy-probe-$(date +%s)-${RANDOM}"
probe_image=${PROBE_IMAGE:-python:3.11-alpine}
echo "Testing NetworkPolicy in $(kubectl config current-context), namespace $probe_namespace"
kubectl create namespace "$probe_namespace"
trap 'kubectl delete namespace "$probe_namespace" --wait=false >/dev/null' EXIT
kubectl -n "$probe_namespace" run server --image="$probe_image" --labels=app=probe --command -- python -m http.server 8000
kubectl -n "$probe_namespace" run allowed --image="$probe_image" --labels=access=allowed --command -- sleep 600
kubectl -n "$probe_namespace" run blocked --image="$probe_image" --command -- sleep 600
kubectl -n "$probe_namespace" wait --for=condition=Ready pod --all --timeout=5m
kubectl -n "$probe_namespace" apply -f - <<'YAML'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: probe-allow-selected-client
spec:
  podSelector:
    matchLabels:
      app: probe
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector:
            matchLabels:
              access: allowed
      ports:
        - protocol: TCP
          port: 8000
YAML
probe_ip=$(kubectl -n "$probe_namespace" get pod server -o jsonpath='{.status.podIP}')
# Allow propagation time by retrying both the positive and negative assertions.
for attempt in {1..15}; do
  if kubectl -n "$probe_namespace" exec allowed -- python -c \
    'import sys, urllib.request; assert urllib.request.urlopen("http://"+sys.argv[1]+":8000", timeout=3).status == 200' "$probe_ip" \
    && kubectl -n "$probe_namespace" exec blocked -- python -c '
import socket, sys
try:
    socket.create_connection((sys.argv[1], 8000), timeout=3).close()
except (TimeoutError, ConnectionRefusedError):
    sys.exit(0)
except OSError as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
else:
    sys.exit(1)
' "$probe_ip"; then
    echo 'PASS: selected client connects; other client is blocked'
    exit 0
  fi
  sleep 2
done
echo 'NetworkPolicy traffic enforcement failed' >&2
exit 1
