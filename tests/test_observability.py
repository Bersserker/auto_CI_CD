"""Проверяет реальные запросы, метрики и отсутствие клиентских данных в логах."""

import logging

from fastapi.testclient import TestClient
import numpy as np
import pytest

from src.api import app as api
from src.api.models import CreditRequest
from src.monitoring.metrics import INFERENCE_ERRORS, INFERENCE_LATENCY, PREDICTIONS


@pytest.fixture
def payload():
    values = {name: 0 for name in CreditRequest.model_fields}
    values.update(sex=1, age=30, limit_bal=123456)
    return values


class Model:
    feature_names_in_ = ["limit_bal"]
    classes_ = [0, 1]

    def predict(self, features):
        return np.array([0])

    def predict_proba(self, features):
        return np.array([[0.8, 0.2]])


def test_success_metrics_and_logs(monkeypatch, payload, caplog):
    monkeypatch.setattr(api, "load_model", lambda: Model())
    counter = PREDICTIONS.labels(api.MODEL_VERSION)
    before = counter._value.get()
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with TestClient(api.app) as client:
            assert client.post("/predict", json=payload).status_code == 200
            metrics = client.get("/metrics")
    assert counter._value.get() == before + 1
    assert metrics.status_code == 200
    assert "scoring_predictions_total" in metrics.text
    assert 'handler="/predict"' in metrics.text
    assert 'handler="/metrics"' not in metrics.text
    assert "event=prediction_success" in caplog.text
    assert "123456" not in caplog.text


def test_error_counted_once(monkeypatch, tmp_path, payload, caplog):
    api.load_model.cache_clear()
    monkeypatch.setattr(api, "BEST_MODEL_PATH", tmp_path / "missing.joblib")
    errors = INFERENCE_ERRORS.labels(api.MODEL_VERSION)
    success = PREDICTIONS.labels(api.MODEL_VERSION)
    before, successful = errors._value.get(), success._value.get()
    with TestClient(api.app) as client:
        assert client.post("/predict", json=payload).status_code == 503
    assert errors._value.get() == before + 1
    assert success._value.get() == successful
    assert "event=inference_error" in caplog.text
    assert INFERENCE_LATENCY.labels(api.MODEL_VERSION)._sum.get() > 0
    api.load_model.cache_clear()


def test_validation_does_not_count_as_inference_error(payload):
    errors = INFERENCE_ERRORS.labels(api.MODEL_VERSION)
    before = errors._value.get()
    with TestClient(api.app) as client:
        assert client.post("/predict", json={}).status_code == 422
        assert client.get("/metrics").status_code == 200
    assert errors._value.get() == before
