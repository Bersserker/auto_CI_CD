#!/usr/bin/env bash
set -euo pipefail
infra_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
environment=${1:?Usage: configure-kubectl.sh staging|production [--external]}
case "$environment" in staging|production) ;; *) exit 2 ;; esac
endpoint_flag=${2:---internal}
case "$endpoint_flag" in --internal|--external) ;; *) exit 2 ;; esac
cluster_id=$(terraform -chdir="$infra_root/environments/$environment" output -raw kubernetes_cluster_id)
# The private endpoint requires an existing VPN/bastion route to the VPC.
yc managed-kubernetes cluster get-credentials --id "$cluster_id" "$endpoint_flag" --force
kubectl cluster-info
