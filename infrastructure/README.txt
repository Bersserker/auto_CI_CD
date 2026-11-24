Этап 2 — Cloud Infrastructure as Code
====================================

Выбор: Yandex Cloud. В проекте уже есть примеры его Terraform-провайдера;
Managed Kubernetes, GPU node groups, S3-совместимое хранилище и Monitoring
позволяют описать требуемую инфраструктуру одним провайдером. Это обоснование
архитектурного выбора, а не сравнение стоимости облаков. Стоимость CPU/GPU
нужно оценить в калькуляторе облака перед apply.

Состав
------
modules/network    VPC, подсеть учебной VM, три подсети Kubernetes, SG для VM.
modules/kubernetes Региональный master a/b/d, CPU/GPU node groups, SG,
                   CALICO, отдельные SA master/nodes, KMS, аудит и логи.
modules/storage    Приватный versioned Object Storage, запрет HTTP,
                   очистка незавершенных загрузок через 7 дней.
modules/monitoring Cloud Logging и dashboard RAM/диск узлов, CPU master.
modules/compute    Учебная VM из примера; по умолчанию выключена.
bootstrap          Создание бакета Terraform state с локальным bootstrap state.
environments       Независимые корневые конфигурации staging и production.
kubernetes         Политики сети namespace mlops.
scripts            Локальные проверки и будущая проверка в облаке.

Staging: одна CPU-группа с 1–3 узлами, одна GPU-группа с 1–2 узлами.
Production: CPU-группы в a/b/d по 1–4 узла и GPU-группа в a с 1–2 узлами.
CPU: standard-v3, 2 vCPU, 4 GiB. GPU: gpu-standard-v2, 8 vCPU, 48 GiB, 1 V100.
Для каждой группы GPU добавлен taint nvidia.com/gpu=present:NoSchedule.
GPU workload должен указать nodeSelector workload=gpu, соответствующий
toleration и requests/limits nvidia.com/gpu: 1. Обычные workload используют CPU.

Staging: подсети VM 10.2.0.0/24, K8s 10.20.0.0/24, 10.20.1.0/24, 10.20.2.0/24.
Production: VM 10.3.0.0/24, K8s 10.30.0.0/24, 10.30.1.0/24, 10.30.2.0/24.
Pod CIDR: 10.112.0.0/16, service CIDR: 10.96.0.0/16 в каждой отдельной VPC.
При будущей связности VPC задайте разные pod/service CIDR перед развертыванием.

Требования
----------
Terraform >=1.7,<2; провайдер ~>0.250.0, зафиксирован lock-файлами.
Для статических проверок YAML: Python 3 с PyYAML (python3 -m pip install PyYAML).
Для облачных проверок дополнительно: yc CLI, kubectl, Python 3; для скриптов Bash.
Для первичной загрузки провайдера нужен интернет или локальное зеркало.
Явно укажите поддерживаемую версию Kubernetes: yc managed-kubernetes list-versions.
Версия master и узлов одинакова. Версия 1.33 в mock-тестах — тестовое значение,
а не обещание доступности в облаке.

Локальные проверки без аккаунта
-------------------------------
bash infrastructure/scripts/check-local.sh

Выполняются fmt, init -backend=false, validate и terraform test с mock_provider.
14 тестовых сценариев: staging 4, production 4, Kubernetes 3, Storage 2, bootstrap 1.
Тесты не создают ресурсы и не читают ключи облака. При первом init скачивается
провайдер. Повторные проверки используют установленный провайдер.
Обычный terraform test в корне репозитория не ищет тесты в этих каталогах.
Mock-тесты не проверяют права, квоты, доступность зон/версий и работу сети.

Подготовка доступа (когда будет настроено облако)
------------------------------------------------
1. Подготовьте cloud_id, folder_id и локальный JSON-ключ deploy-сервисного аккаунта.
   Требуются права на VPC, Kubernetes, IAM-аккаунты/назначения, KMS, Storage,
   Cloud Logging и Monitoring. Для назначения ролей нужны полномочия IAM admin;
   обычной роли editor недостаточно для всех IAM-операций. Ограничьте права
   каталогом проекта. Deploy-аккаунт отличается от создаваемых master/nodes SA.
2. Проверьте квоты регионального master, CPU, дисков, IP и GPU. GPU-квота по
   умолчанию нулевая; запросите ее заранее и проверьте наличие V100 в зоне a.
3. Скопируйте bootstrap/terraform.tfvars.example в bootstrap/terraform.tfvars,
   заполните ID, путь к JSON и глобально уникальное имя state-бакета.
   Все команды ниже выполняются из корня репозитория.

terraform -chdir=infrastructure/bootstrap init
terraform -chdir=infrastructure/bootstrap plan -out=bootstrap.tfplan
terraform -chdir=infrastructure/bootstrap apply bootstrap.tfplan

Bootstrap использует IAM-аутентификацию провайдера и не генерирует статические
ключи в Terraform. Сохраните его локальный state в защищенной резервной копии:
он нужен для управления state-бакетом. Не уничтожайте bootstrap при удалении
окружений. Непустой бакет защищен force_destroy=false.

