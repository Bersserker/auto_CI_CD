"""Write a dedicated CI kubeconfig without printing credentials."""

import base64
import os
from pathlib import Path
import subprocess

import yaml


def main():
    if os.environ["RELEASE_ENVIRONMENT"] not in {"staging", "production"}:
        raise ValueError("Unknown environment")
    if os.environ["RELEASE_MODE"] not in {"blue-green", "rollback", "canary"}:
        raise ValueError("Unknown operation")
    encoded = os.environ.get("KUBECONFIG_B64", "")
    if not encoded:
        raise ValueError("Configure the environment secret KUBECONFIG_B64 first")
    try:
        data = base64.b64decode(encoded, validate=True)
        config = yaml.safe_load(data)
        if (
            not isinstance(config, dict)
            or not config.get("current-context")
            or not config.get("users")
            or not config.get("clusters")
        ):
            raise ValueError
        for user in config.get("users", []):
            auth = user["user"]
            if any(
                key in auth
                for key in [
                    "exec",
                    "auth-provider",
                    "client-certificate",
                    "client-key",
                    "tokenFile",
                ]
            ):
                raise ValueError
        for cluster in config.get("clusters", []):
            settings = cluster["cluster"]
            if (
                not settings.get("server", "").startswith("https://")
                or settings.get("insecure-skip-tls-verify")
                or "certificate-authority" in settings
            ):
                raise ValueError
    except Exception:
        raise ValueError(
            "Use a self-contained HTTPS kubeconfig with token/cert auth"
        ) from None
    path = Path(os.environ["KUBECONFIG"])
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    subprocess.run(["kubectl", "cluster-info"], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
