#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p artifacts/stage6/tools
cli=artifacts/stage6/tools/seldon
if [[ ! -x "$cli" ]]; then
  curl -fL --retry 3 https://github.com/SeldonIO/seldon-core/releases/download/v2.10.0/seldon-linux-amd64 -o "$cli.download"
  printf "%s  %s\n" ea111f7824d7ae4bb18533d71dee0e081e4bc70e63bc0efbfe4c05771ad09b06 "$cli.download" | sha256sum --check
  chmod +x "$cli.download"
  mv "$cli.download" "$cli"
fi
printf "%s  %s\n" ea111f7824d7ae4bb18533d71dee0e081e4bc70e63bc0efbfe4c05771ad09b06 "$cli" | sha256sum --check
uv run --group monitoring python -m src.model_management.prepare
docker image inspect credit-backend:observability >/dev/null 2>&1 || docker build -t credit-backend:observability .
LOCAL_UID=$(id -u) LOCAL_GID=$(id -g) docker compose -p scoring-stage6 -f docker-compose.stage6.yml up -d --build
for model in credit-fp32 credit-int8; do
  ready=0
  for attempt in $(seq 1 30); do
    if "$cli" model load --force --scheduler-host localhost:19004 -f "infrastructure/seldon/$model.yaml"; then ready=1; break; fi
    sleep 2
  done
  [[ "$ready" == 1 ]] || exit 1
  "$cli" model status --scheduler-host localhost:19004 "$model" -w ModelAvailable -t 180
done
printf 'Seldon gateway: http://localhost:18080/docs\nEvidently reports: http://localhost:18081/\n'
