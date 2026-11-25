"""Stable A/B assignment and bounded asynchronous shadowing through Seldon Core 2."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from time import perf_counter
from urllib.request import Request, urlopen
import uuid

from fastapi import FastAPI, Header, HTTPException, Query
import joblib
import numpy as np
import pandas as pd
from prometheus_client import Counter, Histogram, make_asgi_app

from src.api.models import CreditRequest
from src.api.onnx_model import FEATURE_NAMES
from src.config import BEST_NN_SCALER_PATH
from src.features.build_features import build_features

VERSIONS = ("credit-fp32", "credit-int8")
ROUTED = Counter(
    "scoring_routed_predictions_total", "Seldon responses", ["mode", "model"]
)
ERRORS = Counter(
    "scoring_seldon_errors_total", "Primary Seldon failures", ["mode", "model"]
)
SHADOW_ERRORS = Counter(
    "scoring_shadow_errors_total", "Shadow failures, isolated from primary"
)
DELTA = Histogram(
    "scoring_shadow_probability_delta",
    "Absolute paired probability difference",
    buckets=(0.0001, 0.001, 0.005, 0.01, 0.05, 0.1, 1),
)
LATENCY = Histogram("scoring_seldon_duration_seconds", "Seldon HTTP latency", ["model"])
POOL = ThreadPoolExecutor(max_workers=2)
SLOTS = threading.BoundedSemaphore(32)
LOCK = threading.Lock()


@lru_cache(maxsize=1)
def get_scaler():
    return joblib.load(BEST_NN_SCALER_PATH)


app = FastAPI(title="Seldon scoring A/B and shadow gateway")
app.mount("/metrics", make_asgi_app())


def assignment(request_id, candidate_percent=50):
    if not 0 <= candidate_percent <= 100:
        raise ValueError("candidate_percent must be in [0,100]")
    bucket = int(hashlib.sha256(request_id.encode()).hexdigest()[:16], 16) % 10000
    return VERSIONS[int(bucket < candidate_percent * 100)]


def record(row):
    path = Path(os.getenv("JOURNAL_PATH", "artifacts/stage6/records/predictions.jsonl"))
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict(timestamp=datetime.now(timezone.utc).isoformat(), **row)
    with LOCK, path.open("a") as stream:
        stream.write(json.dumps(row, allow_nan=False) + "\n")


def infer(model, values):
    body = {
        "inputs": [
            {
                "name": "features",
                "shape": [1, len(FEATURE_NAMES)],
                "datatype": "FP32",
                "data": values.tolist(),
            }
        ]
    }
    request = Request(
        os.getenv("SELDON_URL", "http://localhost:19000") + f"/v2/models/{model}/infer",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "seldon-model": model},
    )
    start = perf_counter()
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
        probability = float(np.asarray(payload["outputs"][0]["data"]).reshape(-1)[0])
        if not np.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("Invalid model output")
        return probability
    finally:
        LATENCY.labels(model).observe(perf_counter() - start)


def shadow(request_id, values, primary):
    try:
        probability = infer(VERSIONS[1], values)
        DELTA.observe(abs(probability - primary))
        record(
            dict(
                request_id=request_id,
                mode="shadow",
                model=VERSIONS[1],
                probability=probability,
                primary_probability=primary,
                delta=abs(probability - primary),
                success=True,
            )
        )
    except Exception:
        SHADOW_ERRORS.inc()
        record(
            dict(request_id=request_id, mode="shadow", model=VERSIONS[1], success=False)
        )
    finally:
        SLOTS.release()


@app.get("/live")
def live():
    return {"status": "alive"}


@app.post("/predict")
def predict(
    request: CreditRequest,
    mode: str = Query("shadow", pattern="^(ab|shadow)$"),
    x_request_id: str | None = Header(default=None),
):
    request_id = x_request_id or str(uuid.uuid4())
    if not re.fullmatch("[A-Za-z0-9_-]{1,80}", request_id):
        raise HTTPException(
            422, "Use an opaque X-Request-ID (letters, digits, underscore, hyphen)"
        )
    features = build_features(pd.DataFrame([request.model_dump()]))[FEATURE_NAMES]
    values = (
        get_scaler().transform(features.to_numpy(dtype=np.float32)).astype(np.float32)
    )
    if not np.isfinite(values).all():
        raise HTTPException(422, "Features cannot be scaled to finite values")
    model = assignment(request_id) if mode == "ab" else VERSIONS[0]
    try:
        probability = infer(model, values)
    except Exception as exc:
        ERRORS.labels(mode, model).inc()
        record(dict(request_id=request_id, mode=mode, model=model, success=False))
        raise HTTPException(503, "Seldon model unavailable") from exc
    record(
        dict(
            request_id=request_id,
            mode=mode,
            model=model,
            probability=probability,
            success=True,
        )
    )
    ROUTED.labels(mode, model).inc()
    if mode == "shadow":
        if SLOTS.acquire(blocking=False):
            POOL.submit(shadow, request_id, values, probability)
        else:
            SHADOW_ERRORS.inc()
            record(
                dict(
                    request_id=request_id,
                    mode="shadow",
                    model=VERSIONS[1],
                    success=False,
                    reason="queue_full",
                )
            )
    return dict(
        request_id=request_id,
        model_version=model,
        prediction=int(probability >= 0.5),
        default_probability=probability,
    )