Remote state и окружения
-----------------------
1. Скопируйте terraform.tfvars.example -> terraform.tfvars и
   backend.hcl.example -> backend.hcl в каждом нужном окружении.
2. Заполните cloud/folder ID, локальный путь к JSON, версию Kubernetes,
   глобально уникальное имя бакета артефактов (разное для staging/production).
   В backend.hcl задайте имя state-бакета, созданного bootstrap.
3. Для S3 backend передайте статические ключи аккаунта с доступом к state-бакету
   через AWS_ACCESS_KEY_ID и AWS_SECRET_ACCESS_KEY в своей оболочке.
   JSON-ключ провайдера и S3-ключи backend имеют разное назначение.
   Не записывайте секреты в командную историю, .tf или backend.hcl.
4. Инициализируйте backend и просмотрите план:

terraform -chdir=infrastructure/environments/staging init -backend-config=backend.hcl
terraform -chdir=infrastructure/environments/staging plan -out=release.tfplan
terraform -chdir=infrastructure/environments/staging apply release.tfplan

Для production повторите команды с его каталогом. По умолчанию включены
кластер и GPU; VM выключена. enable_gpu=false разрешен для CPU-разработки,
но такой запуск не подтверждает полное выполнение этапа 2.
State keys: staging/terraform.tfstate и production/terraform.tfstate.
Backend S3 не настраивает распределенную блокировку: строго сериализуйте
plan/apply для одного окружения, в том числе между разными компьютерами/CI.
Версионирование бакета дает историю state, но не заменяет блокировку.
Для будущего совместного CI используйте внешнюю сериализацию или YDB-locking.

API и сетевые политики
----------------------
API по умолчанию приватный. Нужен VPN/bastion с маршрутом в VPC и разрешенный
CIDR в kubernetes_api_allowed_cidrs. При необходимости публичного API включите
kubernetes_public_ip=true и разрешите только свой внешний IP/32.

bash infrastructure/scripts/configure-kubectl.sh staging
# Для публичного API: configure-kubectl.sh staging --external
bash infrastructure/scripts/apply-network-policies.sh

Политики применяются в ТЕКУЩИЙ kubectl context — проверьте его перед запуском.
Они создают namespace mlops, default-deny ingress/egress, разрешают DNS,
frontend -> backend:8000, istio-system -> frontend:8501 / backend:8000,
monitoring -> backend:8000 и backend -> публичные HTTPS endpoints.
Имена app=frontend/backend и порты должны совпадать с будущими манифестами.
Перед интеграцией Istio добавьте правила для его control plane и sidecars,
перед Redis/другими внутренними зависимостями — точечные разрешения для них.
CALICO реализует NetworkPolicy; SG ограничивают сетевой доступ к узлам/master.
Системные namespaces этими политиками не затрагиваются.

Проверка реального результата и воспроизводимости
------------------------------------------------
bash infrastructure/scripts/test-network-policy.sh
bash infrastructure/scripts/verify-cloud.sh staging

Первый скрипт создает временный namespace, HTTP-сервер и два клиента и проверяет,
что выбранный клиент проходит, а другой блокируется. Namespace удаляется trap.
Для загрузки probe-образа нужен доступ узлов к registry; можно задать PROBE_IMAGE.
Второй проверяет remote backend, соответствие kubectl context кластеру,
RUNNING/HEALTHY, Ready CPU/GPU-узлы и наличие политик, сохраняет свидетельства
в infrastructure/.evidence/staging и требует пустой повторный terraform plan
(exit 0). Exit 2 означает оставшиеся изменения, и проверка не пройдена.
Повторите для production. В namespace mlops должны появиться все политики.

Для проверки восстановления с нуля создайте отдельное тестовое окружение с
отдельными именами бакетов, state key и непересекающимися CIDR, примените тот же
код и повторите проверки. Не удаляйте production ради демонстрации.
Dashboard URL и log_group_id доступны через terraform output.
Проверьте поступление метрик/логов в консоли после появления узлов.
Проверки наличия ресурсов не заменяют анализ реальных метрик и содержимого логов.
Prometheus/Grafana, логи приложения и алерты относятся к этапу 5.

Состояние выполнения
--------------------
Конфигурация и локальные проверки подготовлены; облачных apply не выполнялось.
Полное завершение этапа 2 требует настоящего remote state, работающих CPU/GPU
кластеров и сохраненных результатов облачных проверок. Пункты task.md поэтому
не отмечены выполненными. Локальный отчет: verification.txt.

Секреты, state, планы, .terraform и .evidence исключены из Git. Lock-файлы
включаются в Git. Проверяйте diff перед коммитом: имена ключей с произвольными
именами не покрываются всеми правилами .gitignore.

Источники
---------
https://yandex.cloud/en/docs/terraform/tutorials/terraform-state-storage
https://yandex.cloud/en/docs/managed-kubernetes/operations/connect/security-groups
https://yandex.cloud/en/docs/managed-kubernetes/concepts/node-group/node-group-gpu
https://yandex.cloud/en/docs/compute/concepts/gpus
https://yandex.cloud/en/docs/terraform/resources/storage_bucket
https://yandex.cloud/en/docs/managed-kubernetes/metrics
