# Этап 7: переобучение, кеширование и масштабирование

## Локальный запуск

```bash
make stage7-experiment
make stage7-credentials
# При уже работающем Prometheus/Grafana этапа 5:
make stage7-monitoring
```

Команда поднимает зависимости Seldon этапа 6, Airflow 3.1.0, training worker,
Redis для каждой локальной площадки, две основные ONNX-реплики, резервную
реплику и HAProxy. Затем выполняет настоящий Airflow DAG, проверяет new-data
trigger, собирает gated candidate image и проверяет нагрузку/отказы.
Существующий Grafana/observability стенд не останавливается.

- Airflow UI: http://localhost:28080, логин admin, сгенерированный пароль
  показывает `make stage7-credentials`.
- Балансируемый ONNX API: http://localhost:28000/docs.
- Training worker: http://localhost:28081/docs.
- Grafana: http://localhost:3000 → «Scoring: переобучение, Redis и реплики (локально)».
- Результаты: `artifacts/stage7/` (исключены из git).

Отдельные команды: `stage7-up`, `stage7-retrain`, `stage7-events`,
`stage7-candidate`, `stage7-load`, `stage7-test`, `stage7-down`.
`stage7-down` сохраняет Airflow database и артефакты; Seldon этапа 6
останавливается отдельно через `make stage6-down`.
Требуются Linux Docker, существующие ONNX/INT8/scaler и processed CSV.
Первый запуск скачивает Airflow/Redis/HAProxy images.

## DAG и триггеры

`airflow/dags/credit_retraining.py` содержит:

1. `credit_retraining`: каждый понедельник в 02:00 UTC (05:00 Москвы),
   `catchup=False`, один активный run, retry=1. Последовательность
   prepare → train → validate → publish. Worker выполняет этапы отдельно,
   статусы сохраняются в run directory, успешные этапы повторно не обучаются.
2. `credit_retraining_events`: каждые 5 минут проверяет new-data inbox и
   drift summary, через TriggerDagRunOperator запускает тот же training DAG.
   Event ID — hash типа события и содержимого, lease 30 минут предотвращает
   повторный запуск при следующем опросе. Завершённые события не переобучаются.

New-data: положите CSV в `artifacts/stage7/inbox/`. Колонки: id, 23 исходных
признака, target, predicted_at, matured_at, observed_at. Production batches
проверяются на дубликаты, Pydantic-схему, конечность вычисленных признаков,
наблюдение не ранее maturity, горизонт >=30 дней и observed_at <= as_of.
`demo.csv` используется только явно смоделированным экспериментом.

Drift: summary этапа 6 в `artifacts/stage6/drift/production/summary.json`.
Триггеры — data_drift/performance_decay/concept_drift_suspected. Симуляционный
summary не запускает production retraining. В локальном стенде разрешена
отдельная демонстрация через concept-drift summary этапа 6; ALLOW_SIMULATION=1.
Локальный worker маркирует результаты STAGE7_LOCAL_ONLY=1; их внешняя
публикация и production CI dispatch отключены.

`make stage7-events` создаёт помеченный demo batch из исторических строк,
используя новые демонстрационные ID и временные метки, и запускает events DAG.
Это проверка автоматизации, не поток банковских наблюдений. Реальные исходы
должны поступать из источника, определённого на этапе 6.

## Обучение и quality gate

Champion-файлы `models/` не перезаписываются. История делится теми же
параметрами train_test_split (20%, seed=42, stratify), что в исходном NN training.
Фиксированные 6000 held-out строк никогда не участвуют в обучении кандидата;
new-data с их ID отклоняются. Повторные train-ID обновляются последним outcome.
В snapshot фиксируются SHA256 dataset/train/holdout/champion/scaler/code.

