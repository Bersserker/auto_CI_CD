"""Trigger a real Airflow run and wait for its complete immutable candidate handoff."""

import json
from pathlib import Path
import subprocess
import time

COMPOSE = [
    "docker",
    "compose",
    "-p",
    "scoring-stage7",
    "-f",
    "docker-compose.stage7.yml",
]


def main():
    run = "demo-" + str(time.time_ns())
    subprocess.run(
        [
            *COMPOSE,
            "exec",
            "-T",
            "airflow",
            "airflow",
            "dags",
            "trigger",
            "credit_retraining",
            "--run-id",
            run,
            "--conf",
            json.dumps({"run_id": run, "demo": True, "trigger": "schedule"}),
        ],
        check=True,
    )
    folder = Path("artifacts/stage7/runs") / run
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if (folder / "delivery.json").exists():
            result = json.loads((folder / "delivery.json").read_text())
            print(json.dumps(result, indent=2))
            Path("artifacts/stage7/last-run.txt").write_text(run + "\n")
            return
        status = folder / "validate-status.json"
        if status.exists():
            state = json.loads(status.read_text())
            if state["state"] == "success" and not state["result"]["passed"]:
                raise RuntimeError("Candidate rejected: " + json.dumps(state["result"]))
        for stage in ("prepare", "train", "validate", "publish"):
            path = folder / (stage + "-status.json")
            if path.exists() and json.loads(path.read_text())["state"] == "failed":
                raise RuntimeError(path.read_text())
        time.sleep(3)
    raise TimeoutError(
        "Airflow run did not produce a candidate; inspect Airflow UI/logs"
    )


if __name__ == "__main__":
    main()
