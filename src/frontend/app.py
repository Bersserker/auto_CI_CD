"""Streamlit-интерфейс PD-модели на признаках UCI Credit Card."""

import os

from pydantic import ValidationError
import streamlit as st

from src.frontend.client import BackendError, check_health, predict

SEX = {1: "Мужской", 2: "Женский"}
EDUCATION = {
    0: "Не указано (0)",
    1: "Послевузовское образование (1)",
    2: "Университет (2)",
    3: "Средняя школа (3)",
    4: "Другое (4)",
    5: "Неизвестно (5)",
    6: "Неизвестно (6)",
}
MARRIAGE = {0: "Не указано (0)", 1: "В браке (1)", 2: "Не в браке (2)", 3: "Другое (3)"}
PAY_FIELDS = ("pay_0", "pay_2", "pay_3", "pay_4", "pay_5", "pay_6")


def client_form() -> tuple[dict, bool]:
    """Собирает ровно 23 признака CreditRequest без подмены кодов категорий."""
    with st.form("credit_request"):
        st.subheader("Данные клиента")
        left, right = st.columns(2)
        with left:
            limit = st.number_input(
                "Кредитный лимит",
                min_value=0.0,
                value=20000.0,
                step=1000.0,
                key="limit_bal",
            )
            age = st.number_input("Возраст", min_value=0, value=30, key="age")
            sex = st.selectbox("Пол", list(SEX), format_func=SEX.get, key="sex")
        with right:
            education = st.selectbox(
                "Образование",
                list(EDUCATION),
                index=2,
                format_func=EDUCATION.get,
                key="education",
            )
            marriage = st.selectbox(
                "Семейное положение",
                list(MARRIAGE),
                index=2,
                format_func=MARRIAGE.get,
                key="marriage",
            )
        payload = {
            "limit_bal": limit,
            "age": age,
            "sex": sex,
            "education": education,
            "marriage": marriage,
        }
        st.subheader("История за шесть месяцев")
        st.caption(
            "Месяц 1 — последний наблюдаемый, месяц 6 — самый ранний. "
            "Суммы указываются в денежных единицах исходных данных. "
            "Отрицательная сумма счёта допустима; платёж должен быть неотрицательным."
        )
        st.caption(
            "Статус погашения — код из исходных данных: "
            "−1 означает оплату вовремя, положительное число — месяцы задержки. "
            "Служебные коды −3, −2 и 0 сохраняются без преобразования."
        )
        for month, field in enumerate(PAY_FIELDS, start=1):
            st.markdown(f"**Месяц {month}**")
            status_col, bill_col, payment_col = st.columns(3)
            with status_col:
                payload[field] = st.number_input(
                    "Статус погашения",
                    min_value=-3,
                    value=0,
                    step=1,
                    key=field,
                )
            with bill_col:
                payload[f"bill_amt{month}"] = st.number_input(
                    "Сумма счёта",
                    value=0.0,
                    step=100.0,
                    key=f"bill_amt{month}",
                )
            with payment_col:
                payload[f"pay_amt{month}"] = st.number_input(
                    "Внесённый платёж",
                    min_value=0.0,
                    value=0.0,
                    step=100.0,
                    key=f"pay_amt{month}",
                )
        submitted = st.form_submit_button(
            "Рассчитать вероятность дефолта", type="primary"
        )
    return payload, submitted


def main() -> None:
    st.set_page_config(page_title="Кредитный скоринг", page_icon="🏦", layout="wide")
    st.title("Кредитный скоринг")
    st.write(
        "Оценка вероятности дефолта клиента в следующем месяце по истории платежей."
    )
    api_url = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")
    with st.sidebar:
        st.subheader("Состояние сервиса")
        try:
            check_health(api_url)
        except BackendError as exc:
            st.warning(str(exc))
        else:
            st.success("Сервис готов к расчёту")
        st.button("Обновить статус")
    payload, submitted = client_form()
    if submitted:
        # При ошибке нового расчёта предыдущий результат не показывается.
        st.session_state.pop("prediction_result", None)
        try:
            with st.spinner("Выполняется расчёт…"):
                result = predict(api_url, payload)
        except ValidationError:
            st.error("Проверьте данные: значения не соответствуют формату модели.")
        except BackendError as exc:
            st.error(str(exc))
        else:
            st.session_state["prediction_result"] = result.model_dump()
    if "prediction_result" in st.session_state:
        result = st.session_state["prediction_result"]
        st.subheader("Результат последнего расчёта")
        st.metric("Вероятность дефолта", f"{result['default_probability']:.1%}")
        if result["prediction"] == 1:
            st.warning("Прогноз модели: дефолт (класс 1).")
        else:
            st.success("Прогноз модели: отсутствие дефолта (класс 0).")
        st.caption(
            "Результат отражает прогноз модели. Решение об одобрении кредита API не принимает."
        )


if __name__ == "__main__":
    main()
