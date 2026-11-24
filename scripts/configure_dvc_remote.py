"""Настройка DVC remote из окружения; учетные данные остаются вне конфигурации."""

import argparse
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit


def configure_remote(url: str, endpoint: str, region: str) -> None:
    if not url:
        raise ValueError("Задайте DVC_REMOTE_URL: s3://<бакет-артефактов>/dvc")
    if url.startswith("s3://"):
        parsed = urlsplit(url)
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", parsed.netloc)
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Некорректный DVC_REMOTE_URL: требуется S3-бакет без ключей в URL."
            )
        if urlsplit(endpoint).scheme != "https":
            raise ValueError("Для облачного S3 remote требуется HTTPS endpoint.")
    elif not Path(url).is_absolute():
        raise ValueError("Remote должен быть s3://... или абсолютным локальным путем.")
    # --local записывает адрес выбранного окружения в .dvc/config.local.
    subprocess.run(
        ["dvc", "remote", "add", "--local", "--force", "--default", "artifacts", url],
        check=True,
    )
    if url.startswith("s3://"):
        for option, value in [("endpointurl", endpoint), ("region", region)]:
            subprocess.run(
                ["dvc", "remote", "modify", "--local", "artifacts", option, value],
                check=True,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.getenv("DVC_REMOTE_URL", ""))
    parser.add_argument(
        "--endpoint",
        default=os.getenv("DVC_S3_ENDPOINT_URL", "https://storage.yandexcloud.net"),
    )
    parser.add_argument(
        "--region", default=os.getenv("AWS_DEFAULT_REGION", "ru-central1")
    )
    args = parser.parse_args()
    try:
        configure_remote(args.url, args.endpoint, args.region)
    except (ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"DVC remote не настроен: {exc}\n")


if __name__ == "__main__":
    main()
