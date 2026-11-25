"""Isolated NN retraining, locked holdout, ONNX parity and fail-closed release gates."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
from time import perf_counter

import joblib
import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper
import onnxruntime as ort
import pandas as pd
from sklearn.metrics import log_loss, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler

from src.api.models import CreditRequest
from src.api.onnx_model import FEATURE_NAMES, ONNXModel
from src.config import BEST_NN_ONNX_PATH, BEST_NN_SCALER_PATH, PROCESSED_DATA_PATH
from src.features.build_features import build_features

ROOT = Path("artifacts/stage7")
POLICY = {
    "min_auc": 0.65,
    "max_auc_drop": 0.01,
    "max_loss_increase": 0.03,
    "max_parity_error": 1e-5,
    "max_latency_seconds": 0.1,
    "min_holdout": 1000,
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, data):
    temporary = Path(str(path) + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def run_dir(run_id):
    if not re.fullmatch("[a-z0-9][a-z0-9-]{0,63}", run_id):
        raise ValueError("Invalid run ID")
    folder = ROOT / "runs" / run_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def validate_rows(frame):
    if (
        frame.id.isna().any()
        or frame.id.duplicated().any()
        or not frame.target.isin([0, 1]).all()
    ):
        raise ValueError("Duplicate IDs or invalid target")
    for item in frame[list(CreditRequest.model_fields)].to_dict("records"):
        try:
            CreditRequest.model_validate(item)
        except ValueError as exc:
            raise ValueError(
                "Invalid input row; inspect the protected source batch"
            ) from exc
    features = build_features(frame[list(CreditRequest.model_fields)])[FEATURE_NAMES]
    if not np.isfinite(features.to_numpy(dtype=float)).all():
        raise ValueError("Non-finite engineered features")
    return features


def prepare(run_id, conf):
    folder = run_dir(run_id)
    if (folder / "prepared.json").exists():
        return json.loads((folder / "prepared.json").read_text())
    history = pd.read_csv(PROCESSED_DATA_PATH).rename(columns={"default": "target"})
    validate_rows(history)
    train, holdout = train_test_split(
        history, test_size=0.2, random_state=42, stratify=history.target
    )
    if conf.get("batch"):
        name = conf["batch"]
        if not re.fullmatch("[a-zA-Z0-9_-]+[.]csv", name):
            raise ValueError("Invalid inbox file name")
        path = ROOT / "inbox" / name
        if path.resolve().parent != (ROOT / "inbox").resolve():
            raise ValueError("Inbox symlinks are not supported")
        if (
            conf.get("event_id")
            and conf["event_id"]
            != hashlib.sha256(("new_data" + sha(path)).encode()).hexdigest()[:24]
        ):
            raise ValueError("Event source changed after detection")
        new = pd.read_csv(path)
        validate_rows(new)
        if set(new.id) & set(holdout.id):
            raise ValueError("Holdout leakage: inbox contains evaluation IDs")
        if not conf.get("demo", False):
            for col in ("predicted_at", "matured_at", "observed_at"):
                new[col] = pd.to_datetime(new[col], utc=True, errors="raise")
                if new[col].isna().any():
                    raise ValueError("Missing maturity timestamps")
            asof = pd.Timestamp(
                conf.get("as_of") or datetime.now(timezone.utc).isoformat()
            )
            if (
                asof.tzinfo is None
                or asof > pd.Timestamp.now(tz="UTC")
                or (
                    (new.observed_at < new.matured_at)
                    | (new.observed_at > asof)
                    | (new.matured_at - new.predicted_at < pd.Timedelta(days=30))
                ).any()
            ):
                raise ValueError("Only observed, matured 30-day outcomes may train")
        train = pd.concat([train, new]).drop_duplicates("id", keep="last")
    if conf.get("trigger") == "drift":
        report = (
            Path("artifacts/stage6/drift")
            / ("concept-drift" if conf.get("demo") else "production")
            / "summary.json"
        )
        if (
            conf.get("event_id")
            and conf["event_id"]
            != hashlib.sha256(("drift" + sha(report)).encode()).hexdigest()[:24]
        ):
            raise ValueError("Drift evidence changed after detection")
        signal = json.loads(report.read_text())
        if signal.get("simulated") and not conf.get("demo"):
            raise ValueError("Simulated drift cannot trigger production retraining")
        if not (
            signal.get("data_drift")
            or signal.get("performance_decay")
            or signal.get("concept_drift_suspected")
        ):
            raise ValueError("No drift trigger")
    train.to_csv(folder / "train.csv", index=False)
    holdout.to_csv(folder / "holdout.csv", index=False)
    info = {
        "run_id": run_id,
        "trigger": conf.get("trigger", "schedule"),
        "simulated": bool(conf.get("demo")) or os.getenv("STAGE7_LOCAL_ONLY") == "1",
        "event_id": conf.get("event_id"),
        "data_sha256": sha(PROCESSED_DATA_PATH),
        "train_sha256": sha(folder / "train.csv"),
        "holdout_sha256": sha(folder / "holdout.csv"),
        "champion_sha256": sha(BEST_NN_ONNX_PATH),
        "champion_scaler_sha256": sha(BEST_NN_SCALER_PATH),
        "code_sha256": sha(__file__),
        "train_rows": len(train),
        "holdout_rows": len(holdout),
        "policy": POLICY,
    }
    write(folder / "prepared.json", info)
    return info


def export_mlp(model, path):
    nodes = []
    weights = []
    previous = "features"
    for i, (coef, bias) in enumerate(zip(model.coefs_, model.intercepts_)):
        weights += [
            numpy_helper.from_array(coef.astype(np.float32), f"w{i}"),
            numpy_helper.from_array(bias.astype(np.float32), f"b{i}"),
        ]
        nodes.extend(
            [
                helper.make_node("MatMul", [previous, f"w{i}"], [f"z{i}"]),
                helper.make_node("Add", [f"z{i}", f"b{i}"], [f"a{i}"]),
            ]
        )
        last = i == len(model.coefs_) - 1
        out = "default_probability" if last else f"h{i}"
        nodes.append(helper.make_node("Sigmoid" if last else "Relu", [f"a{i}"], [out]))
        previous = out
    graph = helper.make_graph(
        nodes,
        "retrained_credit_mlp",
        [
            helper.make_tensor_value_info(
                "features", TensorProto.FLOAT, ["batch", len(FEATURE_NAMES)]
            )
        ],
        [
            helper.make_tensor_value_info(
                "default_probability", TensorProto.FLOAT, ["batch", 1]
            )
        ],
        weights,
    )
    result = helper.make_model(
        graph, opset_imports=[helper.make_opsetid("", 18)], ir_version=10
    )
    onnx.checker.check_model(result)
    onnx.save(result, path)


def train(run_id):
    folder = run_dir(run_id)
    info = json.loads((folder / "prepared.json").read_text())
    if sha(folder / "train.csv") != info["train_sha256"]:
        raise ValueError("Training snapshot changed")
    frame = pd.read_csv(folder / "train.csv")
    features = validate_rows(frame).to_numpy(dtype=np.float32)
    scaler = StandardScaler().fit(features)
    model = MLPClassifier(
        hidden_layer_sizes=(128, 64),
        max_iter=30,
        early_stopping=True,
        n_iter_no_change=5,
        validation_fraction=0.15,
        random_state=42,
        batch_size=256,
        alpha=0.01,
    )
    model.fit(scaler.transform(features).astype(np.float32), frame.target)
    candidate = folder / "models"
    candidate.mkdir(exist_ok=True)
    joblib.dump(scaler, candidate / "best_scaler.joblib")
    joblib.dump(model, folder / "mlp.joblib")
    export_mlp(model, candidate / "credit_default_model.onnx")
    trained = {
        "model_sha256": sha(candidate / "credit_default_model.onnx"),
        "scaler_sha256": sha(candidate / "best_scaler.joblib"),
        "architecture": "MLP 30-128-64-1 ReLU/Sigmoid (sklearn, no BatchNorm/dropout)",
        "iterations": int(model.n_iter_),
    }
    write(folder / "trained.json", trained)
    return trained


def gate(metrics):
    return bool(
        metrics["rows"] >= POLICY["min_holdout"]
        and metrics["parity_error"] <= POLICY["max_parity_error"]
        and metrics["candidate_auc"] >= POLICY["min_auc"]
        and metrics["baseline_auc"] - metrics["candidate_auc"] <= POLICY["max_auc_drop"]
        and metrics["candidate_log_loss"] - metrics["baseline_log_loss"]
        <= POLICY["max_loss_increase"]
        and metrics["latency_seconds"] <= POLICY["max_latency_seconds"]
    )


def validate(run_id):
    folder = run_dir(run_id)
    info = json.loads((folder / "prepared.json").read_text())
    trained = json.loads((folder / "trained.json").read_text())
    if (
        sha(folder / "holdout.csv") != info["holdout_sha256"]
        or sha(BEST_NN_ONNX_PATH) != info["champion_sha256"]
        or sha(BEST_NN_SCALER_PATH) != info["champion_scaler_sha256"]
    ):
        raise ValueError("Evaluation/champion provenance changed")
    model_path = folder / "models/credit_default_model.onnx"
    scaler_path = folder / "models/best_scaler.joblib"
    if (
        sha(model_path) != trained["model_sha256"]
        or sha(scaler_path) != trained["scaler_sha256"]
    ):
        raise ValueError("Candidate artifacts changed")
    frame = pd.read_csv(folder / "holdout.csv")
    features = validate_rows(frame)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    model = ONNXModel(model_path, scaler_path)
    baseline = ONNXModel(BEST_NN_ONNX_PATH, BEST_NN_SCALER_PATH)
    candidate = model.probability(features)
    original = joblib.load(folder / "mlp.joblib").predict_proba(
        model.scaler.transform(features.to_numpy(dtype=np.float32)).astype(np.float32)
    )[:, 1]
    champion = baseline.probability(features)
    durations = []
    for _ in range(20):
        start = perf_counter()
        model.probability(features.iloc[:1])
        durations.append(perf_counter() - start)
    result = {
        "rows": len(frame),
        "candidate_auc": float(roc_auc_score(frame.target, candidate)),
        "baseline_auc": float(roc_auc_score(frame.target, champion)),
        "candidate_log_loss": float(log_loss(frame.target, candidate)),
        "baseline_log_loss": float(log_loss(frame.target, champion)),
        "parity_error": float(np.max(np.abs(candidate - original))),
        "latency_seconds": float(np.quantile(durations, 0.95)),
    }
    result["passed"] = gate(result)
    write(folder / "gate.json", result)
    return result


def verify(folder):
    folder = Path(folder)
    info = json.loads((folder / "prepared.json").read_text())
    result = json.loads((folder / "gate.json").read_text())
    trained = json.loads((folder / "trained.json").read_text())
    if not result.get("passed") or not gate(result) or info["policy"] != POLICY:
        raise ValueError("Candidate rejected by release gate")
    if (
        sha(folder / "models/credit_default_model.onnx") != trained["model_sha256"]
        or sha(folder / "models/best_scaler.joblib") != trained["scaler_sha256"]
    ):
        raise ValueError("Candidate bundle integrity failure")
    return info, trained, result


def publish(run_id):
    folder = run_dir(run_id)
    info, trained, result = verify(folder)
    name = "credit-" + run_id
    repository = Path("artifacts/stage6/repository") / name
    if repository.exists():
        if sha(repository / "model.onnx") != trained["model_sha256"]:
            raise ValueError("Immutable Seldon version conflict")
    else:
        repository.mkdir(parents=True)
        shutil.copyfile(
            folder / "models/credit_default_model.onnx", repository / "model.onnx"
        )
        shutil.copyfile("docker/seldon/runtime.py", repository / "runtime.py")
        write(
            repository / "model-settings.json",
            {
                "name": name,
                "implementation": "runtime.CreditONNX",
                "parameters": {"uri": "./model.onnx", "version": "v1"},
            },
        )
    manifest = {
        "apiVersion": "mlops.seldon.io/v1alpha1",
        "kind": "Model",
        "metadata": {"name": name},
        "spec": {"storageUri": "/repository/" + name, "requirements": ["onnx"]},
    }
    # JSON is valid YAML, no shell interpretation or arbitrary CLI arguments.
    write(folder / "seldon.json", manifest)
    cli = os.getenv("SELDON_CLI", "artifacts/stage6/tools/seldon")
    scheduler = os.getenv("SELDON_SCHEDULER", "localhost:19004")
    subprocess.run(
        [
            cli,
            "model",
            "load",
            "--force",
            "--scheduler-host",
            scheduler,
            "-f",
            str(folder / "seldon.json"),
        ],
        check=True,
        timeout=30,
    )
    status = subprocess.check_output(
        [
            cli,
            "model",
            "status",
            "--scheduler-host",
            scheduler,
            name,
            "-w",
            "ModelAvailable",
            "-t",
            "120",
        ],
        text=True,
        timeout=130,
    )
    if not (folder / "registration.json").exists():
        write(
            folder / "registration.json",
            {"model_name": name, "state": "ModelAvailable", "response": status},
        )
    bundle = folder / "candidate.tar.gz"
    if not bundle.exists():
        with tarfile.open(bundle, "w:gz") as archive:
            for item in [
                "models",
                "prepared.json",
                "trained.json",
                "gate.json",
                "registration.json",
            ]:
                archive.add(folder / item, arcname=item)
    delivery = {
        "run_id": run_id,
        "model_name": name,
        "bundle_sha256": sha(bundle),
        "model_sha256": trained["model_sha256"],
        "stage": "candidate",
        "simulated": info["simulated"],
        "automatic_production_promotion": False,
        "ci_workflow": "model-candidate.yml",
    }
    if (folder / "delivery.json").exists():
        if json.loads((folder / "delivery.json").read_text()) != delivery:
            raise ValueError("Immutable delivery metadata changed")
    else:
        write(folder / "delivery.json", delivery)
    if os.getenv("MODEL_CANDIDATE_BUCKET") and not info["simulated"]:
        import boto3

        endpoint = os.getenv("MODEL_S3_ENDPOINT")
        if endpoint and not endpoint.startswith("https://"):
            raise ValueError("HTTPS artifact endpoint required")
        client = boto3.client("s3", endpoint_url=endpoint)
        for artifact in ("candidate.tar.gz", "delivery.json"):
            client.upload_file(
                str(folder / artifact),
                os.environ["MODEL_CANDIDATE_BUCKET"],
                f"candidates/{run_id}/{artifact}",
            )
    if os.getenv("MODEL_CI_TOKEN") and not info["simulated"]:
        from urllib.request import Request, urlopen

        repository = os.environ["MODEL_CI_REPOSITORY"]
        if not re.fullmatch("[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid MODEL_CI_REPOSITORY")
        if not os.getenv("MODEL_CANDIDATE_BUCKET"):
            raise ValueError("CI dispatch requires an uploaded candidate bundle")
        request = Request(
            f"https://api.github.com/repos/{repository}/actions/workflows/model-candidate.yml/dispatches",
            data=json.dumps(
                {"ref": os.environ["MODEL_CI_REF"], "inputs": {"candidate_id": run_id}}
            ).encode(),
            headers={
                "Authorization": "Bearer " + os.environ["MODEL_CI_TOKEN"],
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=20) as response:
            if response.status != 204:
                raise ValueError("Unexpected CI dispatch result")
    return delivery


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--stage",
        choices=["prepare", "train", "validate", "publish", "verify"],
        required=True,
    )
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    if args.stage == "prepare":
        result = prepare(args.run_id, {"demo": args.demo, "trigger": "schedule"})
    elif args.stage == "verify":
        result = verify(run_dir(args.run_id))[2]
    else:
        result = globals()[args.stage](args.run_id)
    print(json.dumps(result, indent=2))
    if args.stage == "validate" and not result["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
