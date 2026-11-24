"""Headless UI-проверки. Требуют src/frontend/requirements.txt."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from src.api.models import CreditRequest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
APP = Path(__file__).resolve().parents[1] / "src/frontend/app.py"


def reply(status, payload):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    return result


def submit(app):
    next(
        button
        for button in app.button
        if button.label == "Рассчитать вероятность дефолта"
    ).click().run()


def test_form_sends_all_23_api_fields_and_shows_pd(monkeypatch):
    def respond(method, url, **kwargs):
        if method == "GET":
            return reply(200, {"status": "ok"})
        return reply(200, {"prediction": 1, "default_probability": 0.73})

    request = Mock(side_effect=respond)
    monkeypatch.setattr(requests, "request", request)
    monkeypatch.setenv("API_BASE_URL", "http://backend:8000/")
    app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    assert all(call.args[0] == "GET" for call in request.call_args_list)
    app.number_input(key="bill_amt1").set_value(-100.0)
    app.number_input(key="pay_0").set_value(-3)
    app.selectbox(key="education").set_value(0)
    submit(app)
    assert not app.exception
    post = next(call for call in request.call_args_list if call.args[0] == "POST")
    assert post.args[1] == "http://backend:8000/predict"
    payload = post.kwargs["json"]
    assert set(payload) == set(CreditRequest.model_fields)
    assert len(payload) == 23
    assert payload["bill_amt1"] == -100
    assert payload["pay_0"] == -3
    assert payload["education"] == 0
    CreditRequest.model_validate(payload)
    assert app.metric[0].value == "73.0%"
    assert any("класс 1" in warning.value for warning in app.warning)


def test_offline_service_keeps_form_available(monkeypatch):
    monkeypatch.setattr(
        requests, "request", Mock(side_effect=requests.ConnectionError())
    )
    app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    assert app.number_input(key="limit_bal")
    submit(app)
    assert not app.exception
    assert any("подключиться" in error.value for error in app.error)
    assert not app.metric


def test_failed_second_prediction_clears_previous_result(monkeypatch):
    def respond(method, url, **kwargs):
        return (
            reply(200, {"status": "ok"})
            if method == "GET"
            else reply(200, {"prediction": 0, "default_probability": 0.12})
        )

    monkeypatch.setattr(requests, "request", Mock(side_effect=respond))
    app = AppTest.from_file(str(APP)).run()
    submit(app)
    assert app.metric[0].value == "12.0%"
    monkeypatch.setattr(requests, "request", Mock(return_value=reply(503, {})))
    submit(app)
    assert not app.exception
    assert not app.metric
    assert any("Модель пока недоступна" in error.value for error in app.error)
