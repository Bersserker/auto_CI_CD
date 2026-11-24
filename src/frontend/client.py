"""HTTP-клиент существующего API кредитного скоринга."""

from pydantic import ValidationError
import requests

from src.api.models import CreditRequest, CreditResponse


class BackendError(Exception):
    """Ошибка, которую можно показать пользователю интерфейса."""


def _request(method: str, base_url: str, path: str, **kwargs) -> requests.Response:
    try:
        response = requests.request(
            method, f"{base_url.rstrip('/')}{path}", timeout=(3, 30), **kwargs
        )
    except requests.Timeout as exc:
        raise BackendError(
            "Сервис не ответил вовремя. Повторите расчёт позже."
        ) from exc
    except requests.RequestException as exc:
        raise BackendError("Не удалось подключиться к сервису скоринга.") from exc
    if response.status_code == 503:
        raise BackendError("Модель пока недоступна. Повторите расчёт позже.")
    if response.status_code == 422:
        raise BackendError("Сервис отклонил данные. Проверьте значения полей.")
    if not response.ok:
        raise BackendError(f"Ошибка сервиса скоринга (HTTP {response.status_code}).")
    return response


def check_health(base_url: str) -> None:
    """Проверяет доступность API и загруженной модели."""
    _request("GET", base_url, "/health")


def predict(base_url: str, payload: dict) -> CreditResponse:
    """Валидирует запрос и ответ по общему с backend контракту."""
    request = CreditRequest.model_validate(payload)
    response = _request("POST", base_url, "/predict", json=request.model_dump())
    try:
        return CreditResponse.model_validate(response.json())
    except (ValueError, ValidationError) as exc:
        raise BackendError("Сервис вернул некорректный результат расчёта.") from exc
