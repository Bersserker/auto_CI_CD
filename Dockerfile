# Общая версия Python для сборки и запуска
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    HOME=/home/app \
    DVC_NO_ANALYTICS=1 \
    AWS_EC2_METADATA_DISABLED=true

WORKDIR /app

# Этап 1: установка зависимостей
FROM base AS builder

RUN pip install --no-cache-dir uv==0.11.7

COPY pyproject.toml uv.lock ./

# Устанавливаем зависимости в /app/.venv.
# Сам проект будет запускаться из исходного кода.
RUN uv sync --frozen --only-group serving --only-group model-storage --no-install-project

# Этап 2: образ для запуска API
FROM base AS runtime


# Обновления безопасности базового образа и библиотека для ML-зависимостей.
# pip со встроенными библиотеками нужен только на этапе сборки.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/local/bin/python -m pip uninstall --yes pip \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app \
    && mkdir -p /app/models /app/.dvc \
    && chown app:app /app/models /app/.dvc

# Переносим готовое виртуальное окружение из builder
COPY --from=builder /app/.venv /app/.venv

# Переносим только исходный код приложения
COPY --chown=app:app src ./src
COPY --chown=app:app .dvc/config ./.dvc/config
COPY --chown=app:app .dvcignore dvc.yaml dvc.lock ./
COPY --chown=app:app scripts/configure_dvc_remote.py ./scripts/configure_dvc_remote.py
COPY --chown=app:app docker/backend-entrypoint.py ./docker/backend-entrypoint.py
# Образу не нужна история Git. Только runtime-конфигурация DVC использует no_scm.
RUN printf '[core]\n    no_scm = true\n' > /app/.dvc/config.local \
    && chown app:app /app/.dvc/config.local

ENV DVC_SITE_CACHE_DIR=/tmp/dvc-site-cache

USER app

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health', timeout=3)"

ENTRYPOINT ["python", "docker/backend-entrypoint.py"]

CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
