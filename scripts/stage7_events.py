"""Submit a clearly simulated new-data batch through the actual Airflow event DAG."""

import hashlib
import json
import subprocess
import time

import pandas as pd
from sklearn.model_selection import train_test_split

from scripts.stage7_autoscale import COMPOSE
from src.config import PROCESSED_DATA_PATH
from src.retraining.pipeline import ROOT, sha


def main():
    full = pd.read_csv(PROCESSED_DATA_PATH)
    train, _ = train_test_split(
        full, test_size=0.2, random_state=42, stratify=full.default
    )
    batch = (
        train.sample(n=200, random_state=71)
        .rename(columns={"default": "target"})
        .copy()
    )
    batch["id"] = range(1_000_000, 1_000_000 + len(batch))
    batch["predicted_at"] = "2026-08-01T00:00:00Z"
    batch["matured_at"] = "2026-09-01T00:00:00Z"
    batch["observed_at"] = "2026-09-02T00:00:00Z"
    path = ROOT / "inbox/demo.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    batch.to_csv(path, index=False)
    event = hashlib.sha256(("new_data" + sha(path)).encode()).hexdigest()[:24]
    run = "event-" + event
    subprocess.run(
        [
            *COMPOSE,
            "exec",
            "-T",
            "airflow",
            "airflow",
            "dags",
            "trigger",
            "credit_retraining_events",
            "--run-id",
            "events-" + str(time.time_ns()),
        ],
        check=True,
    )
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        folder = ROOT / "runs" / run
        if (folder / "delivery.json").exists():
            print("New-data event produced Seldon candidate:", run)
            return
        for stage in ("prepare", "train", "validate", "publish"):
            status = folder / (stage + "-status.json")
            if status.exists():
                data = json.loads(status.read_text())
                if data["state"] == "failed" or (
                    stage == "validate"
                    and not data.get("result", {}).get("passed", True)
                ):
                    raise RuntimeError(f"Event candidate rejected: {data}")
        time.sleep(3)
    raise TimeoutError("New-data event did not complete")


if __name__ == "__main__":
    main()
