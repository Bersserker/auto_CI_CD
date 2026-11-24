"""Проверить DVC -> модель -> API в Docker без облака, на синтетической модели."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import joblib
import pandas as pd
from sklearn.dummy import DummyClassifier

from src.features.build_features import build_features

ROOT = Path(__file__).resolve().parents[1]


def command(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="credit-backend:dvc")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mlops-dvc-smoke-") as temp:
        root = Path(temp)
        app = root / "publisher"
        (app / ".dvc").mkdir(parents=True)
        (app / "models").mkdir()
        (app / ".dvc/config").write_text(
            "[core]\n    no_scm = true\n    analytics = false\n[cache]\n    type = copy\n"
        )
        payload = {
            "limit_bal": 20000,
            "sex": 2,
            "education": 2,
            "marriage": 1,
            "age": 30,
        }
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
        command("dvc", "commit", "--force", "train", cwd=app, env=env)
        command(
            "dvc",
            "remote",
            "add",
            "--local",
            "--default",
            "artifacts",
            str(root / "remote"),
            cwd=app,
            env=env,
        )
        command("dvc", "push", "models/best_model.joblib", cwd=app, env=env)
        # Только тестовая модель и ее метаданные: исходный dvc.lock не изменяется.
        cid = command(
            "docker",
            "run",
            "--detach",
            "--env",
            "DVC_PULL=1",
            "--env",
            "DVC_REMOTE_URL=/remote",
            "--mount",
            f"type=bind,src={root / 'remote'},dst=/remote,readonly",
            "--mount",
            f"type=bind,src={app / 'dvc.yaml'},dst=/app/dvc.yaml,readonly",
            "--mount",
            f"type=bind,src={app / 'dvc.lock'},dst=/app/dvc.lock,readonly",
            args.image,
        ).stdout.strip()
        try:
            probe = "from urllib.request import urlopen; assert urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200"
            for _ in range(60):
                result = subprocess.run(
                    ["docker", "exec", cid, "python", "-c", probe], capture_output=True
                )
                if result.returncode == 0:
                    break
                state = command(
                    "docker", "inspect", "--format", "{{.State.Running}}", cid
                ).stdout.strip()
                if state != "true":
                    raise RuntimeError("Backend container stopped before readiness")
                time.sleep(1)
            else:
                raise RuntimeError("Backend did not become ready in 60 seconds")
            predict = """import json, sys
from urllib.request import Request, urlopen
request = Request('http://127.0.0.1:8000/predict', data=sys.argv[1].encode(), headers={'Content-Type': 'application/json'})
result = json.load(urlopen(request, timeout=5))
assert result['prediction'] == 0 and result['default_probability'] == 0.5, result
print('PASS: DVC pull from empty cache, /health and /predict; synthetic model only')
"""
            print(
                command(
                    "docker", "exec", cid, "python", "-c", predict, json.dumps(payload)
                ).stdout.strip()
            )
        except Exception:
            print(command("docker", "logs", cid).stdout, file=sys.stderr)
            raise
        finally:
            command("docker", "rm", "--force", cid)


if __name__ == "__main__":
    main()
