"""Verify a trusted training bundle and build a staging image; never promote in place."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

import numpy as np
import pandas as pd

from src.api.models import CreditRequest
from src.api.onnx_model import ONNXModel
from src.features.build_features import build_features
from src.retraining.pipeline import ROOT, sha, verify


def extract(bundle, directory):
    with tarfile.open(bundle, "r:gz") as archive:
        members = archive.getmembers()
        if sum(m.size for m in members) > 20_000_000 or len(members) > 20:
            raise ValueError("Oversized candidate bundle")
        allowed = {
            "models",
            "models/best_scaler.joblib",
            "models/credit_default_model.onnx",
            "prepared.json",
            "trained.json",
            "gate.json",
            "registration.json",
        }
        if any(m.name not in allowed or m.issym() or m.islnk() for m in members):
            raise ValueError("Unexpected bundle contents")
        archive.extractall(directory, filter="data")


def check(directory, allow_simulated):
    info, trained, gate = verify(directory)
    if info["simulated"] and not allow_simulated:
        raise ValueError("Simulated retraining cannot produce a production release")
    if (
        json.loads((directory / "registration.json").read_text())["state"]
        != "ModelAvailable"
    ):
        raise ValueError("Candidate has not passed Seldon registration")
    model = ONNXModel(
        directory / "models/credit_default_model.onnx",
        directory / "models/best_scaler.joblib",
    )
    payload = {key: 0 for key in CreditRequest.model_fields}
    payload.update(sex=1, age=30, limit_bal=100000)
    assert np.isfinite(model.probability(build_features(pd.DataFrame([payload])))).all()
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--allow-simulated", action="store_true")
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch("[a-z0-9][a-z0-9-]{0,63}", args.run_id):
        parser.error("Invalid run ID")
    folder = ROOT / "runs" / args.run_id
    if args.fetch:
        import os

        import boto3

        folder.mkdir(parents=True, exist_ok=True)
        bucket = os.environ["MODEL_CANDIDATE_BUCKET"]
        endpoint = os.environ.get("MODEL_S3_ENDPOINT")
        if endpoint and not endpoint.startswith("https://"):
            raise ValueError("HTTPS endpoint required")
        client = boto3.client("s3", endpoint_url=endpoint)
        for item in ("delivery.json", "candidate.tar.gz"):
            client.download_file(
                bucket, f"candidates/{args.run_id}/{item}", str(folder / item)
            )
    delivery = json.loads((folder / "delivery.json").read_text())
    if sha(folder / "candidate.tar.gz") != delivery["bundle_sha256"]:
        raise ValueError("Bundle checksum mismatch")
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        extract(folder / "candidate.tar.gz", directory)
        info = check(directory, args.allow_simulated)
        if info["run_id"] != args.run_id:
            raise ValueError("Wrong run in candidate bundle")
        if args.build:
            (directory / "Dockerfile").write_text(f"""FROM credit-backend:stage7
COPY --chown=app:app models /app/models
ENV MODEL_BACKEND=onnx DVC_PULL=0 MODEL_VERSION={args.run_id}
""")
            subprocess.run(
                [
                    "docker",
                    "build",
                    "-t",
                    f"credit-backend:candidate-{args.run_id}",
                    str(directory),
                ],
                check=True,
            )
    print("Candidate verified; current production model was not modified.")


if __name__ == "__main__":
    main()
