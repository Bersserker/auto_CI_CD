"""Compare real API inference with the training/export preprocessing contract."""

from fastapi.testclient import TestClient
import joblib
import numpy as np
import onnxruntime as ort
import pandas as pd
import pytest

from src.api import app as api
from src.api.models import CreditRequest
from src.api.onnx_model import FEATURE_NAMES
from src.config import BEST_NN_ONNX_PATH, BEST_NN_SCALER_PATH, PROCESSED_DATA_PATH


@pytest.mark.skipif(
    not all(
        p.is_file()
        for p in (BEST_NN_ONNX_PATH, BEST_NN_SCALER_PATH, PROCESSED_DATA_PATH)
    ),
    reason="Local ONNX artifacts/data required",
)
def test_real_onnx_matches_training_preprocessing(monkeypatch):
    monkeypatch.setenv("MODEL_BACKEND", "onnx")
    api.load_model.cache_clear()
    try:
        frame = pd.read_csv(PROCESSED_DATA_PATH, nrows=3)
        assert frame.drop(columns=["id", "default"]).columns.tolist() == FEATURE_NAMES
        scaler = joblib.load(BEST_NN_SCALER_PATH)
        values = scaler.transform(
            frame[FEATURE_NAMES].to_numpy(dtype=np.float32)
        ).astype(np.float32)
        session = ort.InferenceSession(
            str(BEST_NN_ONNX_PATH), providers=["CPUExecutionProvider"]
        )
        expected = session.run(["default_probability"], {"features": values})[0][:, 0]
        with TestClient(api.app) as client:
            assert client.get("/ready").status_code == 200
            for (_, row), probability in zip(frame.iterrows(), expected):
                payload = CreditRequest.model_validate(
                    row[list(CreditRequest.model_fields)].to_dict()
                ).model_dump()
                response = client.post("/predict", json=payload)
                assert response.status_code == 200, response.text
                assert response.json()["default_probability"] == pytest.approx(
                    float(probability), abs=1e-6
                )
                assert response.json()["prediction"] == int(probability >= 0.5)
    finally:
        api.load_model.cache_clear()


def test_missing_onnx_scaler_is_not_ready(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_BACKEND", "onnx")
    monkeypatch.setattr(api, "BEST_NN_SCALER_PATH", tmp_path / "missing.joblib")
    api.load_model.cache_clear()
    try:
        with TestClient(api.app) as client:
            assert client.get("/ready").status_code == 503
            assert client.get("/live").status_code == 200
            assert client.get("/metrics").status_code == 200
    finally:
        api.load_model.cache_clear()
