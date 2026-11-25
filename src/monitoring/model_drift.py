"""Reproducible Evidently data drift and matured-label performance evaluation."""

import argparse
import hashlib
import json
from pathlib import Path

from evidently import DataDefinition, Dataset, Report
from evidently.presets import DataDriftPreset
import numpy as np
import pandas as pd
from sklearn.metrics import log_loss, roc_auc_score
from sklearn.model_selection import train_test_split

from src.api.models import CreditRequest
from src.api.onnx_model import ONNXModel
from src.config import BEST_NN_ONNX_PATH, BEST_NN_SCALER_PATH, PROCESSED_DATA_PATH
from src.features.build_features import build_features

FEATURES = list(CreditRequest.model_fields)
POLICY = dict(
    min_rows=200,
    psi_threshold=0.2,
    drift_share=0.2,
    auc_drop=0.03,
    log_loss_increase=0.05,
    residual_shift=0.1,
    min_group_rows=50,
    min_label_coverage=0.9,
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def attach_outcomes(predictions, outcomes, as_of):
    """Join immutable predictions to externally provided, matured outcomes only."""
    if (
        predictions.request_id.duplicated().any()
        or outcomes.request_id.duplicated().any()
    ):
        raise ValueError("Duplicate request_id")
    if not outcomes.target.isin([0, 1]).all():
        raise ValueError("Targets must be binary")
    if not {"request_id", "predicted_at"} <= set(predictions):
        raise ValueError("Predictions need request_id and predicted_at")
    cutoff = pd.Timestamp(as_of)
    if cutoff.tzinfo is None:
        raise ValueError("as_of must include timezone")
    outcomes = outcomes.copy()
    for column in ("observed_at", "matured_at"):
        outcomes[column] = pd.to_datetime(outcomes[column], utc=True, errors="raise")
    if (outcomes.observed_at < outcomes.matured_at).any():
        raise ValueError("observed_at precedes maturity")
    eligible = outcomes[
        (outcomes.observed_at <= cutoff) & (outcomes.matured_at <= cutoff)
    ]
    joined = predictions.merge(
        eligible, on="request_id", how="left", validate="one_to_one"
    )
    if "predicted_at" in joined:
        if (pd.to_datetime(joined.predicted_at, utc=True) > cutoff).any():
            raise ValueError("Future predictions")
        if (pd.to_datetime(joined.predicted_at, utc=True) > joined.matured_at).any():
            raise ValueError("Outcome matured before prediction")
    if (
        (joined.matured_at - pd.to_datetime(joined.predicted_at, utc=True))
        < pd.Timedelta(days=30)
    ).any():
        raise ValueError("Outcome horizon must be at least 30 days")
    return joined


def quality(frame):
    labels = frame["target"].to_numpy()
    probabilities = frame["probability"].to_numpy()
    if len(frame) < POLICY["min_rows"] or len(np.unique(labels)) != 2:
        return None
    return {
        "roc_auc": float(roc_auc_score(labels, probabilities)),
        "log_loss": float(log_loss(labels, probabilities, labels=[0, 1])),
    }


def evaluate(reference, current, output, *, simulated=False):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if min(len(reference), len(current)) < POLICY["min_rows"]:
        raise ValueError("Insufficient data: require at least 200 rows per window")
    for frame in (reference, current):
        if not np.isfinite(
            frame[FEATURES + ["probability"]].to_numpy(dtype=float)
        ).all():
            raise ValueError("Non-finite features/predictions")
        if not frame.probability.between(0, 1).all():
            raise ValueError("Invalid probabilities")
        if not frame.target.dropna().isin([0, 1]).all():
            raise ValueError("Invalid labels")
    columns = FEATURES + ["probability"]
    definition = DataDefinition(numerical_columns=columns)
    snapshot = Report(
        [
            DataDriftPreset(
                method="psi",
                threshold=POLICY["psi_threshold"],
                drift_share=POLICY["drift_share"],
            )
        ]
    ).run(
        Dataset.from_pandas(current[columns], data_definition=definition),
        Dataset.from_pandas(reference[columns], data_definition=definition),
    )
    snapshot.save_html(str(output / "evidently.html"))
    snapshot.save_json(str(output / "evidently.json"))
    metrics = snapshot.dict()["metrics"]
    per_column = {
        m["config"]["column"]: float(m["value"])
        for m in metrics
        if m["config"]["type"].endswith(":ValueDrift")
    }
    feature_share = sum(
        per_column[c] >= POLICY["psi_threshold"] for c in FEATURES
    ) / len(FEATURES)
    ref_quality = (
        quality(reference.dropna(subset=["target"]))
        if reference.target.notna().mean() >= POLICY["min_label_coverage"]
        else None
    )
    coverage = float(current.target.notna().mean())
    current_quality = (
        quality(current.dropna(subset=["target"]))
        if coverage >= POLICY["min_label_coverage"]
        else None
    )
    decay = None
    groups = {}
    if ref_quality and current_quality:
        decay = (
            ref_quality["roc_auc"] - current_quality["roc_auc"] > POLICY["auc_drop"]
            or current_quality["log_loss"] - ref_quality["log_loss"]
            > POLICY["log_loss_increase"]
        )
        for group in (0, 1):
            a = reference[(reference.pay_0 > 0).astype(int) == group].dropna(
                subset=["target"]
            )
            b = current[(current.pay_0 > 0).astype(int) == group].dropna(
                subset=["target"]
            )
            if min(len(a), len(b)) >= POLICY["min_group_rows"]:
                groups[str(group)] = float(
                    abs(
                        (b.target - b.probability).mean()
                        - (a.target - a.probability).mean()
                    )
                )
    result = dict(
        simulated=simulated,
        reference_rows=len(reference),
        current_rows=len(current),
        labelled_rows=int(current.target.notna().sum()),
        label_coverage=coverage,
        policy=POLICY,
        feature_psi=per_column,
        drift_share=feature_share,
        data_drift=feature_share >= POLICY["drift_share"],
        reference_quality=ref_quality,
        current_quality=current_quality,
        performance_decay=decay,
        concept_drift_suspected=(
            None
            if decay is None
            else bool(
                decay
                and feature_share < POLICY["drift_share"]
                and any(v > POLICY["residual_shift"] for v in groups.values())
            )
        ),
        residual_shift=groups,
    )
    (output / "summary.json").write_text(
        json.dumps(result, indent=2, allow_nan=False) + "\n"
    )
    gauges = {
        "scoring_data_drift_share": feature_share,
        "scoring_data_drift_detected": int(result["data_drift"]),
        "scoring_labelled_rows": result["labelled_rows"],
        "scoring_quality_available": int(current_quality is not None),
    }
    if decay is not None:
        gauges.update(
            scoring_performance_decay=int(decay),
            scoring_concept_drift_suspected=int(result["concept_drift_suspected"]),
            scoring_roc_auc=current_quality["roc_auc"],
            scoring_log_loss=current_quality["log_loss"],
        )
    (output / "metrics.prom").write_text(
        "".join(f"{key} {value}\n" for key, value in gauges.items())
    )
    return result


def demo(directory):
    """Historical held-out windows; shifts/labels explicitly simulated, seed=42."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    frame = pd.read_csv(PROCESSED_DATA_PATH)
    _, heldout = train_test_split(
        frame, test_size=0.2, random_state=42, stratify=frame.default
    )
    reference = heldout.sample(n=2000, random_state=42).copy()
    current = heldout.drop(reference.index).sample(n=1000, random_state=43).copy()
    model = ONNXModel(BEST_NN_ONNX_PATH, BEST_NN_SCALER_PATH)

    def score(data):
        data = data.copy()
        data["probability"] = model.probability(build_features(data[FEATURES]))
        data["target"] = data["default"]
        return data

    reference = score(reference)
    reference_path = directory / "reference.csv"
    manifest = dict(
        seed=42,
        data_sha256=sha(PROCESSED_DATA_PATH),
        model_sha256=sha(BEST_NN_ONNX_PATH),
        scaler_sha256=sha(BEST_NN_SCALER_PATH),
        reference_ids=reference.id.astype(int).tolist(),
        current_ids=current.id.astype(int).tolist(),
        policy=POLICY,
        simulated=True,
    )
    if (directory / "manifest.json").exists():
        if json.loads((directory / "manifest.json").read_text()) != manifest:
            raise ValueError("Reference provenance changed; use a new output directory")
        if sha(reference_path) != sha_bytes(reference.to_csv(index=False).encode()):
            raise ValueError("Reference artifact changed")
    else:
        reference.to_csv(reference_path, index=False)
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    baseline = score(current)
    shifted = current.copy()
    for feature in FEATURES:
        if feature.startswith("pay_") and not feature.startswith("pay_amt"):
            shifted[feature] += 3
        elif feature.startswith(("bill_amt", "pay_amt")) or feature == "limit_bal":
            shifted[feature] *= 3
    shifted = score(shifted)
    concept = baseline.copy()
    # Same P(X), deliberately inverted P(y|X) for a reproducible incident.
    concept["target"] = 1 - (concept.probability >= 0.5).astype(int)
    for name, batch in [
        ("baseline", baseline),
        ("data-drift", shifted),
        ("concept-drift", concept),
    ]:
        batch.to_csv(directory / f"{name}.csv", index=False)
        result = evaluate(reference, batch, directory / name, simulated=True)
        print(
            name,
            {
                k: result[k]
                for k in ("data_drift", "performance_decay", "concept_drift_suspected")
            },
            flush=True,
        )


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/stage6/drift"))
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--current", type=Path)
    parser.add_argument("--outcomes", type=Path)
    parser.add_argument("--as-of")
    parser.add_argument(
        "--window-end", help="UTC end of 7-day prediction cohort; defaults to as-of"
    )
    args = parser.parse_args()
    if args.reference or args.current:
        if not (args.reference and args.current):
            parser.error("Provide both --reference and --current")
        current = pd.read_csv(args.current)
        if not args.as_of or "predicted_at" not in current:
            parser.error("Production evaluation requires --as-of and predicted_at")
        cutoff = pd.Timestamp(args.as_of)
        end = pd.Timestamp(args.window_end or args.as_of)
        if cutoff.tzinfo is None or end.tzinfo is None or end > cutoff:
            parser.error(
                "Use timezone-aware window/as-of timestamps; window cannot end in future"
            )
        current["predicted_at"] = pd.to_datetime(
            current.predicted_at, utc=True, errors="raise"
        )
        current = current[
            (current.predicted_at > end - pd.Timedelta(days=7))
            & (current.predicted_at <= end)
        ]
        if "target" in current:
            parser.error(
                "Provide external matured targets through --outcomes, not current CSV"
            )
        if args.outcomes:
            if not args.as_of:
                parser.error("--outcomes requires --as-of")
            current = attach_outcomes(current, pd.read_csv(args.outcomes), args.as_of)
        else:
            current["target"] = np.nan
        evaluate(pd.read_csv(args.reference), current, args.output)
    else:
        demo(args.output)


if __name__ == "__main__":
    main()
