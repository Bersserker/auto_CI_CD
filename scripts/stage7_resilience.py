"""Real Redis hit/fail-open, replica failure, simulated site failover and CPU scaling."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import threading
import time
from urllib.request import Request, urlopen

from scripts.stage7_autoscale import COMPOSE, ids

PAYLOAD = dict(limit_bal=100000, sex=1, education=2, marriage=1, age=35)
PAYLOAD.update(
    {name: 0 for name in ["pay_0", "pay_2", "pay_3", "pay_4", "pay_5", "pay_6"]}
)
PAYLOAD.update(
    {f"{prefix}{i}": 0 for prefix in ["bill_amt", "pay_amt"] for i in range(1, 7)}
)


def predict(change=0):
    payload = dict(PAYLOAD, limit_bal=100000 + change)
    request = Request(
        "http://localhost:28000/predict",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=10) as response:
        return (
            json.load(response),
            response.headers.get("X-Serving-Region"),
            response.headers.get("X-Serving-Replica"),
        )


def run(*args):
    subprocess.run([*COMPOSE, *args], check=True)


def wait(check, timeout=45):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if check():
                return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise TimeoutError("Condition did not recover")


def main():
    evidence = {}
    wait(lambda: predict()[1] == "site-a-local-simulation")
    reference = predict()[0]
    # Cache is disposable; clear only our dedicated demo DB.
    run("exec", "-T", "redis-a", "redis-cli", "FLUSHDB")
    for _ in range(20):
        assert predict()[0] == reference
    cache_hits = int(
        subprocess.check_output(
            [*COMPOSE, "exec", "-T", "redis-a", "redis-cli", "INFO", "stats"], text=True
        )
        .split("keyspace_hits:")[1]
        .split()[0]
    )
    assert cache_hits >= 19
    evidence["redis_hits"] = cache_hits
    first = ids()[0]
    try:
        subprocess.run(["docker", "stop", first], check=True)
        wait(lambda: predict()[2] not in (None, ""))
        for _ in range(10):
            assert predict()[0] == reference
        evidence["replica_failure"] = "passed"
    finally:
        subprocess.run(["docker", "start", first], check=True)
    try:
        run("stop", "redis-a")
        for _ in range(10):
            assert predict()[0] == reference
        evidence["redis_fail_open"] = "passed"
    finally:
        run("start", "redis-a")
    try:
        run("stop", "site-a")
        wait(lambda: predict()[1] == "site-b-local-simulation")
        for _ in range(10):
            assert predict()[0] == reference
        evidence["site_failover"] = "passed (local topology simulation)"
    finally:
        run("start", "site-a")
    wait(lambda: predict()[1] == "site-a-local-simulation")
    scaler = subprocess.Popen(
        [
            ".venv/bin/python",
            "-m",
            "scripts.stage7_autoscale",
            "--duration",
            "35",
            "--high",
            "5",
            "--low",
            "1",
        ],
        stdout=open("/tmp/stage7-autoscaler.log", "w"),
    )
    stop = threading.Event()
    counts = [0, 0]
    lock = threading.Lock()

    def load(worker):
        sequence = 0
        while not stop.is_set():
            try:
                predict(worker * 100000 + sequence)
                with lock:
                    counts[0] += 1
            except OSError:
                with lock:
                    counts[1] += 1
            sequence += 1

    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            tasks = [pool.submit(load, i) for i in range(20)]
            time.sleep(30)
            stop.set()
            for future in tasks:
                future.result()
        assert scaler.wait(timeout=30) == 0
        history = json.loads(Path("artifacts/stage7/autoscaling.json").read_text())
        assert any(
            (step["scaled_to"] or 0) > 2 for step in history
        ), "CPU did not cross scale-up threshold"
        evidence["scale_up"] = True
        evidence["load_success"] = counts[0]
        evidence["load_errors"] = counts[1]
        Path("artifacts/stage7/resilience.json").write_text(
            json.dumps(evidence, indent=2) + "\n"
        )
        assert counts[1] / max(1, sum(counts)) < 0.01
    finally:
        stop.set()
        if scaler.poll() is None:
            scaler.terminate()
            scaler.wait(timeout=10)
        run("up", "-d", "--no-deps", "--no-recreate", "--scale", "site-a=2", "site-a")
    Path("artifacts/stage7/resilience.json").write_text(
        json.dumps(evidence, indent=2) + "\n"
    )
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
