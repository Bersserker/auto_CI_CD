#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p artifacts/stage7/inbox artifacts/stage7/runs
uv run python -c 'from pathlib import Path; import secrets; p=Path("artifacts/stage7/cache.key"); p.write_text(secrets.token_hex(32)) if not p.exists() else None'
docker image inspect credit-backend:observability >/dev/null 2>&1 || docker build -t credit-backend:observability .
docker build --build-arg "TRAINER_UID=$(id -u)" -f docker/stage7/Dockerfile -t credit-backend:stage7 .
LOCAL_UID=$(id -u) LOCAL_GID=$(id -g) docker compose -p scoring-stage7 -f docker-compose.stage7.yml up -d
trainer=$(docker compose -p scoring-stage7 -f docker-compose.stage7.yml ps -q trainer)
if ! docker inspect -f '{{json .NetworkSettings.Networks}}' "$trainer" | uv run python -c 'import sys,json;sys.exit(0 if "scoring-stage6_default" in json.load(sys.stdin) else 1)'; then
  docker network connect scoring-stage6_default "$trainer"
fi
for dag in credit_retraining credit_retraining_events; do
  for attempt in $(seq 1 40); do
    if docker compose -p scoring-stage7 -f docker-compose.stage7.yml exec -T airflow airflow dags unpause "$dag" >/dev/null 2>&1; then break; fi
    sleep 2
  done
done
printf 'Airflow: http://localhost:28080; inference: http://localhost:28000/docs\n'
