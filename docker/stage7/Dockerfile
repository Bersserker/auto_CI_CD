FROM credit-backend:observability
USER root
ARG TRAINER_UID=1000
RUN getent passwd "$TRAINER_UID" || useradd --uid "$TRAINER_UID" --gid app --create-home trainer
RUN pip install --no-cache-dir --no-deps --target /app/vendor onnx==1.18.0 redis==5.2.1
COPY --chown=app:app src /app/src
ENV PYTHONPATH=/app:/app/vendor
USER app
