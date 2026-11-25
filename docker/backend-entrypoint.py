"""Получить зафиксированную в dvc.lock модель и только затем запустить API."""

import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    mode = os.getenv("DVC_PULL", "0")
    if mode not in {"0", "1"}:
        print("DVC_PULL должен быть 0 или 1", file=sys.stderr)
        return 1
    backend = os.getenv("MODEL_BACKEND", "joblib")
    if backend not in {"joblib", "onnx"}:
        print("MODEL_BACKEND должен быть joblib или onnx", file=sys.stderr)
        return 1
    artifacts = (
        [Path("models/credit_default_model.onnx"), Path("models/best_scaler.joblib")]
        if backend == "onnx"
        else [Path("models/best_model.joblib")]
    )
    if mode == "1":
        try:
            subprocess.run(
                [sys.executable, "scripts/configure_dvc_remote.py"], check=True
            )
            # При смене релиза обновляется и файл в сохраняемом томе models.
            subprocess.run(
                [
                    "dvc",
                    "pull",
                    "--force",
                    "--jobs",
                    "2",
                    *[str(path) for path in artifacts],
                ],
                check=True,
            )
        except subprocess.CalledProcessError:
            print("Загрузка модели не удалась; API не запущен.", file=sys.stderr)
            return 1
    for artifact in artifacts:
        if not artifact.is_file() or artifact.stat().st_size == 0:
            print(
                f"Нет {artifact}: загрузите артефакты через DVC_PULL=1 или подключите локальные файлы.",
                file=sys.stderr,
            )
            return 1
    command = sys.argv[1:] or [
        "uvicorn",
        "src.api.app:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    # exec передает сигналы остановки непосредственно серверу.
    os.execvp(command[0], command)
    return 0


if __name__ == "__main__":
    sys.exit(main())
