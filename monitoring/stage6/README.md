# Этап 6: drift и управление моделями

Локальное выполнение использует реальные ONNX FP32 и INT8, существующий
StandardScaler и Seldon Core 2.10.0 (scheduler, agent, Envoy, rclone и MLServer
1.7.1 с собственным ONNX runtime). Kafka/dataflow и Kubernetes не нужны для
этих сценариев. A/B маршрутизацию и shadow выполняет gateway перед Seldon;
жизненным циклом обеих моделей управляет scheduler. Облачная HA-установка
этим локальным экспериментом не подтверждается.

## Запуск

```bash
make stage6-experiment
# Если Grafana/Prometheus уже запущены через monitoring или monitoring-experiment:
make stage6-monitoring
```

Первый запуск скачивает Core 2 images и CLI и собирает небольшой MLServer образ.
Порты не пересекаются с этапом 5:

- http://localhost:18080/docs — запросы к Seldon gateway;
- http://localhost:18081/ — три интерактивных HTML-отчёта Evidently;
- http://localhost:19000 — Envoy inference, http://localhost:19004 — scheduler gRPC;
- http://localhost:3000 — существующая Grafana, dashboard
  «Scoring: drift и модели (локальная симуляция)», период Last 30 minutes.

`stage6-monitoring` подключает только Prometheus текущего проекта или
scoring-observability-test к Seldon network и перечитывает его конфигурацию.
Grafana подхватывает JSON dashboard автоматически, пароль остаётся прежним.
Отдельно доступны `make stage6-drift`, `make stage6-up`, `make stage6-test`.
`make stage6-down` останавливает только этап 6, сохраняя volumes и результаты.
Повторить трафик без пересоздания runtime:
`uv run --group monitoring python -m scripts.stage6_experiment`.

## Эталон, окна и пороги

`artifacts/stage6/drift/manifest.json` фиксирует SHA256 исходного processed CSV,
ONNX и скейлера, seed=42, список ID эталона/текущей выборки и пороги.
Эталон — 2000 записей из held-out части (те же train_test_split параметры, что
в обучении NN), current — другие 1000 записей. Обучающие записи исключены.
Эталон повторного запуска не перезаписывается при изменении provenance:
нужно выбрать новую директорию через `--output`.

Evidently 0.7.17 считает PSI для 23 исходных числовых признаков и вероятности.
Порог PSI=0.2; data drift — не менее 20% исходных признаков выше порога.
Вероятность не входит в знаменатель доли признаков. Minimum window — 200 строк.
Качество считается при наличии обоих классов и >=90% зрелых меток:
ROC-AUC и log loss; decay — падение AUC >0.03 или рост loss >0.05.
Residual `target-probability` сравнивается в группах `pay_0 > 0` / остальные
при >=50 размеченных строках на группу. Concept drift **подозревается**, если
при отсутствии feature drift ухудшилось качество и residual изменился >0.1.
Это диагностический сигнал, не статистическое доказательство изменения P(y|X).
При data drift ухудшение качества трактуется как смешанный/неопределённый случай.

Три воспроизводимые симуляции:

1. baseline — обычная held-out выборка;
2. data-drift — увеличение денежных сумм и сдвиг истории просрочек,
   реальные ONNX-вероятности пересчитываются;
3. concept-drift — те же признаки/вероятности, искусственно изменённые targets.

Смоделированные исходы не выдаются за банковские наблюдения. Все dashboard
и информационные алерты по этим результатам обозначены как simulation.
Без зрелых меток performance/concept state = unknown, метрики AUC и decay
не экспортируются; отсутствие метрик не означает отсутствие проблемы.

## Фактические исходы и задержка меток

Production-источник — согласованная выгрузка банковской системы обслуживания
кредитов: outcome для конкретной заявки по истечении горизонта 30 дней,
критерий дефолта должен соответствовать определению target при обучении.
Доступа к такой системе в репозитории нет. Требуется согласовать фактическую
семантику и обеспечить выгрузку владельцем данных, а не выводить labels из
вероятностей модели или выдачи/отказа в кредите.

Таблица predictions: уникальный непрямой `request_id`, `predicted_at` (UTC),
23 исходных признака, `probability`; эталон дополнительно содержит `target`.
Предоставляйте отдельные cohort CSV для каждой неизменной версии модели.
Таблица outcomes: уникальный `request_id`, бинарный `target`, `matured_at`
(окончание горизонта), `observed_at` (когда исход стал доступен системе).
Join строго one-to-one; дубликаты, невалидные метки/времена, горизонт менее
30 дней и наблюдение до созревания отклоняются. Исходы после as-of исключаются,
неизвестный target остаётся NaN и не заменяется на 0.

