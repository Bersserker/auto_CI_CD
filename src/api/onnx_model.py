"""CPU serving exported MLP with the exact training feature order."""

import os

import joblib
import numpy as np
import onnxruntime as ort

from src.api.models import CreditRequest

FEATURE_NAMES = list(CreditRequest.model_fields) + [
    "se_ma_2",
    "agebin",
    "avg_exp_5",
    "avg_exp_4",
    "avg_exp_3",
    "avg_exp_2",
    "avg_exp_1",
]


class ONNXModel:
    def __init__(self, model_path, scaler_path):
        self.scaler = joblib.load(scaler_path)
        options = ort.SessionOptions()
        options.intra_op_num_threads = int(os.getenv("ORT_INTRA_OP_THREADS", "0"))
        self.session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        inputs, outputs = self.session.get_inputs(), self.session.get_outputs()
        if (
            len(inputs) != 1
            or inputs[0].name != "features"
            or inputs[0].type != "tensor(float)"
            or len(inputs[0].shape) != 2
            or inputs[0].shape[1] != len(FEATURE_NAMES)
            or self.scaler.n_features_in_ != len(FEATURE_NAMES)
            or len(outputs) != 1
            or outputs[0].name != "default_probability"
        ):
            raise ValueError("ONNX/scaler contract does not match the scoring features")
        self.feature_names_in_ = FEATURE_NAMES

    def probability(self, features):
        values = features[FEATURE_NAMES].to_numpy(dtype=np.float32)
        scaled = self.scaler.transform(values).astype(np.float32)
        if not np.isfinite(scaled).all():
            raise ValueError("Non-finite features after scaling")
        probabilities = self.session.run(["default_probability"], {"features": scaled})[
            0
        ]
        if (
            probabilities.shape != (len(features), 1)
            or not np.isfinite(probabilities).all()
            or ((probabilities < 0) | (probabilities > 1)).any()
        ):
            raise ValueError("Invalid ONNX probabilities")
        return probabilities[:, 0]
