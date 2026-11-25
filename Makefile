#################################################################################
# GLOBALS                                                                       #
#################################################################################

PROJECT_NAME = teplayakov_an_mlops_project
PYTHON_VERSION = 3.13
PYTHON_INTERPRETER = python
APP_HOST ?= 127.0.0.1
APP_PORT ?= 8000
MODEL_BACKEND ?= onnx
MLFLOW_HOST ?= 127.0.0.1
MLFLOW_PORT ?= 5000
MLFLOW_BACKEND_STORE_URI ?= sqlite:///artifacts/mlflow/mlflow.db
MLFLOW_ARTIFACTS_DESTINATION ?= $(CURDIR)/artifacts/mlflow/mlartifacts
MLFLOW_TRACKING_URI ?= http://127.0.0.1:$(MLFLOW_PORT)
BENCHMARK_ARGS ?=
BENCHMARK_GPU_DIR ?= /tmp/mlops-ort-gpu
MONITORING_COMPOSE = docker compose -f docker-compose.yml -f docker-compose.monitoring.yml

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
	MODEL_BACKEND="$(MODEL_BACKEND)" uv run uvicorn src.api.app:app --host "$(APP_HOST)" --port "$(APP_PORT)"

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

## Start local dashboards and API (set GRAFANA_ADMIN_PASSWORD first)
.PHONY: monitoring
monitoring:
	$(MONITORING_COMPOSE) up -d --build
	@echo "Grafana: http://localhost:3000 (login: admin)"

## Stop local monitoring and API, preserving stored data
.PHONY: monitoring-down
monitoring-down:
	$(MONITORING_COMPOSE) down

## Run a full ONNX monitoring experiment and keep dashboards (~10 minutes)
.PHONY: monitoring-experiment
monitoring-experiment:
	docker build -t credit-backend:observability .
	uv run python -m scripts.test_observability_local --keep

## Stop the experiment, preserving its dashboards and history
.PHONY: monitoring-experiment-down
monitoring-experiment-down:
	GRAFANA_ADMIN_PASSWORD=isolated-smoke-test docker compose -p scoring-observability-test \
		-f docker-compose.monitoring.yml -f docker-compose.monitoring-demo.yml down


#################################################################################
# Self Documenting Commands                                                     #
#################################################################################

.DEFAULT_GOAL := help

define PRINT_HELP_PYSCRIPT
import re, sys; \
lines = '\n'.join([line for line in sys.stdin]); \
matches = re.findall(r'\n## (.*)\n[\s\S]+?\n([a-zA-Z0-9_-]+):', lines); \
print('Available rules:\n'); \
print('\n'.join(['{:25}{}'.format(*reversed(match)) for match in matches]))
endef
export PRINT_HELP_PYSCRIPT

help:
	@$(PYTHON_INTERPRETER) -c "${PRINT_HELP_PYSCRIPT}" < $(MAKEFILE_LIST)

## Generate Evidently baseline, data drift and concept drift reports using real ONNX
.PHONY: stage6-drift
stage6-drift:
	uv run --group monitoring python -m src.monitoring.model_drift

## Start local Seldon Core 2 and register FP32/INT8 ONNX versions
.PHONY: stage6-up
stage6-up:
	bash scripts/stage6_setup.sh

## Connect this project's running Prometheus/Grafana to stage 6 metrics
.PHONY: stage6-monitoring
stage6-monitoring:
	uv run --group monitoring python -m scripts.stage6_monitoring

## Run the full local stage 6 experiment (Evidently + Seldon A/B + shadow)
.PHONY: stage6-experiment
stage6-experiment: stage6-up
	$(MAKE) stage6-drift
	uv run --group monitoring python -m scripts.stage6_experiment
	@echo "Reports: http://localhost:18081/; results: artifacts/stage6/comparison.json"

## Stop Seldon stage 6 while keeping model state and experiment evidence
.PHONY: stage6-down
stage6-down:
	docker compose -p scoring-stage6 -f docker-compose.stage6.yml down

## Test drift evaluation, outcome joins, A/B routing and shadow isolation
.PHONY: stage6-test
stage6-test:
	uv run --group monitoring python -m pytest tests/test_stage6.py -q

## Start Airflow, Redis, ONNX replicas and candidate lifecycle services
.PHONY: stage7-up
stage7-up: stage6-up
	bash scripts/stage7_setup.sh

## Run real Airflow NN retraining, ONNX validation and gated Seldon publication
.PHONY: stage7-retrain
stage7-retrain:
	uv run python -m scripts.stage7_retrain

## Exercise new-data-triggered retraining through the Airflow event DAG
.PHONY: stage7-events
stage7-events:
	uv run python -m scripts.stage7_events

## Test Redis failure, replica/site failover and real CPU scale-out under load
.PHONY: stage7-load
stage7-load:
	uv run python -m scripts.stage7_resilience

## Verify and build the most recent candidate image without replacing production
.PHONY: stage7-candidate
stage7-candidate:
	uv run python -m scripts.stage7_candidate --run-id "$$(cat artifacts/stage7/last-run.txt)" --allow-simulated --build

## Run the full local stage 7 experiment, preserving Airflow history and evidence
.PHONY: stage7-experiment
stage7-experiment: stage7-up
	$(MAKE) stage7-retrain
	$(MAKE) stage7-events
	$(MAKE) stage7-candidate
	$(MAKE) stage7-load

## Show the generated local Airflow admin login/password
.PHONY: stage7-credentials
stage7-credentials:
	docker compose -p scoring-stage7 -f docker-compose.stage7.yml exec -T airflow cat /opt/airflow/simple_auth_manager_passwords.json.generated

## Test stage 7 release gates, holdout protection, caching and autoscaler rules
.PHONY: stage7-test
stage7-test:
	uv run python -m pytest tests/test_stage7.py -q

## Stop only stage 7, preserving Airflow metadata, candidates and experiment evidence
.PHONY: stage7-down
stage7-down:
	docker compose -p scoring-stage7 -f docker-compose.stage7.yml down

## Connect current Prometheus/Grafana to retraining, Redis and replica metrics
.PHONY: stage7-monitoring
stage7-monitoring:
	uv run python -m scripts.stage7_monitoring
