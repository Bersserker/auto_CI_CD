FROM python:3.11-slim
RUN pip install --no-cache-dir mlserver==1.7.1 onnxruntime==1.20.1
ENV MLSERVER_LOAD_MODELS_AT_STARTUP=false MLSERVER_HTTP_PORT=8080 MLSERVER_GRPC_PORT=8081 MLSERVER_METRICS_PORT=8084
CMD ["mlserver", "start", "/mnt/agent/models"]