Кандидат — обучаемая MLP 30→128→64→1 (sklearn MLPClassifier, ReLU/Sigmoid),
отдельный StandardScaler fit только на train, early stopping внутри train,
seed=42. Это новый вариант NN без BatchNorm/dropout исходного PyTorch champion,
а не скрытая подмена его весов. ONNX граф строится из обученных весов; scaler
и порядок признаков сохраняются совместно. Зависимости/архитектура закреплены.

Gate на неизменном holdout:

- минимум 1000 объектов, оба класса;
- ROC-AUC >=0.65 и падение относительно champion <=0.01;
- рост log loss <=0.03;
- max разница sklearn/ONNX <=1e-5, вероятности конечны и в [0,1];
- p95 тёплого batch=1 CPU inference <=100ms (20 замеров).

Провал gate делает DAG failed и блокирует publish. Порог не ослабляется для
демонстрации: недостаточно обученный кандидат действительно был отклонён.
Gate не доказывает отсутствие деградации по каждому сегменту; production
требует согласования статистического объёма, fairness/segment quality и
post-deploy SLO/rollback этапа 4. Для накопления новых unbiased итоговых оценок
нужен отдельный свежий holdout; повторный выбор по одному holdout ограничивает
обобщаемость сравнения.

## Seldon и CI/CD handoff

Только прошедший gate кандидат получает новое имя Seldon `credit-<run_id>`.
Worker регистрирует модель и ждёт ModelAvailable. В bundle входят ONNX,
scaler, prepared/trained/gate/registration metadata; delivery.json содержит
SHA256 bundle/model, logical version и признак candidate. Подготовленный
candidate ожидает **свой** scaler: gateway этапа 6 с champion scaler не должен
маршрутизировать в него до выпуска согласованной пары.

`make stage7-candidate` безопасно распаковывает bundle, повторяет gate и
проверку hashes, выполняет ONNX smoke и собирает отдельный image с обоими
артефактами. Работающие версии не заменяются.

Production worker можно настроить через отдельный защищённый deployment/env:
MODEL_CANDIDATE_BUCKET, MODEL_S3_ENDPOINT=https://storage.yandexcloud.net,
AWS credentials минимальных прав; MODEL_CI_REPOSITORY=owner/repo,
MODEL_CI_REF=<защищённая ветка>, MODEL_CI_TOKEN (Actions dispatch).
Нельзя включать STAGE7_LOCAL_ONLY/ALLOW_SIMULATION для production runs.
При наличии настроек несмоделированный кандидат загружается в
`candidates/<run_id>/` Object Storage, затем запускается model-candidate.yml.
В локальном Compose эти секреты не заданы, внешние действия не выполняются.

CI workflow включается MODEL_CANDIDATES_ENABLED=true, берёт bucket из
repository variables, получает bundle по ID, проверяет SHA256 и gate,
отклоняет simulated candidate, собирает image и запускает Trivy HIGH/CRITICAL.
Verified bundle сохраняется как handoff artifact. Production image publication,
immutable digest и продвижение должны пройти существующие защищённые
staging/production/security/rollback процедуры этапа 4. Автоматическая замена
production из локального Airflow не включена. GitHub/cloud выполнение не проверено
без credentials; локально handoff/image/ONNX проверены реально.

## Redis

Кеш включается REDIS_URL; без него API работает как раньше. Ключ — HMAC-SHA256
канонического запроса и SHA256 модели/скейлера; он не содержит исходных значений.
CACHE_KEY_SECRET (или SECRET_FILE) должен быть >=32 bytes. Новая пара артефактов
автоматически даёт другой namespace ключей. Модель/identity кешируются в процессе:
при смене файлов нужен новый контейнер, а не подмена весов на месте.

TTL=60s (допускается 1..3600), maxmemory=64MiB в локальном стенде, eviction LRU.
JSON response проверяется Pydantic при чтении. Redis timeout=100ms;
недоступный/повреждённый кеш переводит запрос на настоящий inference.
Записываются метрики scoring_cache_events_total{result=hit|miss|error}.
Кеш не является источником истины и не требует географической репликации:
каждая площадка пересчитывает ответ из той же immutable модели.
Для production используйте Redis TLS/auth, NetworkPolicy и секреты;
локальные Redis ports наружу не опубликованы.

