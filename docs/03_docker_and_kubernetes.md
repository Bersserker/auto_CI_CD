# Этап 3 Контейнеризация и оркестрация

## Цель этапа

Упаковать API и интерфейс, обеспечить получение определенной версии модели и проверить замену реплик без потери доступности. Docker отвечает за среду одного процесса, Kubernetes — за размещение, доступность и обновление нескольких экземпляров.

Изучите [Dockerfile backend](../Dockerfile), [Dockerfile frontend](../src/frontend/Dockerfile), [DVC-инструкцию](../docker/README.txt), [Kubernetes-инструкцию](../infrastructure/kubernetes/README.txt).

## Шаг 1 Разобрать многостадийную сборку

Backend использует общий Python base, builder с установкой зависимостей и runtime со средой запуска. В runtime переносятся выбранные зависимости и исходники; инструменты обучения и ноутбуки не нужны для обслуживания запросов. Процесс работает от пользователя UID/GID 10001.

Frontend имеет отдельный образ, не получает модель и S3-ключи. Он вызывает API. Так изменение формы не требует менять backend, а масштабирование API не требует размножать всю пользовательскую часть.

```bash
docker build -t credit-backend:study .
docker build -f src/frontend/Dockerfile -t credit-frontend:study .
```

Сначала проверьте завершение сборки, затем запуск: успешный `docker build` не означает успешную загрузку модели. Секреты и локальные артефакты исключаются из build context через `.dockerignore`. Правила `.gitignore` на состав Docker context не влияют.

## Шаг 2 Проверить доставку модели через DVC

При `DVC_PULL=1` entrypoint настраивает runtime remote и получает `models/best_model.joblib` согласно DVC metadata образа. При ошибке обязательной загрузки процесс не начинает принимать запросы. Каталог models должен быть доступен для записи: read-only mount несовместим с загрузкой новой модели.

Для проверки без облака:

```bash
uv run python -m pytest tests/test_dvc_startup.py -q
uv run python -m scripts.smoke_dvc_container --image credit-backend:study
```

Smoke-сценарий создает отдельный локальный DVC remote и синтетическую модель, запускает контейнер с пустым кешем, проверяет `/health` и `/predict`, затем удаляет временный контейнер. Настоящий `dvc.lock` и модель проекта не заменяются.

Здесь проверяется цепочка «метаданные → получение артефакта → запуск → ответ». Она не измеряет качество PD-модели. В обычном ONNX Compose-сценарии используются FP32 ONNX и scaler, подготовленные на этапе 1; не смешивайте эти два способа получения модели.

## Шаг 3 Прочитать Kubernetes-манифесты

| Ресурс | Что он делает |
| --- | --- |
| Deployment | Поддерживает количество реплик и обновляет ревизии |
| Service | Дает стабильное имя и адрес группе Ready pod |
| Ingress | Описывает входной HTTP-маршрут; требует ingress controller |
| ConfigMap | Передает несекретную конфигурацию |
| Secret | Передает runtime-доступы отдельно от образа |
| NetworkPolicy | Ограничивает допустимые соединения |
| PodDisruptionBudget | Ограничивает добровольное одновременное выключение реплик |

`base/` содержит общие ресурсы, overlays/local, staging и production задают отличия. Kustomize объединяет их в итоговый YAML; это не шаблонизатор shell, поэтому произвольные переменные `.env` сами в манифесты не подставляются.

```bash
kubectl kustomize infrastructure/kubernetes/overlays/local > /tmp/scoring-local-rendered.yaml
kubectl kustomize infrastructure/kubernetes/overlays/staging > /tmp/scoring-staging-rendered.yaml
```

Рендеринг проверяет сборку конфигурации без отправки в кластер. Placeholder `REPLACE_` в облачном overlay должен быть заменен перед реальным deploy; для учебного рендеринга облачные credentials не нужны.

## Шаг 4 Разобрать probes и rolling update

`/live` проверяет жизнь процесса, `/ready` — готовность обслуживать запрос с моделью. Startup probe дает время на получение артефакта. Если readiness ложно, Service не направляет новый трафик в такой pod. Liveness не должна постоянно убивать исправный процесс только из-за временной недоступности внешнего сервиса.

RollingUpdate имеет `maxUnavailable=0`, `maxSurge=1`. Контроллер сначала поднимает дополнительную новую реплику и ждет готовности, затем удаляет старую. Поэтому нужна свободная емкость для лишнего pod. Если candidate не готов, старые реплики должны продолжить обслуживание.

PDB помогает при добровольном eviction, но не делает неуязвимой машину, на которой размещены все реплики. Антиаффинность и реальные границы отказа — отдельная тема.

## Шаг 5 Выполнить локальную интеграцию

```bash
bash scripts/test_kubernetes_local.sh
```

Нужны Docker, kind, kubectl, curl, rg и зависимости проекта. Скрипт создает собственный двухузловой kind, ставит Calico и Traefik, собирает оба образа и использует синтетическую DVC-модель. Доступ проверяется через Ingress с Host `credit.local`.

Проверяются API и форма frontend, rolling restart под трафиком, неготовая ревизия, восстановление и реальные разрешения/запреты NetworkPolicy. Результаты сохраняются в `infrastructure/.evidence/stage3/`, кластер по умолчанию удаляется.

Для учебного осмотра можно оставить только этот тестовый кластер:

```bash
KEEP_CLUSTER=1 bash scripts/test_kubernetes_local.sh
```

Сценарий напечатает context/временный kubeconfig и имя кластера. Используйте именно эти значения для диагностики. После изучения удалите только напечатанный кластер командой `kind delete cluster --name <имя>`.

## Шаг 6 Научиться локализовать отказ

```bash
kubectl --context <тестовый-context> -n mlops get pods
kubectl --context <тестовый-context> -n mlops get services
kubectl --context <тестовый-context> -n mlops describe pod <pod>
kubectl --context <тестовый-context> -n mlops logs <pod>
```

Замените угловые скобки настоящими значениями. Проверяйте путь последовательно: процесс → readiness → endpoint Service → ingress controller → маршрут. У pod может быть статус Running при неготовой модели; такой pod не должен получать рабочий трафик.

| Симптом | Вероятная причина |
| --- | --- |
| ImagePullBackOff | Не загружен образ или нет registry access |
| Pending | Не хватает ресурсов или не совпадает nodeSelector |
| CrashLoopBackOff | Ошибка entrypoint, DVC или прав записи |
| Pod Ready, но frontend не отвечает | Неправильный Service/порт/API_BASE_URL или NetworkPolicy |
| Ingress есть, маршрута нет | Нет controller, неверный class/Host |

## Результат и упражнение

Для сдачи нужны Dockerfile, конфигурации запуска, Kubernetes manifests и протокол интеграции. Подготовка облачного кластера не требуется. Объясните, почему нельзя использовать `/live` вместо проверки загрузки модели и почему синтетическая модель полезна при тестировании доставки.

Далее — [CI/CD и релизные стратегии](04_cicd_and_releases.md).
