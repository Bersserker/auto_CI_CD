Подготовка DVC и backend к развертыванию
=======================================

Аккаунт Yandex Cloud для подготовки и локальных тестов не требуется.
Реальное подключение к Object Storage выполняется после создания аккаунта.

Что готово
----------
- model-storage: DVC 3.67.1 с поддержкой S3, отдельная группа зависимостей.
- serving: зависимости текущего API и классических моделей без Torch/Jupyter.
- Dockerfile устанавливает эти группы из uv.lock; dev-группа не устанавливается.
- В образ включены dvc.yaml/dvc.lock, .dvc/config и скрипт старта, но не Git,
  кеш DVC, модели, .env, локальная конфигурация и облачные ключи.
- DVC_PULL=1: настройка remote -> pull models/best_model.joblib -> запуск API.
- DVC_PULL=0: разработка с уже подключенным файлом models/best_model.joblib.
- Если обязательный pull не удался, API не запускается, даже если есть старая
  модель. Если нужная версия уже есть в кеше/рабочем каталоге, DVC может
  восстановить ее без сети — это штатное поведение.
- DVC использует metadata dvc.lock из образа, поэтому версия модели связана
  с версией образа. Для нового релиза пересобирается образ с новым dvc.lock.

Runtime env
-----------
DVC_PULL               0 или 1; по умолчанию 0.
DVC_REMOTE_URL         s3://<бакет-артефактов>/dvc, либо абсолютный локальный путь.
DVC_S3_ENDPOINT_URL    https://storage.yandexcloud.net по умолчанию.
AWS_DEFAULT_REGION    ru-central1 по умолчанию.
AWS_ACCESS_KEY_ID     статический S3 access key ID; только при запуске.
AWS_SECRET_ACCESS_KEY статический S3 secret key; только при запуске.

Remote записывается в .dvc/config.local, который исключен из Git и build context.
Статические ключи скрипт в конфигурацию не записывает: DVC читает их из окружения.
Проектный .dvc/config содержит только общие настройки cache/analytics.
В контейнере core.no_scm=true задается отдельно; .git в образе не требуется.
Директории models, .dvc и временный кеш доступны пользователю app (UID 10001).
Для DVC_PULL=1 нужен writable models; нельзя оставлять read-only bind mount.

Сейчас: локальная проверка без облака
------------------------------------
Из корня репозитория:

uv sync --locked
uv run --no-sync python -m pytest tests/test_dvc_startup.py -v
docker build -t credit-backend:dvc .
uv run --no-sync python -m scripts.smoke_dvc_container --image credit-backend:dvc

Тесты создают изолированные DVC-репозитории в /tmp. Сквозная проверка публикует
синтетическую DummyClassifier в локальный remote, запускает образ с пустым кешем,
проверяет /health и /predict и удаляет временный контейнер. Порты хоста не
публикуются. Исходные dvc.lock, данные и модели проекта не меняются.
Это проверка доставки и API, а не качества кредитной модели.
Docker должен быть запущен и доступен текущему пользователю.

Запуск с существующей локальной моделью:
docker compose up --build
Интерфейс http://localhost:8501, API http://localhost:8000.
Нужен models/best_model.joblib, совместимый с текущим API. Файлы best_NN.joblib
и ONNX не являются заменой для текущего API: их интеграция — отдельная задача.

Когда появится аккаунт
---------------------
1. Создайте приватный бакет артефактов (Terraform module storage этапа 2).
   Используйте его имя artifacts_bucket_name; бакет terraform state не подходит.
2. Создайте сервисный аккаунт и статические S3-ключи. Для публикации нужны
   права записи в бакет, например storage.editor на конкретный бакет.
   Для API, который только скачивает, используйте отдельные read-only ключи
   с storage.viewer на бакет. Не назначайте runtime доступ к terraform state.
3. Скопируйте .env.dvc.example в .env и замените DVC_REMOTE_URL настоящим URL.
   Передайте ключи через окружение или локальный .env (не добавляйте его в Git).
   Compose читает .env; uv run/скрипт Python автоматически .env НЕ читают.
   Для команд публикации экспортируйте DVC_REMOTE_URL и AWS-переменные в оболочке.
4. Настройте remote в рабочем репозитории:

uv run --no-sync python scripts/configure_dvc_remote.py
uv run --no-sync dvc remote list

5. Получите реальную обученную модель, актуальный dvc.lock и локальный DVC cache.
   Если их нет, выполните существующий pipeline подготовки/обучения проекта.
   Затем опубликуйте модель (и при необходимости все данные):

uv run --no-sync dvc push models/best_model.joblib
# Для всех отслеживаемых данных и артефактов: uv run --no-sync dvc push

   Нельзя просто положить произвольный joblib в бакет: DVC нужен объект по хешу
   из dvc.lock. Не запускайте dvc add на модели, уже объявленной в dvc.yaml.
   Зафиксируйте настоящий dvc.lock и исходники в Git; креды и config.local — нет.
   Текущий lock описывает предыдущий train; после изменения зависимостей
   обновляйте его настоящим обучением/валидацией, а не правкой хешей вручную.
6. Соберите и запустите DVC-вариант Compose:

docker compose -f docker-compose.yml -f docker-compose.dvc.yml up --build

Override включает DVC_PULL=1 и заменяет read-only bind models именованным writable
томом fetched-models. При новом dvc.lock pull обновляет модель в томе.
Перезапуск с пустым кешем при облачном remote требует доступного Object Storage.
После запуска проверьте curl --fail http://localhost:8000/health и интерфейс.
Не выполняйте docker compose config с настоящими ключами в публичных логах:
вывод конфигурации может содержать интерполированные секреты.

Kubernetes
----------
Deployment backend описан в infrastructure/kubernetes/base. Он получает DVC-переменные через ConfigMap,
AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY через Secret и writable /app/models
(например emptyDir). ENTRYPOINT образа уже выполнит pull при старте контейнера.
Не включайте .env в образ и не передавайте ключи через build args.
Используйте readiness/startup probe на /health с временем на загрузку модели;
liveness процесса следует отделить от проверки готовности модели.
NetworkPolicy backend разрешает DNS и публичный HTTPS, необходимые для S3.
Манифесты и сценарий локального Kubernetes-теста: infrastructure/kubernetes/README.txt.
Cloud-доступ, права и наличие настоящего артефакта еще не проверены.

Источники:
https://doc.dvc.org/user-guide/data-management/remote-storage/amazon-s3
https://doc.dvc.org/user-guide/project-structure/configuration
https://docs.astral.sh/uv/concepts/dependencies/

Проверено локально 2026-11-24
---------------------------
Backend credit-backend:dvc успешно собран.
8 тестов tests/test_dvc_startup.py прошли (включая выборочную загрузку модели
без остальных outputs train, обновление версии и отказ запуска при ошибке).
Сквозной smoke_dvc_container прошел: загрузка из пустого кеша, /health 200,
/predict 200 с ожидаемым результатом синтетической модели. Контейнер удален.
uv lock --check --offline и оба варианта Compose проверены.
Yandex Object Storage и настоящая кредитная модель этой проверкой не проверялись.
