"""Mature labels, missing quality and isolated shadow routing."""

import json
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import numpy as np
import pandas as pd
import pytest

from src.api.models import CreditRequest
from src.model_management import gateway
from src.monitoring.model_drift import FEATURES, attach_outcomes, evaluate


def test_assignment_stable_and_balanced():
    values = [gateway.assignment(f"person-{i}") for i in range(1000)]
    assert values == [gateway.assignment(f"person-{i}") for i in range(1000)]
    assert 450 < values.count("credit-int8") < 550
    assert gateway.assignment("id", 0) == "credit-fp32"
    assert gateway.assignment("id", 100) == "credit-int8"


def test_only_mature_observed_labels_are_joined():
    predictions = pd.DataFrame(
        {"request_id": ["a", "b"], "predicted_at": ["2026-01-01T00:00:00Z"] * 2}
    )
    labels = pd.DataFrame(
        {
            "request_id": ["a", "b"],
            "target": [0, 1],
            "matured_at": ["2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z"],
            "observed_at": ["2026-02-02T00:00:00Z", "2026-03-02T00:00:00Z"],
        }
    )
    joined = attach_outcomes(predictions, labels, "2026-02-15T00:00:00Z")
    assert joined.target.notna().tolist() == [True, False]
    with pytest.raises(ValueError, match="Duplicate"):
        attach_outcomes(
            predictions, pd.concat([labels, labels]), "2026-02-15T00:00:00Z"
        )
    labels.loc[0, "matured_at"] = "2026-01-02T00:00:00Z"
    with pytest.raises(ValueError, match="horizon"):
        attach_outcomes(predictions, labels, "2026-02-15T00:00:00Z")


def test_no_labels_does_not_claim_healthy_quality(tmp_path):
    rng = np.random.default_rng(42)
    ref = pd.DataFrame({c: rng.normal(size=250) for c in FEATURES})
    ref["probability"] = rng.uniform(0.1, 0.9, 250)
    ref["target"] = rng.integers(0, 2, 250)
    current = ref.copy()
    current["target"] = np.nan
    result = evaluate(ref, current, tmp_path)
    assert result["data_drift"] is False
    assert result["performance_decay"] is None
    assert result["concept_drift_suspected"] is None
    assert "scoring_roc_auc" not in (tmp_path / "metrics.prom").read_text()


def test_shadow_failure_does_not_change_primary(monkeypatch, tmp_path):
    journal = tmp_path / "predictions.jsonl"
    monkeypatch.setenv("JOURNAL_PATH", str(journal))
    monkeypatch.setattr(
        gateway, "get_scaler", lambda: SimpleNamespace(transform=lambda x: x)
    )

    def infer(model, values):
        if model == "credit-int8":
            raise OSError("candidate offline")
        return 0.25

    monkeypatch.setattr(gateway, "infer", infer)
    payload = {c: 0 for c in CreditRequest.model_fields}
    payload.update(limit_bal=100000, sex=1, age=30)
    with TestClient(gateway.app) as client:
        response = client.post(
            "/predict?mode=shadow", json=payload, headers={"X-Request-ID": "opaque-id"}
        )
        assert response.status_code == 200
        assert response.json()["default_probability"] == 0.25
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            rows = [json.loads(line) for line in journal.read_text().splitlines()]
            if len(rows) == 2:
                break
            time.sleep(0.01)
        assert any(r["model"] == "credit-int8" and r["success"] is False for r in rows)
        assert all("limit_bal" not in r for r in rows)
        assert (
            client.post(
                "/predict?mode=ab",
                json=payload,
                headers={"X-Request-ID": "email@example.com"},
            ).status_code
            == 422
        )


def test_primary_error_is_recorded(monkeypatch, tmp_path):
    journal = tmp_path / "primary.jsonl"
    monkeypatch.setenv("JOURNAL_PATH", str(journal))
    monkeypatch.setattr(
        gateway, "get_scaler", lambda: SimpleNamespace(transform=lambda x: x)
    )

    def unavailable(*args):
        raise OSError("Seldon unavailable")

    monkeypatch.setattr(gateway, "infer", unavailable)
    payload = {c: 0 for c in CreditRequest.model_fields}
    payload.update(limit_bal=100000, sex=1, age=30)
    with TestClient(gateway.app) as client:
        assert (
            client.post(
                "/predict?mode=ab",
                json=payload,
                headers={"X-Request-ID": "failure-case"},
            ).status_code
            == 503
        )
    assert json.loads(journal.read_text())["success"] is False
