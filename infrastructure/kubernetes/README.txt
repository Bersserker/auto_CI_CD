Этап 3. Контейнеризация и оркестрация
====================================

Состав
------
base/                  Deployment backend/frontend, Service, ConfigMap, Ingress,
                       ServiceAccount, PDB и NetworkPolicy.
overlays/local/        Тестовый DVC remote на узле kind, синтетическая модель.
overlays/staging/      Cloud image/tag, бакет и TLS-домен тестового окружения.
overlays/production/   Свои image/tag, бакет, домен и 3 реплики каждого компонента.
local/traefik.yaml     Ingress controller для локальной проверки.

В base/local/staging по 2 реплики. RollingUpdate: maxUnavailable=0, maxSurge=1,
minReadySeconds=5, progressDeadlineSeconds=900. Запрашиваются CPU-узлы с меткой
workload=cpu (они уже описаны модулем Terraform). PodAntiAffinity предпочитает
разные узлы; локально реплики могут работать на одном worker.
PDB сохраняет минимум одну доступную реплику при добровольном eviction.
Для обновления с maxSurge нужен запас ресурсов на дополнительный pod.

Probes:
backend startup/readiness: /ready (проверка загрузки модели), liveness: /live
(процесс API). /health сохранен для внешней проверки API с моделью.
Startup допускает до 10 минут на получение модели; readiness не включает
неподготовленный pod в Service. Backend получает SIGTERM через exec и имеет
25 секунд на завершение запросов, общий terminationGracePeriodSeconds=45.
Frontend probes: /_stcore/health. Доступность backend не влияет на liveness UI.
Существующие Streamlit WebSocket-сессии при замене pod могут переподключиться;
sticky cookie Service помогает удерживать сессию на одной реплике.

ConfigMaps содержат DVC URL/endpoint/region, API_BASE_URL и OMP_NUM_THREADS.
Secret s3-credentials создается отдельно из окружения. Ключи не хранятся в YAML.
Frontend не получает ключи S3 и не скачивает модель. Backend скачивает ровно
models/best_model.joblib по хешу dvc.lock из образа. /app/models — writable
emptyDir; каждая новая реплика получает версию модели самостоятельно.
API и frontend работают под UID/GID 10001, токен Kubernetes API не монтируется.

Локальная проверка без Yandex Cloud
-----------------------------------
Требуются Linux amd64, Docker с docker image save --platform, kubectl 1.35.0,
kind 0.33.0, curl, rg и Python-окружение проекта с DVC/sklearn. Для скачивания
пакетов и инфраструктурных образов нужен интернет; облачный аккаунт не нужен.
В рабочем каталоге проекта:

uv sync --locked
bash scripts/test_kubernetes_local.sh

Если инструменты установлены в /tmp/mlops-k8s-tools:
PATH="/tmp/mlops-k8s-tools:$PATH" bash scripts/test_kubernetes_local.sh

Сценарий строит ОБА образа, создает отдельный kind-кластер с двумя узлами,
загружает образы через Docker хоста, устанавливает Calico 3.33.0 и Traefik 3.6.2,
создает фиктивный S3 Secret и локальный DVC remote с синтетической DummyClassifier.
Исходный dvc.lock и настоящие модели не меняются. Все kubectl-запросы идут
в явно выбранный context, kubeconfig пользователя не меняется.

Проверяет:
- API /health и /predict через Ingress с Host: credit.local;
- frontend HTTP и отправку настоящей формы Streamlit на backend Service;
- rolling restart обоих Deployment под непрерывными запросами /health, /predict;
- недоступную новую ревизию backend: старые Ready-реплики остаются доступны;
- возврат к рабочей ревизии и реальное действие NetworkPolicy (allow/deny).

Результаты: infrastructure/.evidence/stage3/result.json, pods.txt, events.txt.
По умолчанию тестовый кластер и временные данные удаляются, образы сохраняются
в Docker cache. Чтобы оставить кластер для просмотра:
KEEP_CLUSTER=1 bash scripts/test_kubernetes_local.sh

