#################################################################################
# GLOBALS                                                                       #
#################################################################################

PROJECT_NAME = teplayakov_an_mlops_project
PYTHON_VERSION = 3.13
PYTHON_INTERPRETER = python
APP_HOST ?= 127.0.0.1
APP_PORT ?= 8000
MLFLOW_HOST ?= 127.0.0.1
MLFLOW_PORT ?= 5000
MLFLOW_BACKEND_STORE_URI ?= sqlite:///artifacts/mlflow/mlflow.db
MLFLOW_ARTIFACTS_DESTINATION ?= $(CURDIR)/artifacts/mlflow/mlartifacts
MLFLOW_TRACKING_URI ?= http://127.0.0.1:$(MLFLOW_PORT)
BENCHMARK_ARGS ?=
BENCHMARK_GPU_DIR ?= /tmp/mlops-ort-gpu

#################################################################################
# COMMANDS                                                                      #
#################################################################################


## Install Python dependencies
.PHONY: requirements
requirements:
	uv sync
	



## Delete all compiled Python files
.PHONY: clean
clean:
	find . -type f -name "*.py[co]" -delete
	find . -type d -name "__pycache__" -delete


## Lint using flake8, black, and isort (use `make format` to do formatting)
.PHONY: lint
lint:
	uv run flake8 src tests
	uv run isort --check --diff src tests
	uv run black --check src tests

## Format source code with isort and black
.PHONY: format
format:
	uv run isort src tests
	uv run black src tests



## Run tests
.PHONY: test
test:
	python -m pytest tests


## Set up Python interpreter environment
.PHONY: create_environment
create_environment:
	uv venv --python $(PYTHON_VERSION)
	@echo ">>> New uv virtual environment created. Activate with:"
	@echo ">>> Windows: .\\\\.venv\\\\Scripts\\\\activate"
	@echo ">>> Unix/macOS: source ./.venv/bin/activate"
	



#################################################################################
# PROJECT RULES                                                                 #
#################################################################################

## Download dataset into data/raw
.PHONY: data
data:
	uv run python -m src.data.make_dataset

## validate and clean the dataset into data/processed
.PHONY: prepare-data 
prepare-data:
	uv run python -m src.data.prepare_dataset

## Train and register the best credit default model
.PHONY: train
train:
	uv run python -m src.model_training.train

## Train neural network, export and validate ONNX, quantize and validate INT8
.PHONY: train-nn
train-nn:
	mkdir -p models reports
	uv run python -m src.neural.train_nn
	uv run python -m src.neural.export_onnx
	uv run python -m src.neural.validate_onnx
	uv run python -m src.neural.quantize_onnx

## Benchmark PyTorch FP32, ONNX FP32 and INT8 on CPU and available GPU
.PHONY: benchmark-nn
benchmark-nn:
	uv run python -m src.neural.benchmark $(BENCHMARK_ARGS)

## Benchmark CPU/CUDA using ONNX Runtime GPU installed in a separate directory
.PHONY: benchmark-nn-gpu
benchmark-nn-gpu:
	uv pip install --target "$(BENCHMARK_GPU_DIR)" --no-deps "onnxruntime-gpu==1.32.0" "nvidia-curand==10.4.5.25"
	LD_LIBRARY_PATH="$(BENCHMARK_GPU_DIR)/nvidia/cu13/lib:$${LD_LIBRARY_PATH:-}" \
		PYTHONPATH="$(BENCHMARK_GPU_DIR):$(CURDIR)$${PYTHONPATH:+:$$PYTHONPATH}" \
		uv run python -m src.neural.benchmark $(BENCHMARK_ARGS)

## Start FastAPI server (localhost:8000 by default)
.PHONY: api
api:
	uv run uvicorn src.api.app:app --host "$(APP_HOST)" --port "$(APP_PORT)"

## Send sample requests to the running API
.PHONY: test-api test_my_api test_api.py
test-api:
	uv run python -m src.api.test_api --port "$(APP_PORT)"

test_my_api test_api.py: test-api

## PSI test on test
.PHONY: psi_test
psi_test:
	uv run python -m src.monitoring.simulate --port "$(APP_PORT)"

## Start MLflow UI and tracking API (localhost:5000 by default)
.PHONY: mlflow
mlflow:
	mkdir -p artifacts/mlflow
	uv run mlflow server --host "$(MLFLOW_HOST)" --port "$(MLFLOW_PORT)" \
		--backend-store-uri "$(MLFLOW_BACKEND_STORE_URI)" \
		--artifacts-destination "$(MLFLOW_ARTIFACTS_DESTINATION)"


#################################################################################
# Self Documenting Commands                                                     #
#################################################################################

.DEFAULT_GOAL := help

define PRINT_HELP_PYSCRIPT
import re, sys; \
lines = '\n'.join([line for line in sys.stdin]); \
matches = re.findall(r'\n## (.*)\n[\s\S]+?\n([a-zA-Z_-]+):', lines); \
print('Available rules:\n'); \
print('\n'.join(['{:25}{}'.format(*reversed(match)) for match in matches]))
endef
export PRINT_HELP_PYSCRIPT

help:
	@$(PYTHON_INTERPRETER) -c "${PRINT_HELP_PYSCRIPT}" < $(MAKEFILE_LIST)
