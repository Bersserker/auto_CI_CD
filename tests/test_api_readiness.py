"""Readiness зависит от модели, liveness — только от процесса."""

from fastapi.testclient import TestClient
import joblib
import pytest

from src.api import app as api


@pytest.fixture
def client(monkeypatch, tmp_path):
    api.load_model.cache_clear()
    monkeypatch.setattr(api, "BEST_MODEL_PATH", tmp_path / "model.joblib")
    with TestClient(api.app) as client:
        yield client
    api.load_model.cache_clear()


def test_missing_model_does_not_break_liveness(client):
    assert client.get("/live").status_code == 200
    assert client.get("/ready").status_code == 503
    assert client.get("/health").status_code == 503


def test_corrupt_model_is_not_ready(client):
    api.BEST_MODEL_PATH.write_bytes(b"not-a-joblib-model")
    assert client.get("/ready").status_code == 503
    assert client.get("/live").status_code == 200


def test_loaded_model_is_ready(client):
    joblib.dump({"test": "readiness-only"}, api.BEST_MODEL_PATH)
    assert client.get("/ready").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}
