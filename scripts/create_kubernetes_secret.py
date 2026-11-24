"""Передать S3-ключи из окружения в Kubernetes Secret без аргументов с секретами."""

import argparse
import base64
import json
import os
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", default="mlops")
    args = parser.parse_args()
    keys = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]
    if any(not os.getenv(key) for key in keys):
        parser.error("Задайте AWS_ACCESS_KEY_ID и AWS_SECRET_ACCESS_KEY в окружении.")
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": "s3-credentials", "namespace": args.namespace},
        "data": {
            key: base64.b64encode(os.environ[key].encode()).decode() for key in keys
        },
    }
    # Server-side apply не сохраняет копию секрета в last-applied annotation.
    result = subprocess.run(
        [
            "kubectl",
            "--context",
            args.context,
            "apply",
            "--server-side",
            "--field-manager=mlops-secrets",
            "-f",
            "-",
        ],
        input=json.dumps(secret),
        text=True,
        capture_output=True,
    )
    if result.returncode:
        # Не выводим тело запроса/ответ сервера: там могут быть ключи.
        parser.exit(
            1, "Не удалось применить Secret. Проверьте context, namespace и права.\n"
        )
    print(f"Secret s3-credentials обновлен в namespace {args.namespace}.")


if __name__ == "__main__":
    main()
