from prometheus_client import Counter, Histogram

PREDICTIONS = Counter(
    "scoring_predictions_total",
    "Total number of successful predictions",
    ["model_version"],
)

INFERENCE_ERRORS = Counter(
    "scoring_inference_errors_total",
    "Total number of inference errors",
    ["model_version"],
)

INFERENCE_LATENCY = Histogram(
    "scoring_inference_duration_seconds",
    "Prediction pipeline duration, including model loading and preprocessing",
    ["model_version"],
    buckets=(
        0.001,
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        float("inf"),
    ),
)