Скрипт напечатает имя кластера и временный KUBECONFIG. После просмотра удалите
только этот кластер: kind delete cluster --name <напечатанное-имя>.
Не выполняйте kind delete clusters --all: это затронет чужие кластеры.

Установка инструментов в /tmp (не требует изменения системных каталогов)
------------------------------------------------------------------------
mkdir -p /tmp/mlops-k8s-tools
curl -fLsS https://kind.sigs.k8s.io/dl/v0.33.0/kind-linux-amd64 -o /tmp/mlops-k8s-tools/kind
curl -fLsS https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-linux-amd64.sha256sum -o /tmp/mlops-k8s-tools/kind.sha256sum
curl -fLsS https://dl.k8s.io/release/v1.35.0/bin/linux/amd64/kubectl -o /tmp/mlops-k8s-tools/kubectl
curl -fLsS https://dl.k8s.io/release/v1.35.0/bin/linux/amd64/kubectl.sha256 -o /tmp/mlops-k8s-tools/kubectl.sha256

Проверьте sha256sum файлов против загруженных контрольных сумм перед запуском,
затем chmod +x /tmp/mlops-k8s-tools/kind /tmp/mlops-k8s-tools/kubectl.
kind node закреплен digest-ом в scripts/test_kubernetes_local.sh.
Calico/Traefik версии закреплены в сценарии и local/traefik.yaml.

Когда будет настроен Yandex Cloud
--------------------------------
1. Разверните кластер этапа 2. У CPU-узлов должна быть метка workload=cpu,
   у kubeconfig — доступ к API кластера. Calico уже включен в Terraform:
   повторно ставить Calico manifest в Managed Kubernetes НЕ нужно.
2. Подготовьте Registry, загрузите оба образа с одним уникальным release tag:

docker build -t cr.yandex/<registry-id>/credit-backend:<release-tag> .
docker build -f src/frontend/Dockerfile -t cr.yandex/<registry-id>/credit-frontend:<release-tag> .
docker push cr.yandex/<registry-id>/credit-backend:<release-tag>
docker push cr.yandex/<registry-id>/credit-frontend:<release-tag>

   Применяйте один overlay к одному кластеру: staging и production используют
   namespace mlops в разных кластерах этапа 2.
   Доступ Docker к Registry настраивается отдельно через yc/iam. Node SA
   Terraform имеет container-registry.images.puller. Модель должна быть
   опубликована в DVC remote по хешу dvc.lock, включенному в этот образ.
   Команды публикации модели: docker/README.txt.
3. В нужном overlays/staging или overlays/production замените ВСЕ REPLACE_:
   registry, tag, имя бакета артефактов и домен. Для точного закрепления образа
   можно использовать digest в Kustomize images вместо tag.
4. Установите обслуживаемый ingress controller Traefik в namespace ingress-system
   (в облаке предпочтителен официальный Helm chart). Его pods должны иметь
   дополнительную метку app=traefik: под нее настроены NetworkPolicy.
   Класс Ingress — traefik. Нужен Service LoadBalancer с HTTPS entrypoint,
   DNS на адрес балансировщика и правила security groups для его портов/NodePort
   согласно выбранной конфигурации Yandex Cloud. local/traefik.yaml содержит
   только HTTP ClusterIP и предназначен для локальных тестов через port-forward.
5. Подготовьте namespace и Secret без записи ключей в Git/аргументы команды:

kubectl --context <context> create namespace mlops --dry-run=client -o yaml | kubectl --context <context> apply -f -
# Экспортируйте AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY в своей оболочке.
uv run --no-sync python scripts/create_kubernetes_secret.py --context <context>

   Используйте для скачивания отдельные S3-ключи с правом чтения бакета
   артефактов. Для публикации модели требуется доступ на запись.
   Создайте TLS Secret из настоящих файлов сертификата и ключа:

