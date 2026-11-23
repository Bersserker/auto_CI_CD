"""Экспорт MLP в ONNX. Входные признаки должны быть масштабированы скейлером.

Запуск: uv run python -m src.neural.export_onnx
"""

import argparse
from pathlib import Path

import joblib
import onnx
import torch
from torch import nn

from src.config import BEST_NN_ONNX_PATH, BEST_NN_PATH, BEST_NN_SCALER_PATH
from src.neural.neural_network import CreditDefaultMLP


class ModelWithSigmoid(nn.Module):
    """Возвращает вероятность дефолта вместо логита."""

    def __init__(self, base_model: nn.Module):
        super().__init__()
        self.base_model = base_model
        self.sigmoid = nn.Sigmoid()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.sigmoid(self.base_model(x))


def export_onnx(
    model_path: Path = BEST_NN_PATH,
    scaler_path: Path = BEST_NN_SCALER_PATH,
    output_path: Path = BEST_NN_ONNX_PATH,
) -> Path:
    """Сохраняет ONNX: вход float32 [batch, features], выход [batch, 1].

    Перед инференсом вызовите scaler.transform для признаков в порядке обучения.
    Сам StandardScaler в ONNX не включён.
    """
    for path in (model_path, scaler_path):
        if not path.is_file():
            raise FileNotFoundError(
                f"Не найден {path}. Сначала запустите "
                "uv run python -m src.neural.train_nn"
            )

    scaler = joblib.load(scaler_path)
    n_features = int(scaler.n_features_in_)
    model = CreditDefaultMLP(in_features=n_features)
    state_dict = torch.load(model_path, map_location="cpu", weights_only=True)
    model.load_state_dict(state_dict)
    export_model = ModelWithSigmoid(model).cpu().eval()
    # Размер 2 позволяет сохранить batch динамическим при torch.export.
    dummy_input = torch.zeros(2, n_features, dtype=torch.float32)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with torch.no_grad():
        torch.onnx.export(
            export_model,
            dummy_input,
            str(output_path),
            export_params=True,
            opset_version=18,
            dynamo=True,
            external_data=False,
            input_names=["features"],
            output_names=["default_probability"],
            dynamic_shapes={"x": {0: torch.export.Dim("batch_size", min=1)}},
        )
    onnx.checker.check_model(onnx.load(str(output_path)))
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=BEST_NN_PATH)
    parser.add_argument("--scaler", type=Path, default=BEST_NN_SCALER_PATH)
    parser.add_argument("--output", type=Path, default=BEST_NN_ONNX_PATH)
    args = parser.parse_args()
    output_path = export_onnx(args.model, args.scaler, args.output)
    print(f"ONNX-модель сохранена: {output_path}")
    print("Вход features: float32, после scaler.transform, порядок как при обучении.")


if __name__ == "__main__":
    main()