Production CLI применяет фиксированное окно 7 дней с `--window-end`,
по умолчанию заканчивающееся в `--as-of`. Data drift можно считать для
последних 7 дней без outcomes. Для качества нужен отдельный зрелый cohort,
например окно, завершившееся не позднее чем 30 дней до as-of:

```bash
uv run --group monitoring python -m src.monitoring.model_drift \
  --reference /secure/reference.csv --current /secure/predictions.csv \
  --outcomes /secure/outcomes.csv --as-of 2026-11-25T12:00:00Z \
  --window-end 2026-10-25T12:00:00Z --output artifacts/stage6/production-cohort
```

Чувствительные признаки не пишутся gateway в stdout/журнал. Сохранять их для
мониторинга должен согласованный защищённый ingestion с политикой доступа и
удаления. CLI не получает данные из банковской системы автоматически.
HTML-отчёты могут содержать агрегаты признаков: production-отчёты храните с
соответствующим контролем доступа. Локальный reports server отдаёт только три
демонстрационных HTML и агрегированные метрики, не CSV.

## A/B, shadow и критерии продвижения

A/B: SHA256 непрямого X-Request-ID, 10000 buckets, 50% FP32 / 50% INT8.
При повторе того же ID назначение стабильно. Для банковского A/B используйте
стабильный псевдоним клиента, чтобы повторы/заявки не переходили между группами.
Gateway выбирает model name, масштабирует признаки и вызывает Seldon V2 inference.
Случайное конечное число запросов не обязано дать ровно 50/50.

Shadow: клиент получает только результат FP32. Отдельный pool из двух workers
с максимум 32 задачами отправляет те же признаки INT8; ошибки/переполнение
shadow учитываются отдельно и не меняют primary ответ. При переполнении копия
пропускается, а не блокирует клиента. Нет автоматического fallback или promotion.

Журнал `artifacts/stage6/records/predictions.jsonl`: request_id, время, режим,
модель, вероятность/ошибка, paired delta. Источник модели/скейлера и SHA256 —
`artifacts/stage6/repository/manifest.json`. Исходные признаки и банковские
идентификаторы в журнал не пишутся. На production нужен устойчивый storage
и ограничение retention; локальный append-only журнал предназначен для демо.

Эксперимент выполняет 200 A/B и 200 shadow запросов на held-out данных,
сверяет ответы Seldon с прямым ONNX (1e-5), ждёт 200 shadow результатов,
считает AUC/loss на **одних и тех же** 200 размеченных объектах и max delta.
`comparison.json` содержит preliminary gate: падение AUC <=0.01,
max probability delta <=0.01. Это проверка совместимости, не вывод о
причинном эффекте реального онлайн A/B. Для promotion нужны согласованный
объём и длительность A/B, зрелые исходы, non-inferiority по AUC/loss,
latency p95, ошибки, coverage shadow и проверка значимых сегментов.
Автоматическое продвижение здесь отключено, переобучение относится к этапу 7.

## Диагностика и восстановление

- Data drift: проверьте provenance, схему и окно, затем входные распределения.
  Не меняйте эталон для снятия тревоги.
- Decay/concept: проверьте зрелость, coverage, правильность join, cohort и версию
  модели; сравните сегменты. Не запускайте обучение на незрелых исходах.
- Seldon 503: `docker compose -p scoring-stage6 -f docker-compose.stage6.yml logs
  scheduler agent mlserver`, `artifacts/stage6/tools/seldon model status
  --scheduler-host localhost:19004 credit-fp32` (аналогично INT8).
- Shadow errors: проверьте доступность INT8, лимиты очереди, pair coverage.
  Primary при shadow должен продолжать возвращать FP32.
- Восстановление: верните исправные артефакты, `make stage6-up`, повторите
  эксперимент; сравните gate/дельты и состояние `ModelAvailable` обеих моделей.
  Не меняйте файлы активной модели на месте: создавайте новую revision/model name.

Источники: [Evidently Report](https://docs.evidentlyai.com/docs/library/report),
[Seldon Core 2 installation](https://docs.seldon.ai/seldon-core-2/installation/installation).
