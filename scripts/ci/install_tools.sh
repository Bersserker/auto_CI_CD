#!/usr/bin/env bash
set -euo pipefail
# Versions match the locally verified stage 3 cluster.
tools_dir=$(mktemp -d)
trap 'rm -rf "$tools_dir"' EXIT
curl -fsSL --retry 3 -o "$tools_dir/kubectl" https://dl.k8s.io/release/v1.35.0/bin/linux/amd64/kubectl
curl -fsSL --retry 3 -o "$tools_dir/kubectl.sha256" https://dl.k8s.io/release/v1.35.0/bin/linux/amd64/kubectl.sha256
printf '%s  %s\n' "$(cat "$tools_dir/kubectl.sha256")" "$tools_dir/kubectl" | sha256sum --check
curl -fsSL --retry 3 -o "$tools_dir/kind" https://kind.sigs.k8s.io/dl/v0.33.0/kind-linux-amd64
curl -fsSL --retry 3 -o "$tools_dir/kind.sha256sum" https://kind.sigs.k8s.io/dl/v0.33.0/kind-linux-amd64.sha256sum
(cd "$tools_dir" && sed 's/kind-linux-amd64/kind/' kind.sha256sum | sha256sum --check)
mkdir -p "$HOME/.local/bin"
install -m 0755 "$tools_dir/kubectl" "$tools_dir/kind" "$HOME/.local/bin/"
if [[ -n ${GITHUB_PATH:-} ]]; then echo "$HOME/.local/bin" >> "$GITHUB_PATH"; fi
