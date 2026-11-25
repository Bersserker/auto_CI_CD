"""Release gates, leakage prevention, Redis privacy and scale hysteresis."""

import io
import tarfile
from types import SimpleNamespace

import pandas as pd
import pytest
from sklearn.model_selection import train_test_split

from scripts.stage7_autoscale import decide
from scripts.stage7_candidate import extract
from src.api import prediction_cache as cache
from src.api.models import CreditRequest, CreditResponse
from src.retraining import pipeline


def request():
    values = {c: 0 for c in CreditRequest.model_fields}
    values.update(limit_bal=123456, sex=1, age=30)
    return CreditRequest(**values)


def test_cache_keys_private_model_scoped(monkeypatch):
    monkeypatch.setattr(cache, "secret", lambda: b"x" * 32)
    monkeypatch.setattr(cache, "identity", lambda: "model-A-scaler-A")
    first = cache.key(request())
    assert first == cache.key(request())
    assert "123456" not in first and "limit_bal" not in first
    monkeypatch.setattr(cache, "identity", lambda: "model-B-scaler-A")
    assert first != cache.key(request())


def test_cache_outage_and_corruption_fail_open(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://local")
    monkeypatch.setattr(cache, "key", lambda r: "key")

    def unavailable():
        raise TimeoutError()

    monkeypatch.setattr(cache, "client", unavailable)
    assert cache.get(request()) is None
    cache.put(request(), CreditResponse(prediction=0, default_probability=0.2))
    monkeypatch.setattr(
        cache,
        "client",
        lambda: SimpleNamespace(
            get=lambda k: b'{"prediction":7,"default_probability":2}'
        ),
    )
    assert cache.get(request()) is None


def test_cache_ttl_bounded(monkeypatch):
    calls = []
    monkeypatch.setenv("REDIS_URL", "redis://local")
    monkeypatch.setattr(cache, "key", lambda r: "key")
    monkeypatch.setattr(
        cache, "client", lambda: SimpleNamespace(setex=lambda *x: calls.append(x))
    )
    monkeypatch.setenv("CACHE_TTL_SECONDS", "10")
    cache.put(request(), CreditResponse(prediction=0, default_probability=0.2))
    assert calls[0][1] == 10
    monkeypatch.setenv("CACHE_TTL_SECONDS", "0")
    cache.put(request(), CreditResponse(prediction=0, default_probability=0.2))
    assert len(calls) == 1


def test_release_gate_rejects_bad_quality_and_parity():
    metrics = dict(
        rows=6000,
        candidate_auc=0.78,
        baseline_auc=0.77,
        candidate_log_loss=0.45,
        baseline_log_loss=0.56,
        parity_error=1e-7,
        latency_seconds=0.002,
    )
    assert pipeline.gate(metrics)
    for key, value in [
        ("candidate_auc", 0.6),
        ("parity_error", 0.1),
        ("candidate_log_loss", 1),
        ("latency_seconds", 1),
        ("rows", 1),
        ("candidate_auc", float("nan")),
    ]:
        assert not pipeline.gate(dict(metrics, **{key: value}))


def history(tmp_path, monkeypatch):
    frame = pd.DataFrame(
        [dict(request().model_dump(), id=i, default=i % 2) for i in range(250)]
    )
    source = tmp_path / "history.csv"
    frame.to_csv(source, index=False)
    monkeypatch.setattr(pipeline, "PROCESSED_DATA_PATH", source)
    monkeypatch.setattr(pipeline, "ROOT", tmp_path / "stage7")
    (pipeline.ROOT / "inbox").mkdir(parents=True)
    return frame


def test_holdout_leakage_blocks_new_data(tmp_path, monkeypatch):
    frame = history(tmp_path, monkeypatch)
    _, holdout = train_test_split(
        frame, test_size=0.2, random_state=42, stratify=frame.default
    )
    holdout.iloc[:1].rename(columns={"default": "target"}).to_csv(
        pipeline.ROOT / "inbox/new.csv", index=False
    )
    with pytest.raises(ValueError, match="Holdout leakage"):
        pipeline.prepare("leak", {"batch": "new.csv", "demo": True})


def test_unmature_labels_cannot_train(tmp_path, monkeypatch):
    frame = (
        history(tmp_path, monkeypatch).iloc[:1].rename(columns={"default": "target"})
    )
    frame["id"] = 9999
    frame["predicted_at"] = "2026-11-01T00:00:00Z"
    frame["matured_at"] = "2026-11-02T00:00:00Z"
    frame["observed_at"] = "2026-11-03T00:00:00Z"
    frame.to_csv(pipeline.ROOT / "inbox/new.csv", index=False)
    with pytest.raises(ValueError, match="matured"):
        pipeline.prepare(
            "immature", {"batch": "new.csv", "as_of": "2026-11-25T00:00:00Z"}
        )


def test_scaler_hysteresis_bounds():
    assert decide(2, 1, 0, 2, 4) == 2
    assert decide(2, 2, 0, 2, 4) == 3
    assert decide(4, 9, 0, 2, 4) == 4
    assert decide(3, 0, 4, 2, 4) == 2
    assert decide(2, 0, 9, 2, 4) == 2


def test_bundle_traversal_blocked(tmp_path):
    bundle = tmp_path / "bad.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        item = tarfile.TarInfo("../escape")
        item.size = 1
        archive.addfile(item, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="Unexpected"):
        extract(bundle, tmp_path / "output")
    assert not (tmp_path.parent / "escape").exists()


def test_missing_maturity_rejected_and_default_cutoff_accepts_mature_batch(
    tmp_path, monkeypatch
):
    frame = (
        history(tmp_path, monkeypatch).iloc[:1].rename(columns={"default": "target"})
    )
    frame["id"] = 9999
    now = pd.Timestamp.now(tz="UTC")
    frame["predicted_at"] = (now - pd.Timedelta(days=60)).isoformat()
    frame["matured_at"] = None
    frame["observed_at"] = (now - pd.Timedelta(days=20)).isoformat()
    path = pipeline.ROOT / "inbox/new.csv"
    frame.to_csv(path, index=False)
    with pytest.raises(ValueError, match="Missing maturity"):
        pipeline.prepare("missing-time", {"batch": "new.csv"})
    frame["matured_at"] = (now - pd.Timedelta(days=30)).isoformat()
    frame.to_csv(path, index=False)
    champion = tmp_path / "model.onnx"
    scaler = tmp_path / "scaler.joblib"
    champion.write_bytes(b"prepare-stage-fixture")
    scaler.write_bytes(b"prepare-stage-fixture")
    monkeypatch.setattr(pipeline, "BEST_NN_ONNX_PATH", champion)
    monkeypatch.setattr(pipeline, "BEST_NN_SCALER_PATH", scaler)
    prepared = pipeline.prepare("mature-batch", {"batch": "new.csv", "as_of": None})
    assert prepared["train_rows"] == 201
