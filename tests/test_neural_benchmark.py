import time

import numpy as np
import pytest

from src.neural import benchmark


def test_measure_counts_wall_throughput_and_preprocessing():
    class Scaler:
        def transform(self, sample):
            return sample + 2

    def run(sample):
        assert np.all(sample == 3)
        assert sample.dtype == np.float32
        time.sleep(0.002)
        return np.zeros((len(sample), 1), dtype=np.float32)

    result = benchmark.measure(
        run,
        Scaler(),
        [np.ones((8, 3), dtype=np.float32)],
        concurrency=2,
        duration=0.04,
        warmup=1,
        cpus=2,
    )
    assert result["requests"] >= 2
    assert result["p50_ms"] >= 2
    assert result["p50_ms"] <= result["p95_ms"] <= result["p99_ms"]
    assert result["requests_per_second"] == pytest.approx(
        result["requests"] / result["wall_seconds"]
    )
    assert result["samples_per_second"] == pytest.approx(
        8 * result["requests_per_second"]
    )
    assert result["rss_peak_mib"] > 0
    assert result["gpu_util_mean_percent"] is None


def test_onnx_gpu_rejects_missing_cuda_provider(monkeypatch):
    import onnxruntime
    import torch

    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(
        onnxruntime, "get_available_providers", lambda: ["CPUExecutionProvider"]
    )
    with pytest.raises(benchmark.Unavailable, match="CUDAExecutionProvider"):
        benchmark.make_runner("onnx_fp32", "cuda", 1, 32)


def test_warmup_rejects_nonfinite_outputs():
    class Scaler:
        def transform(self, sample):
            return sample

    with pytest.raises(ValueError, match="NaN"):
        benchmark.measure(
            lambda sample: np.full((1, 1), np.nan),
            Scaler(),
            [np.ones((1, 3), dtype=np.float32)],
            1,
            0.01,
            1,
            1,
        )
