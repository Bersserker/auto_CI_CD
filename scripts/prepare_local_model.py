"""Создать изолированный тестовый DVC remote для проверки Kubernetes без облака."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import joblib
import pandas as pd
from sklearn.dummy import DummyClassifier

from src.features.build_features import build_features


def prepare(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("Каталог тестового remote должен быть пустым.")
    app = root / "publisher"
    (app / ".dvc").mkdir(parents=True)
    (app / "models").mkdir()
    (app / ".dvc/config").write_text(
        "[core]\n    no_scm = true\n    analytics = false\n[cache]\n    type = copy\n"
    )
    payload = {"limit_bal": 20000, "sex": 2, "education": 2, "marriage": 1, "age": 30}
    payload.update(
        {k: 0 for k in ["pay_0", "pay_2", "pay_3", "pay_4", "pay_5", "pay_6"]}
    )
    payload.update({f"bill_amt{i}": 0 for i in range(1, 7)})
    payload.update({f"pay_amt{i}": 0 for i in range(1, 7)})
    model = DummyClassifier(strategy="prior").fit(
        build_features(pd.DataFrame([payload, payload])), [0, 1]
    )
    joblib.dump(model, app / "models/best_model.joblib")
    (app / "dvc.yaml").write_text(
        "stages:\n  train:\n    cmd: python train.py\n    outs:\n      - models/best_model.joblib\n"
    )
    env = os.environ.copy()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    env["DVC_SITE_CACHE_DIR"] = str(root / "site-cache")
    for command in [
        ["dvc", "commit", "--force", "train"],
        [
            "dvc",
            "remote",
            "add",
            "--local",
            "--default",
            "artifacts",
            str(root / "remote"),
        ],
        ["dvc", "push", "models/best_model.joblib"],
    ]:
        subprocess.run(
            command, cwd=app, env=env, check=True, capture_output=True, text=True
        )
    (root / "request.json").write_text(json.dumps(payload))
    print(
        f"Синтетическая модель опубликована в {root / 'remote'}; исходники проекта не изменены."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.directory.resolve())


if __name__ == "__main__":
    main()