## Масштабирование и географическая репликация

Локальный CPU autoscaler читает реальные docker stats: два высоких замера
для scale-out, четыре низких для scale-in, cooldown=8s, min=2/max=4.
Scale-out использует --no-recreate, сохраняет работающие реплики.
`stage7-load` использует демонстрационный порог 5% CPU на реплику и реальную
нагрузку с различными запросами, чтобы cache hits не скрывали вычисления.
После теста возвращает 2 реплики. Обычный scaler по умолчанию использует
high=50%, low=10%; это не production cluster autoscaler.

Kubernetes: `infrastructure/stage7/autoscaling.yaml` — HPA autoscaling/v2,
CPU 60%, min=2/max=6, scale-down stabilization=300s. Нужны metrics-server и
resource requests. YC node autoscaling уже задан в
`infrastructure/modules/kubernetes/node_group.tf`: min/initial/max для каждой
node group. HPA создаёт pod demand, Managed Kubernetes расширяет node group.
При Blue-Green/Canary направляйте HPA на активное deployment, а не неактивную
версию. Имеются Redis Deployment/Service/NetworkPolicy и отдельный cache patch;
в каждом кластере заранее создайте Secret scoring-cache с password/url/hmac.
Сначала примените существующий immutable ONNX release, затем HPA/Redis и patch.

Локальные site-a/site-b находятся на **одном хосте**: это симуляция топологии,
не географическая проверка. HAProxy проверяет /ready, балансирует site-a,
при полном отказе использует site-b; заголовки X-Serving-Region/Replica помогают
проверить маршрутизацию. Тесты подтверждают отказ реплики, Redis, всей site-a.

Для настоящей георепликации подготовлен `scripts/stage7_geo.py`: два независимых
kubectl contexts, непересекающиеся topology.kubernetes.io/region, два различных
региональных HTTPS endpoints и одинаковый rendered release directory.
Две availability zones одного ru-central1 не принимаются за разные регионы.
Сначала server dry-run в обоих кластерах, только --apply выполняет deploy и
проверяет rollout. Скрипт создаёт HAProxy edge config с TLS verification/SNI и
корректным региональным Host header. Edge и клиентский TLS/LB должны работать
в независимом третьем failure domain, с резервированием и DNS health checks,
которые настраиваются владельцем внешнего endpoint.

```bash
uv run python -m scripts.stage7_geo \
  --context-a region-a --context-b region-b \
  --manifest-dir /secure/rendered-release \
  --host-a scoring-a.example.org --host-b scoring-b.example.org
# Добавьте --apply только для подготовленных целевых кластеров.
```

Второй географический регион, global edge/DNS и credentials сейчас отсутствуют:
реальное multi-region failover и node autoscaling в облаке не подтверждены.
Развёртывание этих компонентов нельзя считать завершённым по локальной симуляции.

## Диагностика

Airflow: состояние prepare/train/validate/publish и файл gate.json. Ошибка
качества — ожидаемая блокировка, а не повод менять пороги.
Seldon: ModelAvailable и согласованная пара model/scaler.
Cache: hit/miss/error, Redis memory/eviction, readiness backend при отключённом Redis.
Load: artifacts/stage7/resilience.json и autoscaling.json; ненулевые HTTP errors
при scale-out требуют проверки readiness/DNS и недопустимого recreate.
Не удаляйте holdout/provenance для повторного gate; создавайте новый run ID.

Источники: [Airflow Docker](https://airflow.apache.org/docs/apache-airflow/3.1.8/howto/docker-compose/),
[YC autoscaling](https://yandex.cloud/en/docs/managed-kubernetes/operations/autoscale).
