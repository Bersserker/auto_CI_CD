ЭТАП 4.2: BLUE-GREEN И CANARY НА ISTIO

Назначение
  scripts/ci/istio_delivery.py — общий контроллер локального теста и GitHub Actions.
  Для каждого окружения нужны отдельный кластер и namespace mlops.
  По два Deployment, Service и ConfigMap на слот: backend-blue/frontend-blue и
  backend-green/frontend-green. Реплики берутся из environment overlay: staging=2,
  production=3 на каждый компонент каждого слота. Планируйте ёмкость на оба слота
  и дополнительную реплику при обновлении неактивного слота.

  Istio Gateway завершает TLS. DestinationRule выбирает pod subset по slot, а
  один VirtualService меняет веса одновременно для backend и frontend.
  Frontend каждого цвета обращается к backend того же цвета через отдельный
  Service. Настройки/DVC metadata неактивного цвета не изменяют активный.
  Внешний API /health, /predict, /ready, /live идёт в backend; остальные пути,
  включая Streamlit WebSocket, — во frontend. Веса относятся к новым запросам;
  уже установленные WebSocket-соединения не перемещаются между pod.

  Используется Gateway traffic management без sidecar/ambient в приложении.
  Namespace помечен istio-injection=disabled, pod — label inject=false. Поэтому
  DestinationRule использует plaintext HTTP от Gateway до приложения; внутренний
  mTLS данным вариантом не предоставляется. NetworkPolicy сохраняется: DNS,
  frontend -> backend, Istio ingress -> приложение, backend -> публичный HTTPS/S3.

Файлы
  operator.yaml        Установка Istiod и ingressgateway, версия Istio 1.32.0.
  gateway.yaml         TLS Gateway, secret credit-tls рядом с gateway workload.
  destinations.yaml    Слоты blue/green для обоих компонентов.
  kustomization.yaml   Общие Services/NetworkPolicy/PDB/SA, без rolling Deployment.
  release-policy.yaml  Общие критерии допуска и веса Canary.
  VirtualService и ресурсы слотов формирует контроллер из проверенного коммита.

Алгоритм
  1. Проверить текущий active через настоящий Gateway, а не прямой pod endpoint.
  2. Записать pending-транзакцию в ConfigMap mlops-release-state. При перезаписи
     прежнего inactive он теряет статус previous: старая версия уже не сохранена.
  3. Собрать неактивный цвет из manifests/digest проверенного релиза. Server-side
     dry-run, apply, ожидание Ready обоих Deployment (до 15 минут на каждый).
  4. Проверить candidate через authenticated kubectl port-forward: health,
     predict и Streamlit health. В frontend pod отправить настоящую Streamlit-форму
     и получить ответ через Service своего backend.
  5. Blue-Green: перевести 100% новых запросов на готовый candidate.
     Canary: 10 -> 25 -> 50 -> 100%, проверка на каждой ступени.
  6. Подтвердить применение xDS конфигурации через response header x-mlops-route
     на API и UI (до 45 секунд), затем выполнить измерения.
  7. Только после последней проверки записать active=candidate, previous=старый.
     Старый слот остаётся запущенным. Следующий релиз перезапишет его как inactive.

Критерии и остановка продвижения
  release-policy.yaml — минимум 60 выборок на ступень, интервал 1 секунда. Если
  редкий subset ещё не наблюдался, допускается до 180 выборок. Каждая выборка:
  /health, /predict и /_stcore/health (минимум 180 HTTP-запросов на ступень).
  - Каждый ответ HTTP 200; ни одной transport/HTTP/JSON/contract ошибки.
  - /health: status=ok; predict: prediction 0/1, конечная probability в [0,1].
  - p95 всех запросов <=1 секунды; отдельный запрос <=5 секунд.
  - Каждый ненулевой subset фактически наблюдается и для API, и для UI;
    response headers содержат ожидаемые slot, commit и поколение маршрута.
  - Ready и успешная форма frontend обязательны до переключения трафика.
  Любая ошибка останавливает продвижение. Малую долю нельзя подтверждать только
  YAML весами: если candidate не наблюдался, релиз отклоняется. При необходимости
  увеличьте samples в policy и проверьте это изменение в CI.
  Это критерии доступности, latency и контракта. Оценка ROC-AUC/качества реальной
  модели по размеченным данным и production SLO относятся к отдельным ML/monitoring
  gates; прохождение этих probes их не заменяет.

Rollback и восстановление
  При ошибке controller возвращает 100% на baseline, ждёт применения маршрутов и
  повторяет измерения. Pending снимается только после успешного восстановления;
  исходный deploy остаётся неуспешным. Images и конфигурация baseline не меняются.
  Ошибка сохранения нового active также вызывает rollback к исходному baseline.
  Оптимистическая блокировка resourceVersion защищает запись state от потери
  конкурентных обновлений; GitHub jobs дополнительно сериализованы по окружению.

  Ручной Rollback переключает на previous. Если pending остался после аварии,
  Rollback возвращает текущий active вместо недостоверного inactive и снимает
  pending. Новый deploy при pending запрещён. SIGTERM обрабатывается с попыткой
  восстановления, но SIGKILL/потеря runner/недоступность API не позволяют обещать
  автоматический откат. После восстановления связи запустите workflow Rollback.
  Если целевой previous провалит gate, controller вернётся на исходный active.

  При первой установке baseline ещё нет: требуйте ALLOW_INITIAL_DEPLOY=true.
  Неуспешный bootstrap удаляет VirtualService, не объявляет candidate здоровым
  и не притворяется успешным rollback. Исправьте причину и повторите bootstrap.
  После первой успешной доставки снимите ALLOW_INITIAL_DEPLOY.
  Secrets и DVC remote не откатываются. Сохраните обе версии модели в remote и
  обеспечьте доступ обоим слотам до завершения периода rollback.
  Не редактируйте state/VirtualService вручную и не запускайте параллельные CLI
  операции; используйте контроллер и общую concurrency group workflows.

