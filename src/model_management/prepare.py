"""Package the existing FP32/INT8 ONNX models with immutable provenance."""

import json
from pathlib import Path
import shutil

from src.config import BEST_NN_ONNX_PATH, BEST_NN_SCALER_PATH, MODELS_DIR
from src.monitoring.model_drift import sha


def main():
    root = Path("artifacts/stage6/repository")
    manifest = {}
    if (root / "manifest.json").exists():
        previous = json.loads((root / "manifest.json").read_text())
        if any(
            entry["scaler_sha256"] != sha(BEST_NN_SCALER_PATH)
            for entry in previous.values()
        ):
            raise ValueError("Scaler changed: use a new model revision/name")
    for name, path in [
        ("credit-fp32", BEST_NN_ONNX_PATH),
        ("credit-int8", MODELS_DIR / "credit_default_model_quant_dyn.onnx"),
    ]:
        target = root / name
        target.mkdir(parents=True, exist_ok=True)
        if (target / "model.onnx").exists() and sha(target / "model.onnx") != sha(path):
            raise ValueError("Packaged model changed: use a new model revision/name")
        shutil.copyfile(path, target / "model.onnx")
        shutil.copyfile("docker/seldon/runtime.py", target / "runtime.py")
        (target / "model-settings.json").write_text(
            json.dumps(
                dict(
                    name=name,
                    implementation="runtime.CreditONNX",
                    parameters=dict(uri="./model.onnx", version="v1"),
                ),
                indent=2,
            )
            + "\n"
        )
        manifest[name] = dict(
            model_sha256=sha(path),
            scaler_sha256=sha(BEST_NN_SCALER_PATH),
            source=str(path),
        )
    Path("artifacts/stage6/records").mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
