"""CPU-based local Compose scaler with hysteresis; cloud scaling uses HPA/YC."""

import argparse
import json
from pathlib import Path
import subprocess
import time

COMPOSE = [
    "docker",
    "compose",
    "-p",
    "scoring-stage7",
    "-f",
    "docker-compose.stage7.yml",
]


def ids():
    return subprocess.check_output([*COMPOSE, "ps", "-q", "site-a"], text=True).split()


def decide(replicas, high_streak, low_streak, minimum, maximum):
    if high_streak >= 2:
        return min(maximum, replicas + 1)
    if low_streak >= 4:
        return max(minimum, replicas - 1)
    return replicas


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--high", type=float, default=50)
    parser.add_argument("--low", type=float, default=10)
    args = parser.parse_args()
    if not 0 <= args.low < args.high or args.interval <= 0 or args.duration <= 0:
        parser.error("Invalid thresholds/duration")
    history = []
    high = low = 0
    last_change = 0
    end = time.monotonic() + args.duration
    while time.monotonic() < end:
        containers = ids()
        if not containers:
            raise RuntimeError("site-a is not running")
        stats = subprocess.check_output(
            ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", *containers],
            text=True,
        )
        cpu = sum(float(p.rstrip("%")) for p in stats.split()) / len(containers)
        high = high + 1 if cpu > args.high else 0
        low = low + 1 if cpu < args.low else 0
        target = decide(len(containers), high, low, 2, 4)
        action = None
        if target != len(containers) and time.monotonic() - last_change >= 8:
            subprocess.run(
                [
                    *COMPOSE,
                    "up",
                    "-d",
                    "--no-deps",
                    "--no-recreate",
                    "--scale",
                    f"site-a={target}",
                    "site-a",
                ],
                check=True,
            )
            last_change = time.monotonic()
            high = low = 0
            action = target
        history.append(
            {
                "timestamp": time.time(),
                "replicas": len(containers),
                "cpu_percent": cpu,
                "scaled_to": action,
            }
        )
        Path("artifacts/stage7/autoscaling.json").write_text(
            json.dumps(history, indent=2) + "\n"
        )
        time.sleep(args.interval)
    print(json.dumps(history, indent=2))


if __name__ == "__main__":
    main()
