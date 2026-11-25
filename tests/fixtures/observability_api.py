from pathlib import Path

import uvicorn

from src.api import app as api

original_load_model = api.load_model


def load():
    if Path("/tmp/scoring-fail").exists():
        raise api.HTTPException(status_code=503, detail="Simulated incident")
    return original_load_model()


api.load_model = load
uvicorn.run(api.app, host="0.0.0.0", port=8000)
