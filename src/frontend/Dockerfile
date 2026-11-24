# Сборка из корня репозитория: docker build -f src/frontend/Dockerfile .
FROM python:3.13-slim AS builder
WORKDIR /build
COPY src/frontend/requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.13-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/home/app \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
WORKDIR /app
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app
COPY --from=builder /opt/venv /opt/venv
COPY src/__init__.py ./src/__init__.py
COPY src/api/__init__.py src/api/models.py ./src/api/
COPY src/frontend ./src/frontend
USER app
EXPOSE 8501
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3)"
CMD ["streamlit", "run", "src/frontend/app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
