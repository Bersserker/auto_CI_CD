"""REST API для модели с помощью FastAPI."""

from functools import lru_cache
import logging
import os
from time import perf_counter

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
import joblib
import numpy as np
import pandas as pd
from prometheus_fastapi_instrumentator import Instrumentator

from src.api import prediction_cache
from src.api.models import CreditRequest, CreditResponse
from src.config import BEST_MODEL_PATH, BEST_NN_ONNX_PATH, BEST_NN_SCALER_PATH
from src.features.build_features import build_features
from src.monitoring.metrics import INFERENCE_ERRORS, INFERENCE_LATENCY, PREDICTIONS

logger = logging.getLogger("uvicorn.error")
MODEL_VERSION = os.environ.get("MODEL_VERSION", "unknown")
for metric in (PREDICTIONS, INFERENCE_ERRORS, INFERENCE_LATENCY):
    metric.labels(MODEL_VERSION)


@lru_cache(maxsize=1)
def load_model():
    """Загружает модель из файла."""
    if os.getenv("MODEL_BACKEND", "joblib") == "onnx":
        from src.api.onnx_model import ONNXModel

        try:
            return ONNXModel(BEST_NN_ONNX_PATH, BEST_NN_SCALER_PATH)
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="ONNX model/scaler cannot be loaded."
            ) from exc
    if os.getenv("MODEL_BACKEND", "joblib") != "joblib":
        raise HTTPException(status_code=503, detail="Unsupported MODEL_BACKEND")
    if not BEST_MODEL_PATH.is_file():
        raise HTTPException(status_code=503, detail="Model is missing. Run make train.")
    try:
        return joblib.load(BEST_MODEL_PATH)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Model cannot be loaded.") from exc


app = FastAPI(title="Credit Scoring API")


@app.middleware("http")
async def serving_location(request, call_next):
    response = await call_next(request)
    if os.getenv("SERVING_REGION"):
        import socket

        response.headers["X-Serving-Region"] = os.environ["SERVING_REGION"]
        response.headers["X-Serving-Replica"] = socket.gethostname()
    return response


Instrumentator(
    excluded_handlers=["/metrics", "/live", "/ready", "/health"],
    should_ignore_untemplated=True,
).instrument(app).expose(app, include_in_schema=False)


@app.get("/", include_in_schema=False)
async def root():
    """Редирект на документацию."""
    return RedirectResponse(url="/docs")


@app.get("/live")
def liveness():
    """Процесс работает независимо от наличия модели."""
    return {"status": "alive"}


@app.get("/ready")
@app.get("/health")
def health():
    """Проверяет статус сервера."""
    load_model()
    return {"status": "ok"}


@app.post("/predict", response_model=CreditResponse)
def predict(request: CreditRequest):
    """Делает предсказание с помощью модели."""
    started = perf_counter()
    try:
        model = load_model()
        response = prediction_cache.get(request)
        if response is None:
            features = build_features(pd.DataFrame([request.model_dump()]))
            features = (
                features[list(model.feature_names_in_)]
                .replace([np.inf, -np.inf], np.nan)
                .astype(float)
            )
            if os.getenv("MODEL_BACKEND", "joblib") == "onnx":
                probability = float(model.probability(features)[0])
                response = CreditResponse(
                    prediction=int(probability >= 0.5), default_probability=probability
                )
            else:
                response = joblib_response(model, features)
            prediction_cache.put(request, response)
    except Exception:
        INFERENCE_ERRORS.labels(MODEL_VERSION).inc()
        logger.error("event=inference_error model_version=%s", MODEL_VERSION)
        raise
    else:
        PREDICTIONS.labels(MODEL_VERSION).inc()
        logger.info("event=prediction_success model_version=%s", MODEL_VERSION)
        return response
    finally:
        INFERENCE_LATENCY.labels(MODEL_VERSION).observe(perf_counter() - started)


def joblib_response(model, features):
    default_index = list(model.classes_).index(1)
    return CreditResponse(
        prediction=int(model.predict(features)[0]),
        default_probability=float(model.predict_proba(features)[0, default_index]),
    )
