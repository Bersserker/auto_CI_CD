"""Настоящие DVC push/pull в локальный remote; аккаунт облака не требуется."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def deployment(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / ".dvc").mkdir()
    (app / ".dvc/config").write_text(
        "[core]\n    no_scm = true\n    analytics = false\n[cache]\n    type = copy\n"
    )
    (app / "models").mkdir()
    for folder, name in [
        ("scripts", "configure_dvc_remote.py"),
        ("docker", "backend-entrypoint.py"),
    ]:
        (app / folder).mkdir()
        shutil.copyfile(ROOT / folder / name, app / folder / name)
    env = {"PATH": os.environ["PATH"]}
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    env["DVC_PULL"] = "1"
    env["DVC_REMOTE_URL"] = str(tmp_path / "remote")
    env["DVC_NO_ANALYTICS"] = "1"
    env["DVC_SITE_CACHE_DIR"] = str(tmp_path / "site-cache")
    # Доступ к SCM/глобальным настройкам не нужен этой изолированной копии.
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir()
    return app, env


def run(app, env, *args):
    return subprocess.run(
        args, cwd=app, env=env, text=True, capture_output=True, check=True
    )


def publish(app, env, contents):
    (app / "models/best_model.joblib").write_bytes(contents)
    (app / "models/reference.csv").write_text("test-reference\n")
    (app / "dvc.yaml").write_text(
        "stages:\n  train:\n    cmd: python train.py\n    outs:\n      - models/best_model.joblib\n      - models/reference.csv\n"
    )
    run(app, env, "dvc", "commit", "--force", "train")
    run(app, env, sys.executable, "scripts/configure_dvc_remote.py")
    run(app, env, "dvc", "push", "models/best_model.joblib")


def start(app, env):
    return subprocess.run(
        [
            sys.executable,
            "docker/backend-entrypoint.py",
            sys.executable,
            "-c",
            "from pathlib import Path; Path('started').write_text('yes')",
        ],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
    )


def test_download_from_empty_cache_before_start(deployment):
    app, env = deployment
    publish(app, env, b"version-one-test-artifact")
    (app / "models/best_model.joblib").unlink()
    (app / "models/reference.csv").unlink()
    shutil.rmtree(app / ".dvc/cache")
    result = start(app, env)
    assert result.returncode == 0, result.stderr
    assert (
        app / "models/best_model.joblib"
    ).read_bytes() == b"version-one-test-artifact"
    assert not (app / "models/reference.csv").exists()
    assert (app / "started").is_file()


def test_new_lock_replaces_stale_model(deployment):
    app, env = deployment
    publish(app, env, b"old-test-artifact")
    publish(app, env, b"new-test-artifact")
    shutil.rmtree(app / ".dvc/cache")
    (app / "models/best_model.joblib").write_bytes(b"old-test-artifact")
    result = start(app, env)
    assert result.returncode == 0, result.stderr
    assert (app / "models/best_model.joblib").read_bytes() == b"new-test-artifact"


def test_failed_pull_never_starts_with_stale_model(deployment):
    app, env = deployment
    publish(app, env, b"old-test-artifact")
    publish(app, env, b"new-test-artifact")
    (app / "models/best_model.joblib").write_bytes(b"old-test-artifact")
    shutil.rmtree(app / ".dvc/cache")
    env["DVC_REMOTE_URL"] = str(app / "missing-remote")
    result = start(app, env)
    assert result.returncode != 0
    assert not (app / "started").exists()


@pytest.mark.parametrize("mode", ["0", "incorrect"])
def test_missing_model_blocks_start(deployment, mode):
    app, env = deployment
    env["DVC_PULL"] = mode
    result = start(app, env)
    assert result.returncode != 0
    assert not (app / "started").exists()


def test_explicit_local_model_mode(deployment):
    app, env = deployment
    (app / "models/best_model.joblib").write_bytes(b"mounted-test-artifact")
    env["DVC_PULL"] = "0"
    result = start(app, env)
    assert result.returncode == 0, result.stderr
    assert (app / "started").exists()


def test_s3_configuration_contains_no_credentials(deployment):
    app, env = deployment
    env["DVC_REMOTE_URL"] = "s3://example-artifacts/dvc"
    env["AWS_ACCESS_KEY_ID"] = "fake-test-access-key"
    env["AWS_SECRET_ACCESS_KEY"] = "fake-test-secret-key"
    run(app, env, sys.executable, "scripts/configure_dvc_remote.py")
    local = (app / ".dvc/config.local").read_text()
    assert "https://storage.yandexcloud.net" in local
    assert "ru-central1" in local
    assert env["AWS_ACCESS_KEY_ID"] not in local
    assert env["AWS_SECRET_ACCESS_KEY"] not in local


def test_empty_remote_blocks_start(deployment):
    app, env = deployment
    env.pop("DVC_REMOTE_URL")
    result = start(app, env)
    assert result.returncode != 0
    assert not (app / "started").exists()
