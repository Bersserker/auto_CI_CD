"""Проверяет граф ONNX и совпадение вероятностей с PyTorch на test-выборке."""

import argparse
from pathlib import Path

import joblib
import numpy as np
import onnx
import onnxruntime as ort
import pandas as pd
from sklearn.model_selection import train_test_split
import torch

from src.config import (
    BEST_NN_PATH,
    BEST_NN_SCALER_PATH,
    MODELS_DIR,
    PROCESSED_DATA_PATH,
)
from src.neural.neural_network import CreditDefaultMLP


def validate_onnx(
    onnx_path: Path = MODELS_DIR / "credit_default_model.onnx",
    model_path: Path = BEST_NN_PATH,
    scaler_path: Path = BEST_NN_SCALER_PATH,
    data_path: Path = PROCESSED_DATA_PATH,
    batch_size: int = 512,
) -> float:
    """Проверяет всю test-выборку; при расхождении завершает работу с ошибкой."""
    if batch_size < 1:
        raise ValueError("batch_size должен быть положительным")
    for path in (onnx_path, model_path, scaler_path, data_path):
        if not path.is_file():
            raise FileNotFoundError(f"Не найден файл: {path}")

    # Повторяем подготовку и разбиение из train_nn.py, включая порядок колонок.
    df = pd.read_csv(data_path).drop(columns=["id"])
    features = df.drop(columns=["default"]).to_numpy(dtype=np.float32)
    targets = df["default"].to_numpy(dtype=np.float32)
    _, X_test, _, _ = train_test_split(
        features, targets, test_size=0.2, random_state=42, stratify=targets
    )
    scaler = joblib.load(scaler_path)
    X_test = scaler.transform(X_test).astype(np.float32)
    if not np.isfinite(X_test).all():
        raise ValueError("После масштабирования обнаружены NaN или бесконечности")

    model = CreditDefaultMLP(in_features=int(scaler.n_features_in_))
    model.load_state_dict(torch.load(model_path, map_location="cpu", weights_only=True))
    model.eval()

    onnx.checker.check_model(onnx.load(str(onnx_path)))
    session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    max_error = 0.0
    # Отдельный одиночный пример дополнительно проверяет динамический batch.
    batches = [X_test[:1]]
    for start in range(0, len(X_test), batch_size):
        stop = start + batch_size
        batches.append(X_test[start:stop])
    for sample in batches:
        with torch.no_grad():
            torch_probs = torch.sigmoid(model(torch.from_numpy(sample))).numpy()
        onnx_probs = session.run(["default_probability"], {"features": sample})[0]
        if onnx_probs.shape != (len(sample), 1):
            raise AssertionError(f"Некорректная форма выхода: {onnx_probs.shape}")
        if not np.isfinite(onnx_probs).all() or not np.isfinite(torch_probs).all():
            raise AssertionError("Предсказания содержат NaN или бесконечности")
        if ((onnx_probs < 0) | (onnx_probs > 1)).any():
            raise AssertionError("Вероятности ONNX выходят за диапазон [0, 1]")
        np.testing.assert_allclose(
            onnx_probs,
            torch_probs,
            rtol=1e-5,
            atol=1e-6,
            err_msg="Вероятности ONNX и PyTorch не совпадают",
        )
        max_error = max(max_error, float(np.max(np.abs(onnx_probs - torch_probs))))

    print(f"Проверка пройдена: {len(X_test)} тестовых объектов и batch=1.")
    print(f"Максимальная абсолютная ошибка: {max_error:.3e}")
    return max_error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--onnx", type=Path, default=MODELS_DIR / "credit_default_model.onnx"
    )
    parser.add_argument("--model", type=Path, default=BEST_NN_PATH)
    parser.add_argument("--scaler", type=Path, default=BEST_NN_SCALER_PATH)
    parser.add_argument("--data", type=Path, default=PROCESSED_DATA_PATH)
    parser.add_argument("--batch-size", type=int, default=512)
    args = parser.parse_args()
    validate_onnx(args.onnx, args.model, args.scaler, args.data, args.batch_size)


if __name__ == "__main__":
    main()
