"""Optional Redis cache: model-scoped HMAC keys, bounded TTL and fail-open reads."""

from functools import lru_cache
import hashlib
import hmac
import json
import os
from pathlib import Path

from prometheus_client import Counter

from src.api.models import CreditResponse
from src.config import BEST_MODEL_PATH, BEST_NN_ONNX_PATH, BEST_NN_SCALER_PATH

EVENTS = Counter("scoring_cache_events_total", "Prediction cache events", ["result"])


@lru_cache(maxsize=1)
def client():
    import redis

    return redis.Redis.from_url(
        os.environ["REDIS_URL"], socket_timeout=0.1, socket_connect_timeout=0.1
    )


@lru_cache(maxsize=1)
def identity():
    paths = (
        [BEST_NN_ONNX_PATH, BEST_NN_SCALER_PATH]
        if os.getenv("MODEL_BACKEND", "joblib") == "onnx"
        else [BEST_MODEL_PATH]
    )
    return ":".join(hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths)


@lru_cache(maxsize=1)
def secret():
    value = (
        Path(os.environ["CACHE_KEY_SECRET_FILE"]).read_bytes()
        if os.getenv("CACHE_KEY_SECRET_FILE")
        else os.getenv("CACHE_KEY_SECRET", "").encode()
    )
    if len(value) < 32:
        raise ValueError("CACHE_KEY_SECRET must contain at least 32 bytes")
    return value


def key(request):
    payload = json.dumps(
        request.model_dump(), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    digest = hmac.new(
        secret(), (identity() + ":" + payload).encode(), hashlib.sha256
    ).hexdigest()
    return "scoring:v1:" + digest


def get(request):
    if not os.getenv("REDIS_URL"):
        return None
    try:
        value = client().get(key(request))
        if value is not None:
            response = CreditResponse.model_validate_json(value)
            EVENTS.labels("hit").inc()
            return response
        EVENTS.labels("miss").inc()
    except Exception:
        EVENTS.labels("error").inc()
    return None


def put(request, response):
    if not os.getenv("REDIS_URL"):
        return
    try:
        ttl = int(os.getenv("CACHE_TTL_SECONDS", "60"))
        if not 1 <= ttl <= 3600:
            raise ValueError("Cache TTL must be 1..3600 seconds")
        client().setex(key(request), ttl, response.model_dump_json())
    except Exception:
        EVENTS.labels("error").inc()
