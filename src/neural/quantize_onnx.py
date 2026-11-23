"""Динамическая INT8-квантизация ONNX с проверкой качества на test-выборке."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import joblib
import numpy as np
import onnx
import onnxruntime as ort
from onnxruntime.quantization import QuantType, quantize_dynamic
from onnxruntime.quantization.shape_inference import quant_pre_process
import pandas as pd
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score
from sklearn.model_selection import train_test_split

from src.config import (
    BEST_NN_ONNX_PATH,
    BEST_NN_SCALER_PATH,
    MODELS_DIR,
    PROCESSED_DATA_PATH,
    REPORTS_DIR,
)

QUANTIZED_PATH = MODELS_DIR / "credit_default_model_quant_dyn.onnx"


def predict(session: ort.InferenceSession, sample: np.ndarray) -> np.ndarray:
    """Проверяет контракт выхода и возвращает вероятности."""
    probs = session.run(["default_probability"], {"features": sample})[0]
    if probs.shape != (len(sample), 1):
        raise AssertionError(f"Некорректная форма выхода: {probs.shape}")
    if not np.isfinite(probs).all() or ((probs < 0) | (probs > 1)).any():
        raise AssertionError("Вероятности должны быть конечными числами в [0, 1]")
    return probs.ravel()


def quality(targets: np.ndarray, probs: np.ndarray) -> dict:
    labels = probs >= 0.5
    return {
        "roc_auc": float(roc_auc_score(targets, probs)),
        "f1": float(f1_score(targets, labels, zero_division=0)),
        "accuracy": float(accuracy_score(targets, labels)),
    }


def quantize_onnx(
    source: Path = BEST_NN_ONNX_PATH,
    output: Path = QUANTIZED_PATH,
    scaler_path: Path = BEST_NN_SCALER_PATH,
    data_path: Path = PROCESSED_DATA_PATH,
    report_path: Path = REPORTS_DIR / "quantization.json",
    batch_size: int = 512,
    max_metric_drop: float = 0.01,
) -> dict:
    """Квантует веса в UINT8; падение каждой метрики ограничено абсолютным порогом.

    Сравнивает с исходной FP32 ONNX, а не с PyTorch. При провале проверки
    сохраняет отчёт со статусом failed и завершает работу с ошибкой.
    """
    if batch_size < 1:
        raise ValueError("batch_size должен быть положительным")
    if not np.isfinite(max_metric_drop) or not 0 <= max_metric_drop <= 1:
        raise ValueError("max_metric_drop должен быть в [0, 1]")
    if output.resolve() in {
        source.resolve(),
        scaler_path.resolve(),
        data_path.resolve(),
    }:
        raise ValueError("Выходной файл не должен перезаписывать входные файлы")
    if report_path.resolve() in {
        p.resolve() for p in (source, output, scaler_path, data_path)
    }:
        raise ValueError("Путь отчёта должен отличаться от путей моделей и данных")
    for path in (source, scaler_path, data_path):
        if not path.is_file():
            raise FileNotFoundError(f"Не найден файл: {path}")
    onnx.checker.check_model(onnx.load(str(source)))

    # Совпадает с train_nn.py: тот же порядок признаков и разбиение.
    df = pd.read_csv(data_path).drop(columns=["id"])
    features = df.drop(columns=["default"]).to_numpy(dtype=np.float32)
    targets = df["default"].to_numpy(dtype=np.float32)
    _, X_test, _, y_test = train_test_split(
        features, targets, test_size=0.2, random_state=42, stratify=targets
    )
    scaler = joblib.load(scaler_path)
    X_test = scaler.transform(X_test).astype(np.float32)
    if not np.isfinite(X_test).all():
        raise ValueError("После масштабирования обнаружены NaN или бесконечности")

    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="onnx-quant-") as tmp:
        prepared = Path(tmp) / "prepared.onnx"
        quant_pre_process(source, prepared)
        quantize_dynamic(prepared, output, weight_type=QuantType.QUInt8)

    quantized = onnx.load(str(output))
    onnx.checker.check_model(quantized)
    if not any(n.op_type == "MatMulInteger" for n in quantized.graph.node):
        raise AssertionError("Квантизация не создала INT8-операторы MatMulInteger")
    sessions = [
        ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        for path in (source, output)
    ]
    # Проверяем динамический batch=1 отдельно от основного размера батча.
    for session in sessions:
        predict(session, X_test[:1])
    predictions = [[], []]
    for start in range(0, len(X_test), batch_size):
        stop = start + batch_size
        sample = X_test[start:stop]
        for session, parts in zip(sessions, predictions):
            parts.append(predict(session, sample))
    fp32_probs, int8_probs = [np.concatenate(parts) for parts in predictions]
    fp32_metrics, int8_metrics = [quality(y_test, p) for p in (fp32_probs, int8_probs)]
    drops = {key: fp32_metrics[key] - int8_metrics[key] for key in fp32_metrics}
    failed = [key for key, drop in drops.items() if drop > max_metric_drop]
    difference = np.abs(fp32_probs - int8_probs)
    report = {
        "status": "failed" if failed else "passed",
        "source": str(source),
        "output": str(output),
        "quantization": "dynamic QUInt8",
        "test_samples": len(y_test),
        "batch_size": batch_size,
        "threshold": 0.5,
        "max_metric_drop": max_metric_drop,
        "fp32_bytes": source.stat().st_size,
        "int8_bytes": output.stat().st_size,
        "fp32_metrics": fp32_metrics,
        "int8_metrics": int8_metrics,
        "metric_drops": drops,
        "max_probability_difference": float(difference.max()),
        "mean_probability_difference": float(difference.mean()),
        "label_disagreement_rate": float(
            np.mean((fp32_probs >= 0.5) != (int8_probs >= 0.5))
        ),
        "failed_metrics": failed,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Отчёт сохранён: {report_path}")
    if failed:
        raise AssertionError(f"Падение качества превысило порог: {', '.join(failed)}")
    print("Валидация квантованной модели пройдена.")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onnx", type=Path, default=BEST_NN_ONNX_PATH)
    parser.add_argument("--output", type=Path, default=QUANTIZED_PATH)
    parser.add_argument("--scaler", type=Path, default=BEST_NN_SCALER_PATH)
    parser.add_argument("--data", type=Path, default=PROCESSED_DATA_PATH)
    parser.add_argument(
        "--report", type=Path, default=REPORTS_DIR / "quantization.json"
    )
    parser.add_argument("--batch-size", type=int, default=512)
    parser.add_argument("--max-metric-drop", type=float, default=0.01)
    args = parser.parse_args()
    quantize_onnx(
        args.onnx,
        args.output,
        args.scaler,
        args.data,
        args.report,
        args.batch_size,
        args.max_metric_drop,
    )


if __name__ == "__main__":
    main()
