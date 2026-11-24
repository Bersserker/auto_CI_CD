"""Проверки контракта и ошибок HTTP-клиента frontend."""

import json
from unittest.mock import Mock

import pytest
import requests

from src.frontend.client import BackendError, check_health, predict


def response(status, payload):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    return result


@pytest.fixture
def credit_payload():
    result = {"limit_bal": 20000, "sex": 2, "education": 2, "marriage": 1, "age": 30}
    result.update(
        {k: 0 for k in ["pay_0", "pay_2", "pay_3", "pay_4", "pay_5", "pay_6"]}
    )
    result.update({f"bill_amt{i}": -100 if i == 1 else 0 for i in range(1, 7)})
    result.update({f"pay_amt{i}": 0 for i in range(1, 7)})
    return result


def test_predict_uses_existing_api_contract(monkeypatch, credit_payload):
    request = Mock(
        return_value=response(200, {"prediction": 1, "default_probability": 0.73})
    )
    monkeypatch.setattr(requests, "request", request)
    result = predict("http://backend:8000/", credit_payload)
    assert result.prediction == 1
    assert result.default_probability == 0.73
    assert request.call_args.args == ("POST", "http://backend:8000/predict")
    assert request.call_args.kwargs["json"] == credit_payload


@pytest.mark.parametrize("status", [422, 503, 500])
def test_http_errors_are_reported(monkeypatch, credit_payload, status):
    monkeypatch.setattr(requests, "request", Mock(return_value=response(status, {})))
    with pytest.raises(BackendError):
        predict("http://backend:8000", credit_payload)


@pytest.mark.parametrize("error", [requests.Timeout(), requests.ConnectionError()])
def test_connection_failures_are_reported(monkeypatch, error):
    monkeypatch.setattr(requests, "request", Mock(side_effect=error))
    with pytest.raises(BackendError):
        check_health("http://backend:8000")


@pytest.mark.parametrize(
    "payload", [{"success": True}, {"prediction": 0, "default_probability": 2}]
)
def test_invalid_prediction_response(monkeypatch, credit_payload, payload):
    monkeypatch.setattr(requests, "request", Mock(return_value=response(200, payload)))
    with pytest.raises(BackendError, match="некорректный результат"):
        predict("http://backend:8000", credit_payload)


def test_non_json_response(monkeypatch, credit_payload):
    result = response(200, None)
    result._content = b"<html>proxy error</html>"
    monkeypatch.setattr(requests, "request", Mock(return_value=result))
    with pytest.raises(BackendError, match="некорректный результат"):
        predict("http://backend:8000", credit_payload)
