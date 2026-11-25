"""Small MLServer runtime for our exported ONNX probability graph."""

from mlserver import MLModel
from mlserver.codecs import NumpyCodec
from mlserver.types import InferenceRequest, InferenceResponse, ResponseOutput
from mlserver.utils import get_model_uri
import numpy as np
import onnxruntime as ort


class CreditONNX(MLModel):
    async def load(self) -> bool:
        self.session = ort.InferenceSession(
            await get_model_uri(self.settings), providers=["CPUExecutionProvider"]
        )
        return True

    async def predict(self, payload: InferenceRequest) -> InferenceResponse:
        values = NumpyCodec.decode_input(payload.inputs[0]).astype(np.float32)
        result = self.session.run(["default_probability"], {"features": values})[0]
        return InferenceResponse(
            model_name=self.name,
            model_version=self.version,
            outputs=[
                ResponseOutput(
                    name="default_probability",
                    shape=list(result.shape),
                    datatype="FP32",
                    data=result.reshape(-1).tolist(),
                )
            ],
        )
