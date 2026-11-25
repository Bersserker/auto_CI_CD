# Этап 5: observability

API экспортирует `/metrics`: HTTP rate/status/latency, Python process metrics,
`scoring_predictions_total`, `scoring_inference_errors_total` и histogram
`scoring_inference_duration_seconds`. Метка `model_version` берётся из
`MODEL_VERSION` (по умолчанию `unknown`); задавайте идентификатор DVC/model release.
Один процесс uvicorn на контейнер: для нескольких workers нужен отдельный
multiprocess режим prometheus-client. Histogram измеряет весь prediction pipeline,
включая cold load и preprocessing. Валидация 422 учитывается только в HTTP метриках.
Данные клиентов не записываются в события `prediction_success`/`inference_error`.

## Локальное развёртывание (Linux Docker)

```bash
export GRAFANA_ADMIN_PASSWORD='<задайте пароль>'
make monitoring
```

Локальный Compose использует `MODEL_BACKEND=onnx`: нужны
`models/credit_default_model.onnx` и `models/best_scaler.joblib`.
Preprocessing повторяет обучение: 23 исходных и 7 вычисляемых признаков,
StandardScaler, float32; порог класса — 0.5.
Grafana: http://localhost:3000, пользователь admin; Prometheus:
http://localhost:9090; Alertmanager: http://localhost:9093; Loki:
http://localhost:3100/ready. Дашборды и datasources provisioned автоматически.
Данные сохраняются в named volumes, retention метрик/логов — 7 дней.
Остановка с сохранением данных: `make monitoring-down`.

Alloy читает Docker logs и добавляет service/project. Он видит все Compose
проекты на хосте; для общего хоста ограничьте discovery label-фильтром своего
проекта. Docker socket и privileged cAdvisor требуют доверенного Linux-хоста;
read-only bind socket не ограничивает операции Docker API. Не публикуйте эти
сервисы наружу. Node exporter показывает ресурсы хоста, cAdvisor — контейнеров.
Это локальный стенд; single replica и filesystem Loki не обеспечивают HA.

Alertmanager принимает алерты Prometheus и Loki ruler. Receiver `local-ui`
показывает их в UI; внешняя отправка не настроена. Для уведомлений укажите
свой webhook receiver и секрет вне git. [Runbook](RUNBOOK.md).

## Проверки

```bash
.venv/bin/python -m pytest tests/test_observability.py tests/test_api_readiness.py -q
docker run --rm -v "$PWD/monitoring/prometheus:/etc/prometheus:ro" \
  --entrypoint promtool prom/prometheus:v3.2.1 check config /etc/prometheus/prometheus.yml
docker run --rm -w /etc/prometheus -v "$PWD/monitoring/prometheus:/etc/prometheus:ro" \
  --entrypoint promtool prom/prometheus:v3.2.1 test rules test-alerts.yml
```

Воспроизводимые инциденты на отдельном тестовом стенде:

1. Остановите app через compose `stop app`. Через 1–1.5 минуты ScoringDown
   должен стать firing в Prometheus, затем появиться в Alertmanager.
   `start app` восстанавливает target, алерт должен разрешиться.
2. Запустите тестовый app с пустой директорией models, DVC_PULL=0. Отправляйте
   валидный JSON из `src/api/test_api.py` на `/predict` не реже раза в секунду
   в течение 3 минут. Ожидаются 503, рост inference_errors, ScoringErrors и
   ScoringLogErrors. Последний появляется после >5 событий в окне 1m и 30s for.
   Верните модель и перезапустите app; повторите успешный запрос, дождитесь resolved.
3. На обычном трафике алерты ошибок и latency отсутствуют. Проверяйте свежие
   логи в Grafana и resource metrics; пустая панель означает отсутствие данных.

Не используйте настоящие клиентские данные для этих сценариев. Порог latency
0.5s и error rate 5% — начальные демонстрационные значения, требуют согласования
с production SLO. Проверки качества модели и drift относятся к этапу 6.

## Источники конфигурации

[Prometheus alerting rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/),
[Alloy Docker logs](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.docker/).

Автоматический сквозной тест (свободные порты 8000, 3000, 9090, 9093, 3100):

```bash
docker build -t credit-backend:observability .
.venv/bin/python -m scripts.test_observability_local
```

Тест создаёт отдельный Compose project, подключает настоящую ONNX-модель и скейлер; fixture
добавляет управляемые ошибки загрузки для проверки инцидентов, проверяет метрики хоста/контейнеров, Grafana, доставку
метрических и логовых алертов в Alertmanager, отказ API и resolved после
восстановления. Выполняется около 10 минут; в конце удаляет только собственные
контейнеры и volumes. Не запускайте одновременно с локальным основным стеком.

