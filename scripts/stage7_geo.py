"""Deploy stateless replicas in two verified distinct regions; emit external edge config."""

import argparse
import json
from pathlib import Path
import re
import subprocess


def regions(context):
    nodes = json.loads(
        subprocess.check_output(
            ["kubectl", "--context", context, "get", "nodes", "-o", "json"], text=True
        )
    )
    values = {
        n["metadata"]["labels"].get("topology.kubernetes.io/region")
        for n in nodes["items"]
    }
    if not values or None in values:
        raise ValueError("Every node must have a real region label")
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context-a", required=True)
    parser.add_argument("--context-b", required=True)
    parser.add_argument(
        "--manifest-dir",
        type=Path,
        required=True,
        help="Fully rendered immutable ONNX release + HPA/Redis",
    )
    parser.add_argument("--host-a", required=True)
    parser.add_argument("--host-b", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.context_a == args.context_b or regions(args.context_a) & regions(
        args.context_b
    ):
        raise ValueError(
            "Two independent geographic regions are required, not two YC availability zones"
        )
    for host in (args.host_a, args.host_b):
        if not re.fullmatch("[a-z0-9.-]+", host) or "." not in host:
            raise ValueError("Invalid HTTPS hostname")
    if args.host_a == args.host_b:
        raise ValueError("Regional endpoints must differ")
    if not args.manifest_dir.is_dir():
        raise ValueError("Manifest directory required")
    for context in (args.context_a, args.context_b):
        subprocess.run(
            [
                "kubectl",
                "--context",
                context,
                "apply",
                "--dry-run=server",
                "-f",
                str(args.manifest_dir),
            ],
            check=True,
        )
    if args.apply:
        for context in (args.context_a, args.context_b):
            subprocess.run(
                [
                    "kubectl",
                    "--context",
                    context,
                    "apply",
                    "-f",
                    str(args.manifest_dir),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "kubectl",
                    "--context",
                    context,
                    "-n",
                    "mlops",
                    "rollout",
                    "status",
                    "deployment/backend",
                    "--timeout=10m",
                ],
                check=True,
            )
    output = Path("artifacts/stage7/geo-edge.cfg")
    output.parent.mkdir(parents=True, exist_ok=True)
    config = """defaults
  mode http
  timeout connect 2s
  timeout client 30s
  timeout server 30s
frontend global_edge
  bind *:8000
  use_backend region_b if { nbsrv(region_a) eq 0 }
  default_backend region_a
"""
    for name, host in [("region_a", args.host_a), ("region_b", args.host_b)]:
        config += f"""backend {name}
  option httpchk
  http-check send meth GET uri /ready ver HTTP/1.1 hdr Host {host}
  http-check expect status 200
  http-request set-header Host {host}
  server origin {host}:443 ssl verify required ca-file /etc/ssl/certs/ca-certificates.crt sni str({host}) check check-sni {host}
"""
    output.write_text(config)
    print(
        f"Edge config: {output}; run edge outside both failure domains and terminate client TLS there."
    )


if __name__ == "__main__":
    main()
