"""Local trusted worker for Airflow; inputs are IDs, not commands or arbitrary paths."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict

from src.retraining import pipeline

app = FastAPI(title="Retraining worker")
POOL = ThreadPoolExecutor(max_workers=1)
LOCK = threading.Lock()
JOBS = {}


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    stage: Literal["prepare", "train", "validate", "publish"]
    demo: bool = False
    trigger: Literal["schedule", "new_data", "drift"] = "schedule"
    batch: str | None = None
    event_id: str | None = None
    as_of: str | None = None


def execute(job, key):
    path = pipeline.run_dir(job.run_id) / (job.stage + "-status.json")
    try:
        result = (
            pipeline.prepare(job.run_id, job.model_dump())
            if job.stage == "prepare"
            else getattr(pipeline, job.stage)(job.run_id)
        )
        status = {"state": "success", "result": result}
    except Exception as exc:
        status = {"state": "failed", "error": str(exc)[:500]}
    pipeline.write(path, status)
    with LOCK:
        JOBS[key] = status


@app.get("/live")
def live():
    return {"status": "alive"}


@app.post("/jobs")
def submit(job: Job):
    if job.demo and os.getenv("ALLOW_SIMULATION") != "1":
        raise HTTPException(422, "Simulation is disabled")
    folder = pipeline.run_dir(job.run_id)
    key = job.run_id + "-" + job.stage
    with LOCK:
        status_path = folder / (job.stage + "-status.json")
        if status_path.exists():
            status = json.loads(status_path.read_text())
            if status["state"] == "success":
                JOBS[key] = status
                return {"job_id": key}
        if key in JOBS and JOBS[key]["state"] == "running":
            return {"job_id": key}
        if len(JOBS) > 512:
            raise HTTPException(429, "Worker needs maintenance; job history full")
        JOBS[key] = {"state": "running"}
        POOL.submit(execute, job, key)
    return {"job_id": key}


@app.get("/jobs/{key}")
def status(key: str):
    with LOCK:
        if key not in JOBS:
            raise HTTPException(404, "Unknown job")
        return JOBS[key]


@app.post("/events")
def events():
    root = pipeline.ROOT
    root.mkdir(parents=True, exist_ok=True)
    with LOCK:
        path = root / "event-claims.json"
        claims = json.loads(path.read_text()) if path.exists() else {}
        found = []
        candidates = [("new_data", p) for p in sorted((root / "inbox").glob("*.csv"))]
        for scenario in ("concept-drift", "production"):
            report = Path("artifacts/stage6/drift") / scenario / "summary.json"
            if report.exists():
                signal = json.loads(report.read_text())
                if signal.get("simulated") and os.getenv("ALLOW_SIMULATION") != "1":
                    continue
                if any(
                    signal.get(key)
                    for key in (
                        "data_drift",
                        "performance_decay",
                        "concept_drift_suspected",
                    )
                ):
                    candidates.append(("drift", report))
        for trigger, source in candidates:
            event = hashlib.sha256(
                (trigger + pipeline.sha(source)).encode()
            ).hexdigest()[:24]
            run = "event-" + event
            folder = root / "runs" / run
            terminal = folder / "validate-status.json"
            if (folder / "delivery.json").exists() or terminal.exists():
                continue
            if claims.get(event, 0) > time.time():
                continue
            claims[event] = time.time() + 1800
            conf = {
                "event_id": event,
                "run_id": run,
                "trigger": trigger,
                "demo": (
                    source.name == "demo.csv"
                    if trigger == "new_data"
                    else bool(json.loads(source.read_text()).get("simulated"))
                ),
            }
            if trigger == "new_data":
                conf["batch"] = source.name
            found.append(conf)
        pipeline.write(path, claims)
        return found


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    summaries = []
    for path in (pipeline.ROOT / "runs").glob("*/gate.json"):
        try:
            summaries.append((path.stat().st_mtime, json.loads(path.read_text())))
        except (OSError, ValueError):
            continue
    text = f'scoring_retraining_completed_total{{result="passed"}} {sum(bool(r[1]["passed"]) for r in summaries)}\n'
    text += f'scoring_retraining_completed_total{{result="rejected"}} {sum(not r[1]["passed"] for r in summaries)}\n'
    if summaries:
        stamp, result = max(summaries, key=lambda row: row[0])
        for name, value in [
            ("last_timestamp_seconds", stamp),
            ("gate_passed", int(result["passed"])),
            ("candidate_auc", result["candidate_auc"]),
            ("baseline_auc", result["baseline_auc"]),
        ]:
            text += f"scoring_retraining_{name} {value}\n"
    return text
