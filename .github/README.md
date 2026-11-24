# GitHub Actions

`workflows/ci-cd.yml` запускается на push, pull request и вручную:
quality → tests → images/security + локальная Istio integration → release artifact
→ staging → production. Production использует те же image digest, что и staging.

- `code-quality.yml`: Black, Flake8, actionlint, Trivy secret scanning.
- `build-and-test.yml`: pytest, два Docker image, Trivy HIGH/CRITICAL с исправлениями;
  публикация в GHCR только для default branch. Job `istio-strategies` проверяет
  Blue-Green, Canary и откат в реальном kind/Istio; release artifact ожидает его.
- `deploy-staging.yml`, `deploy-production.yml`: доставка Blue-Green на Istio.
- `blue-green-release.yml`, `canary-release.yml`: ручной выбор стратегии и окружения.
- `rollback.yml`: переключение на предыдущий слот или восстановление active после
  прерванной доставки.
- `_deploy.yml`: общая environment/concurrency/provenance/credentials/monitor логика.

Images: `ghcr.io/<owner>/<repo>/credit-backend` и `credit-frontend`. Артефакт
`release.json` содержит commit SHA, CI run ID и два digest. Произвольные tags и
артефакты PR к доставке не допускаются. K8s manifests берутся из того же коммита.
PR не получает credentials кластера и не публикует images. `GITHUB_TOKEN` с
`packages:write` позволяет публиковать GHCR без аккаунта Yandex Cloud.

## Пока аккаунта Yandex нет

Оставьте repository variable `CLOUD_DEPLOY_ENABLED` пустой или `false`.
Проверки, сборка, сканирование и kind-тест работают без облачных credentials.
После push default branch также публикуются packages GHCR. Сам GitHub run и
облачное развёртывание запускаются только в вашем GitHub/подготовленном кластере.

| Repository variable | Значение |
| --- | --- |
| `CLOUD_DEPLOY_ENABLED` | `true` после подготовки обоих окружений |
| `RUN_ISTIO_INTEGRATION` | Включена по умолчанию; `false` явно отключает тяжёлую локальную проверку и её gate |
| `RUN_K8S_INTEGRATION` | `true` дополнительно запускает прежний тест этапа 3 на Traefik |
| `VALIDATE_KAGGLE_DATA` | `true` включает Kaggle/Great Expectations; schema unit tests работают всегда |
| `DEPLOY_RUNNER_LABELS` | JSON-массив, по умолчанию `["ubuntu-latest"]`; для private API, например `["self-hosted","linux","mlops-deploy"]` |

CI требуется интернет для зависимостей, security databases и system images.
Runner доставки должен иметь доступ к Kubernetes API, HTTPS APP_DOMAIN и GitHub;
нужны Bash, curl, tar, sha256sum, Git, Python и `gh`. K8s integration использует
Docker с `docker image save --platform`. Не запускайте недоверенные PR на runner
доставки. Kubectl 1.35 рассчитан на API 1.34–1.36; синхронизируйте версии при смене
версии облачного кластера.

## GitHub Environments

Создайте `staging` и `production` с отдельными credentials. Для production
в Settings → Environments настройте required reviewers, запрет обхода approval
и разрешите deploy только с default branch. Одного имени environment в YAML
недостаточно для approval. Branch protection должна требовать CI перед merge.

| Тип | Имя | Содержание |
| --- | --- | --- |
| Variable | `APP_DOMAIN` | DNS hostname без схемы/пути, направленный на Istio LoadBalancer |
| Variable | `ARTIFACTS_BUCKET` | Бакет моделей/данных, отдельно от Terraform state |
| Variable | `REGISTRY_PULL_SECRET` | Имя image pull Secret в mlops; пусто для публичного GHCR |
| Variable | `ALLOW_INITIAL_DEPLOY` | `true` для первой установки без baseline, затем удалить/выключить |
| Secret | `KUBECONFIG_B64` | `base64 -w0 ci-kubeconfig.yaml`, отдельный CI context с token/client certificate |
| Secret | `AWS_ACCESS_KEY_ID` | S3 access key с чтением артефактов обоих сохраняемых релизов |
| Secret | `AWS_SECRET_ACCESS_KEY` | Соответствующий S3 secret key |

Kubeconfig — самодостаточный HTTPS с CA/certificate/key в `*-data`, без локальных
путей, `exec`/`auth-provider` и отключения TLS validation. Обычный `yc` exec
kubeconfig не подходит. Обновляйте CI credentials до истечения срока. Не храните
их в Git. Private GHCR требует заранее созданный registry Secret с отдельным
`read:packages` credential; краткоживущий `GITHUB_TOKEN` не годится для постоянного
pull узлами.

До первого deploy: CPU nodes `workload=cpu`, Istiod и Istio ingressgateway,
DNS, TLS Secret **istio-system/credit-tls**, registry Secret при необходимости,
реальная `models/best_model.joblib` в DVC remote и соответствующая DVC metadata
в проверенном коммите. CI не публикует синтетическую модель в облако.
Подробные команды установки, права CI account, ёмкость двух слотов, критерии и
восстановление: [Istio README](../infrastructure/kubernetes/istio/README.txt).
Схема gateway traffic management не требует sidecar в приложениях и не включает
внутренний mTLS. Traefik из этапа 3 сохраняется для отдельного локального теста.

## Запуск и результат

Actions → Deploy staging / Deploy production / Blue-Green release / Canary release:
передайте `run_id` успешного CI на default branch. Production требует receipt
успешного staging для того же release.json. Если staging запускали отдельно,
передайте ID его workflow run в `staging_run_id`; в автоматической цепочке пусто.
Артефакты хранятся 30 дней. Ручной deploy требует завершённый CI; внутри текущего
CI его deploy jobs могут использовать уже готовый release artifact.

Canary направляет 10 → 25 → 50 → 100% новых запросов API и frontend на candidate.
Blue-Green проверяет неактивный слот и переключает 100%. На каждой ступени:
минимум 60 выборок health/predict/UI (до 180 при редком subset), ноль ошибок,
p95 ≤1с, каждый запрос ≤5с, валидный
API-контракт и фактические ответы всех ненулевых subset. Политика хранится в
`infrastructure/kubernetes/istio/release-policy.yaml`. Это проверки доставки;
бизнес-метрики и качество модели по размеченным данным требуют отдельных gates.

При нарушении критериев контроллер возвращает трафик на baseline, проверяет его
и оставляет job красным. Предыдущий слот сохраняется без пересборки. Rollback
workflow переключает на previous, либо возвращает active при pending-транзакции.
Secrets/DVC remote не откатываются; обе модели и доступы должны сохраняться.
При первом deploy предыдущей версии нет. При SIGKILL/аварии runner/потере API
нужен ручной Rollback после восстановления связи; новый deploy с pending запрещён.
Состояние: `mlops/mlops-release-state`. Отчёты: artifacts `release-evidence-*` и
`istio-delivery-evidence`. Jobs одного environment сериализованы.

Локальная демонстрация:

```bash
bash scripts/test_istio_local.sh
```

Она создаёт и удаляет собственный kind-кластер, использует синтетические модели
с probability 0.5/0.75 и проверяет реальные переключения и ошибки через TLS Istio.
Обычные модели и пользовательский kubeconfig не изменяются.

Основание для environment/reusable workflows:
[GitHub Docs](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows).
Маршрутизация:
[Istio Traffic Shifting](https://istio.io/latest/docs/tasks/traffic-management/traffic-shifting/).