## Kubernetes

`monitoring/kustomization.yaml` разворачивает тот же single-replica стенд в
namespace `monitoring` с PVC, сбором pod-метрик backend из namespace `mlops`,
node-exporter на Linux-узлах, cAdvisor через kubelet API и логами через
`loki.source.kubernetes`. У Alloy нет Docker socket. NetworkPolicy приложения
из этапа 3 уже разрешает ingress из namespace monitoring.

```bash
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
# Задайте секрет через ваш secret manager или локальный файл без завершающего перевода строки:
kubectl -n monitoring create secret generic grafana-admin --from-file=password=/path/to/password
kubectl apply -k monitoring
kubectl -n monitoring rollout status deployment/prometheus
kubectl -n monitoring rollout status deployment/loki
kubectl -n monitoring rollout status deployment/grafana
kubectl -n monitoring port-forward svc/grafana 3000:3000
# В отдельных терминалах:
kubectl -n monitoring port-forward svc/prometheus 9090:9090
kubectl -n monitoring port-forward svc/alertmanager 9093:9093
```

Требуется default StorageClass (PVC суммарно 23 GiB), backend из текущего образа
с `/metrics` и доступ monitoring pods к Kubernetes API. Образы backend
не обновляются этой командой: используйте delivery pipeline этапа 4.
При strict Istio mTLS отдельно настройте разрешённый сбор метрик.
В Grafana контейнерные метрики Kubernetes доступны по меткам namespace/pod/container;
предустановленные панели Docker с name предназначены для Compose. Для Kubernetes добавлены отдельные панели CPU/RAM pods с запросами `sum by (namespace,pod,container)
(rate(container_cpu_usage_seconds_total{namespace="mlops",container!=""}[5m]))`
и `container_memory_working_set_bytes{namespace="mlops",container!=""}`.
Этот вариант не заменяет HA monitoring в production. Облачное развёртывание
и проверка Kubernetes RBAC/StorageClass выполняются отдельно в целевом кластере.

## Полный демонстрационный эксперимент

Используется существующая ONNX-модель `models/credit_default_model.onnx` и
скейлер `models/best_scaler.joblib`. Joblib-модель скоринга не нужна.
Успешные запросы проходят настоящий inference; входные запросы генерируются
для демонстрации, а 503 инъецируются отдельно для проверки алертов.

```bash
# Остановите обычный стенд, чтобы освободить порты (с тем же паролем в окружении):
make monitoring-down
make monitoring-experiment
```

Команда собирает backend и запускает отдельный проект с ONNX-моделью.
Модельные файлы не меняются. Эксперимент длится около 10–12 минут:
2 минуты успешных прогнозов, 3 минуты ошибок 503, проверка алертов по метрикам
и логам, остановка API, проверка ScoringDown и восстановление. При восстановлении
продолжаются успешные запросы до снятия алертов. Grafana доступна во время
эксперимента и остаётся после него: http://localhost:3000,
логин `admin`, пароль `isolated-smoke-test`.

Откройте «Scoring: система и запросы» и «Scoring: анализ логов», выберите
Last 30 minutes и обновление 15s. Видны rate/5xx/p95, ресурсы, error logs и ALERTS.
Панели Kubernetes на Compose-стенде остаются пустыми; latency/RAM/disk alerts
не вызываются этим сценарием (их условия проверяются через promtool).
После завершения трафик прекращается, а история сохраняется в volumes.
Остановка с сохранением истории: `make monitoring-experiment-down`.
Это демонстрация observability с настоящим ONNX-инференсом на генерируемых
запросах, а не оценка качества кредитной модели на размеченной выборке.

### ONNX API без Docker

`make api` также выбирает ONNX. Для прежней joblib-модели можно явно указать
`make api MODEL_BACKEND=joblib`. При прямом запуске uvicorn задайте
`MODEL_BACKEND=onnx`. Дефолт для существующих Kubernetes/DVC deployments сохранён
joblib; для ONNX там нужно явно задать MODEL_BACKEND и доставить оба артефакта.

Контракт реального serving проверяется командой:

```bash
.venv/bin/python -m pytest tests/test_onnx_serving.py -q
```

Тест использует локальные модель/скейлер и строки processed CSV, сверяет
порядок 30 признаков, вероятности API и прямого ONNX-вызова (допуск 1e-6).
