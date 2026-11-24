#!/usr/bin/env bash
set -euo pipefail
# Official 1.32.0 release, verified against its published SHA256.
workspace=$(mktemp -d)
trap 'rm -rf "$workspace"' EXIT
curl -fsSL --retry 3 -o "$workspace/istio.tar.gz" https://github.com/istio/istio/releases/download/1.32.0/istio-1.32.0-linux-amd64.tar.gz
printf '%s  %s\n' b9cc0d427e2742b63ade28e15332a4b5da37ebf2d777a25ba22cc91bbfd51fbf "$workspace/istio.tar.gz" | sha256sum --check
tar -xzf "$workspace/istio.tar.gz" -C "$workspace" istio-1.32.0/bin/istioctl
mkdir -p "$HOME/.local/bin"
install -m 0755 "$workspace/istio-1.32.0/bin/istioctl" "$HOME/.local/bin/istioctl"
if [[ -n ${GITHUB_PATH:-} ]]; then echo "$HOME/.local/bin" >> "$GITHUB_PATH"; fi
