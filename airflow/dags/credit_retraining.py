"""Weekly retraining plus leased event triggers; production promotion is separate."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import time
from urllib.request import Request, urlopen

from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.sdk import dag, get_current_context, task


def api(path, data=None):
    request = Request(
        os.getenv("TRAINER_URL", "http://trainer:8000") + path,
        data=None if data is None else json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def perform(stage, plan):
    job = api("/jobs", dict(plan, stage=stage))["job_id"]
    deadline = time.monotonic() + 1100
    while time.monotonic() < deadline:
        status = api("/jobs/" + job)
        if status["state"] == "failed":
            raise RuntimeError(status["error"])
        if status["state"] == "success":
            if stage == "validate" and not status["result"]["passed"]:
                raise RuntimeError("Candidate rejected by release gate")
            return plan
        time.sleep(2)
    raise TimeoutError("Training worker timeout")


@dag(
    dag_id="credit_retraining",
    schedule="0 2 * * 1",
    start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
    default_args={
        "retries": 1,
        "retry_delay": timedelta(seconds=15),
        "execution_timeout": timedelta(minutes=20),
    },
)
def credit_retraining():
    @task
    def prepare():
        context = get_current_context()
        conf = dict(context["dag_run"].conf or {})
        allowed = {"demo", "trigger", "batch", "event_id", "as_of", "run_id"}
        if set(conf) - allowed:
            raise ValueError("Unsupported training parameters")
        conf.setdefault(
            "run_id",
            "dag-" + hashlib.sha256(context["run_id"].encode()).hexdigest()[:24],
        )
        return perform("prepare", conf)

    @task
    def train(plan):
        return perform("train", plan)

    @task
    def validate(plan):
        return perform("validate", plan)

    @task
    def publish(plan):
        return perform("publish", plan)

    publish(validate(train(prepare())))


@dag(
    dag_id="credit_retraining_events",
    schedule="*/5 * * * *",
    start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
)
def credit_retraining_events():
    @task
    def detect():
        return api("/events", {})

    TriggerDagRunOperator.partial(
        task_id="trigger_candidate",
        trigger_dag_id="credit_retraining",
        wait_for_completion=False,
    ).expand(conf=detect())


credit_retraining()
credit_retraining_events()
