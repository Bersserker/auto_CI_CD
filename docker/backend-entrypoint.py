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
    model = Path("models/best_model.joblib")
    if mode == "1":
        try:
            subprocess.run(
                [sys.executable, "scripts/configure_dvc_remote.py"], check=True
            )
            # При смене релиза обновляется и файл в сохраняемом томе models.
            subprocess.run(
                ["dvc", "pull", "--force", "--jobs", "2", str(model)], check=True
            )
        except subprocess.CalledProcessError:
            print("Загрузка модели не удалась; API не запущен.", file=sys.stderr)
            return 1
    if not model.is_file() or model.stat().st_size == 0:
        print(
            "Нет models/best_model.joblib: загрузите модель через DVC_PULL=1 или подключите локальный файл.",
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
