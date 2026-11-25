"""Verify actual Seldon A/B and paired shadow predictions against local ONNX."""

from collections import Counter
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from src.api.models import CreditRequest
from src.api.onnx_model import ONNXModel
from src.config import (
    BEST_NN_ONNX_PATH,
    BEST_NN_SCALER_PATH,
    MODELS_DIR,
    PROCESSED_DATA_PATH,
)
from src.features.build_features import build_features
from src.monitoring.model_drift import quality


def main():
    from sklearn.model_selection import train_test_split

    full = pd.read_csv(PROCESSED_DATA_PATH)
    _, heldout = train_test_split(
        full, test_size=0.2, random_state=42, stratify=full.default
    )
    frame = heldout.sample(n=200, random_state=46).reset_index(drop=True)
    models = {
        name: ONNXModel(path, BEST_NN_SCALER_PATH)
        for name, path in [
            ("credit-fp32", BEST_NN_ONNX_PATH),
            ("credit-int8", MODELS_DIR / "credit_default_model_quant_dyn.onnx"),
        ]
    }
    expected = {
        name: np.concatenate(
            [
                model.probability(
                    build_features(frame.iloc[[i]][list(CreditRequest.model_fields)])
                )
                for i in range(len(frame))
            ]
        )
        for name, model in models.items()
    }
    run_id = str(time.time_ns())
    counts = Counter()
    for mode in ("ab", "shadow"):
        for index, row in frame.iterrows():
            sample = CreditRequest.model_validate(
                row[list(CreditRequest.model_fields)].to_dict()
            ).model_dump()
            request_id = f"{run_id}-{mode}-{index}"
            request = Request(
                f"http://localhost:18080/predict?mode={mode}",
                data=json.dumps(sample).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Request-ID": request_id,
                },
            )
            with urlopen(request, timeout=20) as response:
                result = json.load(response)
            assert result["request_id"] == request_id
            name = result["model_version"]
            if mode == "shadow":
                assert name == "credit-fp32"
            assert (
                abs(result["default_probability"] - float(expected[name][index])) < 1e-5
            )
            counts[f"{mode}:{name}"] += 1
    assert counts["ab:credit-fp32"] > 50 and counts["ab:credit-int8"] > 50
    journal = Path("artifacts/stage6/records/predictions.jsonl")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        entries = [
            json.loads(line)
            for line in journal.read_text().splitlines()
            if line.strip()
        ]
        pairs = [
            row
            for row in entries
            if row["request_id"].startswith(run_id + "-shadow-")
            and row["model"] == "credit-int8"
        ]
        if len(pairs) == len(frame):
            break
        time.sleep(0.2)
    assert len(pairs) == len(frame) and all(row["success"] for row in pairs)
    quality_by_model = {
        name: quality(pd.DataFrame({"target": frame.default, "probability": p}))
        for name, p in expected.items()
    }
    differences = np.abs(expected["credit-fp32"] - expected["credit-int8"])
    auc_drop = (
        quality_by_model["credit-fp32"]["roc_auc"]
        - quality_by_model["credit-int8"]["roc_auc"]
    )
    result = dict(
        run_id=run_id,
        counts=dict(counts),
        paired_shadow=len(pairs),
        max_probability_delta=float(differences.max()),
        quality=quality_by_model,
        candidate_offline_gate_passed=bool(
            auc_drop <= 0.01 and differences.max() <= 0.01
        ),
        automatic_promotion=False,
        note="Historical sample: compatibility gate only; no causal online efficacy conclusion",
    )
    Path("artifacts/stage6/comparison.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
