#!/usr/bin/env bash
# Read-only checks plus mock plans; no cloud calls or credentials required.
set -euo pipefail
infra_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
terraform fmt -check -recursive "$infra_root"
for environment in staging production; do
  env_dir="$infra_root/environments/$environment"
  terraform -chdir="$env_dir" init -backend=false -input=false
  terraform -chdir="$env_dir" validate
  terraform -chdir="$env_dir" test
done
terraform -chdir="$infra_root/bootstrap" init -backend=false -input=false
terraform -chdir="$infra_root/bootstrap" validate
terraform -chdir="$infra_root/bootstrap" test
for module in kubernetes storage; do
  terraform -chdir="$infra_root/modules/$module" init -backend=false -input=false
  terraform -chdir="$infra_root/modules/$module" test
done

python3 "$infra_root/scripts/check-policies.py"
for script in "$infra_root"/scripts/*.sh "$infra_root/modules/compute/init.sh"; do
  bash -n "$script"
done