Установка в будущем облачном кластере
  Версия 1.32.0 выбрана для локального Kubernetes 1.35. Перед установкой в YC
  сверяйте версии с официальной матрицей совместимости:
  https://istio.io/latest/docs/releases/supported-releases/

  bash scripts/ci/install_istio.sh
  export PATH="$HOME/.local/bin:$PATH"
  istioctl --context YOUR_CONTEXT install \
    -f infrastructure/kubernetes/istio/operator.yaml -y
  kubectl --context YOUR_CONTEXT -n istio-system rollout status \
    deployment/istio-ingressgateway --timeout=5m

  operator.yaml создаёт LoadBalancer для облака. Направьте DNS APP_DOMAIN на
  адрес этого LoadBalancer. Публичный сертификат и ключ установите как TLS Secret
  istio-system/credit-tls (именно namespace gateway pod, не mlops).
  Создайте namespace mlops, Secret s3-credentials и registry pull Secret, если
  GHCR packages приватные. Передайте настройки окружения через GitHub Environment,
  затем выполните Deploy staging/production с ALLOW_INITIAL_DEPLOY=true.
  Gateway/DR/Services/NetworkPolicy/SA создаёт контроллер первой доставки.

  CI account нужны права в mlops на deployments/replicasets/pods/services/
  configmaps/secrets/networkpolicies/pdb/serviceaccounts и Istio Gateway,
  VirtualService, DestinationRule, а также pods/exec, pods/portforward;
  get namespace/создание и обновление mlops, чтение CRD и ограниченный read в
  istio-system (gateway Deployment, pod/service/portforward и TLS Secret).
  Права установки Istio, CRD и LoadBalancer оставьте отдельному bootstrap account.
  Не используйте cluster-admin для обычной доставки.

  Если уже есть rolling backend/frontend из этапа 3, bootstrap намеренно
  останавливается. Для данного проекта облачных приложений ещё нет; новые staging
  и production можно сразу создать по этой схеме. Миграцию работающего Traefik
  выполняйте отдельно через параллельный кластер и проверенный план переключения
  DNS/LB, с сохранением старого маршрута до подтверждения нового. Не запускайте
  оба инструмента доставки на одних Deployment и не удаляйте живые pod ради
  прохождения bootstrap-проверки.

Локальная демонстрация без облака
  Нужны Docker, kind v0.33.0, kubectl v1.35.0, istioctl v1.32.0, curl, openssl,
  Python окружение serving+model-storage (PyYAML входит в DVC dependencies).
  Установка Linux/amd64 инструментов:
    bash scripts/ci/install_tools.sh
    bash scripts/ci/install_istio.sh
    export PATH="$HOME/.local/bin:$PATH"
  Оба installer проверяют checksum. Docker/openssl и Python окружение устанавливаются
  отдельно. Cloud CI не переустанавливает control plane при каждом релизе.

  bash scripts/test_istio_local.sh

  Создаёт новый двухузловой kind, устанавливает Calico и реальный Istio,
  самоподписанный тестовый TLS certificate с проверкой CA и SNI, локальные DVC
  remote и два разных backend image с DVC metadata. Версии синтетической модели
  возвращают probability 0.5 и 0.75. Настоящая модель проекта не изменяется.
  Исходные Dockerfile и приложение используются без production fault switches.
  Тестовая ASGI-обёртка существует только как локальный ConfigMap и доступна
  только в context kind-mlops-strategies-*; GitHub cloud deploy её не подключает.

  Сценарии: bootstrap; Blue-Green; ошибка predict ПОСЛЕ переключения 100% и
  автоматический возврат; успешный Canary со всеми весами; ошибка predict на
  ступени 50% и возврат; неготовый candidate; успешный повтор и ручной rollback.
  В тесте интервал сокращён до 0.02с при тех же 60 выборках и порогах. Поэтому
  локальная демонстрация подтверждает механизм, но не длительный production soak.
  Скрипт использует отдельный kubeconfig и удаляет свой кластер. KEEP_CLUSTER=1
  оставит его для диагностики; значения context/workspace появятся в выводе.

  Отчёты: infrastructure/.evidence/stage4/result.json, отдельные JSON сценариев,
  pods/events/routes/analysis. В Git не попадают. GitHub job istio-strategies
  публикует их как artifact istio-delivery-evidence. Cloud deploy публикует
  release-evidence-* даже при отклонении candidate.

Диагностика
  kubectl --context YOUR_CONTEXT -n mlops get deployments,pods,services
  kubectl --context YOUR_CONTEXT -n mlops get virtualservice credit-scoring -o yaml
  kubectl --context YOUR_CONTEXT -n mlops get configmap mlops-release-state -o yaml
  kubectl --context YOUR_CONTEXT -n mlops logs deployment/backend-green --tail=100
  istioctl --context YOUR_CONTEXT proxy-status
  istioctl --context YOUR_CONTEXT analyze -n mlops

Официальные материалы
  https://istio.io/latest/docs/tasks/traffic-management/traffic-shifting/
  https://istio.io/latest/docs/tasks/traffic-management/ingress/secure-ingress/
  https://istio.io/latest/docs/reference/config/networking/virtual-service/