kubectl --context <context> -n mlops create secret tls credit-tls --cert=/path/fullchain.pem --key=/path/privkey.pem

6. Просмотрите и примените конфигурацию:

kubectl kustomize infrastructure/kubernetes/overlays/staging
bash scripts/deploy_kubernetes.sh staging <context>

   Deploy проверяет IngressClass, s3-credentials и облачный TLS Secret;
   откажется работать с REPLACE_, выполнит server-side
   dry-run, apply и дождется rollout. Для production используйте его overlay.
   Содержимое секретов и kubeconfig не включайте в публичные логи.

Проверка и обновление
--------------------
curl --fail https://<domain>/health
curl --fail -H 'Content-Type: application/json' --data @request.json https://<domain>/predict

request.json должен содержать 23 поля CreditRequest; тестовый пример можно
получить scripts/prepare_local_model.py, он сохраняет request.json в отдельный
каталог. Это проверка контракта, не оценка качества кредитной модели.
Для проверки через port-forward контроллера без готового внешнего LB:

uv run --no-sync python -m scripts.verify_kubernetes --context <context> --host <domain> --request-file request.json

Проверка делает настоящий запрос из frontend pod к Service backend, затем
использует Ingress controller через localhost port-forward. Внешний DNS/LB/TLS
нужно отдельно проверить командами curl выше. --check-rollout перезапускает оба
Deployment и проверяет отсутствие ошибок запросов; запускайте это осознанно.
Негативный --check-failed-rollout разрешен только для локального kind-теста.

После изменения ConfigMap или Secret существующим pod нужен новый rollout:
kubectl --context <context> -n mlops rollout restart deployment/backend deployment/frontend

Обновление образов: измените release tag/digest в overlay и повторите deploy.
Ручной откат к предыдущей ревизии:
kubectl --context <context> -n mlops rollout undo deployment/backend
kubectl --context <context> -n mlops rollout status deployment/backend

Историю rollout проверяйте вместе с версией ConfigMap/Secret: undo Deployment
не откатывает содержимое этих объектов. ConfigMaps для разных release при
необходимости версионируйте отдельно. Автоматический rollback — этап 4.

Диагностика
-----------
kubectl --context <context> -n mlops get pods,deploy,svc,ingress,endpointslices
kubectl --context <context> -n mlops describe pod <pod>
kubectl --context <context> -n mlops logs deployment/backend --tail=100
kubectl --context <context> -n mlops logs <pod> --previous
kubectl --context <context> -n mlops get events --sort-by=.lastTimestamp

ImagePullBackOff: проверьте имя образа, tag, Registry-доступ node SA/Secret.
CreateContainerConfigError: проверьте Secret и обязательные ключи.
DVC pull failed: проверьте URL, S3-ключи, наличие хеша модели, DNS/HTTPS и квоты.
Pending: проверьте workload=cpu, taints и запас ресурсов для maxSurge.
Startup/Readiness failed: проверьте /ready, загрузку модели и логи backend.
Ingress 404/503: Host/class, контроллер, EndpointSlices и его метку app=traefik.
NetworkPolicy: Calico, DNS kube-dns, ingress-system и app=frontend/backend.
Для Istio/Redis/новых зависимостей понадобятся дополнительные точечные правила.

Проверка этапа 3 локально не подтверждает IAM, GPU, Object Storage или публичный
балансировщик Yandex Cloud. Облачное развертывание ожидает аккаунта и настоящей
обученной модели. Результаты фактического локального запуска: verification.txt.

Источники
---------
https://kubernetes.io/docs/concepts/workloads/controllers/deployment/
https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/
https://kind.sigs.k8s.io/docs/user/quick-start/
https://docs.tigera.io/calico/latest/getting-started/kubernetes/kind
https://doc.traefik.io/traefik/v3.6/reference/install-configuration/providers/kubernetes/kubernetes-ingress/
