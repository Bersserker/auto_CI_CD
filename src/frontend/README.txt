Frontend кредитного скоринга

Интерфейс Streamlit отправляет 23 поля CreditRequest на POST /predict и
показывает prediction и default_probability. Проверка готовности: GET /health.
API_BASE_URL — адрес backend без /api/v1 (по умолчанию http://localhost:8000).
Значение задается на сервере frontend, а не в браузере пользователя.
При недоступной модели форма остается доступной; расчет показывает ошибку.

Из корня репозитория, в отдельном окружении:
uv venv /tmp/mlops-frontend --python 3.13
uv pip install --python /tmp/mlops-frontend/bin/python -r src/frontend/requirements.txt
/tmp/mlops-frontend/bin/python -m streamlit run src/frontend/app.py

Backend запускается отдельно на порту 8000 с файлом models/best_model.joblib.
Модель frontend не читает и не включает в свой образ.

Docker (контекст — корень репозитория):
docker build -f src/frontend/Dockerfile -t credit-frontend:local .
docker compose up --build
Открыть http://localhost:8501. Compose задает API_BASE_URL=http://app:8000.
В Kubernetes задайте адрес Service backend через переменную окружения;
для подготовленных NetworkPolicy используйте namespace mlops, app=frontend
и app=backend, порт frontend 8501, backend 8000.

Проверки:
uv pip install --python /tmp/mlops-frontend/bin/python pytest
/tmp/mlops-frontend/bin/python -m pytest tests/test_frontend.py tests/test_frontend_ui.py -v
При отсутствии Streamlit UI-тесты пропускаются; HTTP-клиент тестируется отдельно.
Тесты используют подставные HTTP-ответы, реальный ML-артефакт для них не нужен.
Для полной интеграционной проверки нужен запущенный backend с моделью.
