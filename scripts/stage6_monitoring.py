"""Attach only this project's running Prometheus to the local Seldon network."""

import json
from pathlib import Path
import subprocess


def main(network="scoring-stage6_default"):
    output = subprocess.check_output(
        [
            "docker",
            "ps",
            "--filter",
            "label=com.docker.compose.service=prometheus",
            "--format",
            "{{.ID}}",
        ],
        text=True,
    )
    attached = 0
    for container in output.split():
        info = json.loads(
            subprocess.check_output(["docker", "inspect", container], text=True)
        )[0]
        project = info["Config"]["Labels"].get("com.docker.compose.project")
        if project not in {Path.cwd().name, "scoring-observability-test"}:
            continue
        if network not in info["NetworkSettings"]["Networks"]:
            subprocess.run(
                ["docker", "network", "connect", network, container],
                check=True,
            )
        subprocess.run(["docker", "kill", "--signal", "HUP", container], check=True)
        attached += 1
    if not attached:
        raise SystemExit(
            "No project Prometheus running. Start make monitoring or monitoring-experiment first."
        )
    print(
        "Metrics connected to Prometheus; Grafana loads dashboards automatically."
    )


if __name__ == "__main__":
    main()
