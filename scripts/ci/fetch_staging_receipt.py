"""Require a matching release receipt from a trusted staging delivery run."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def main():
    run_id = os.environ["STAGING_RUN_ID"]
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise ValueError("Invalid staging run ID")
    repository = os.environ["GH_REPO"]
    run = json.loads(
        subprocess.check_output(
            ["gh", "api", f"repos/{repository}/actions/runs/{run_id}"], text=True
        )
    )
    if (
        run["path"]
        not in {
            ".github/workflows/ci-cd.yml",
            ".github/workflows/deploy-staging.yml",
            ".github/workflows/canary-release.yml",
            ".github/workflows/blue-green-release.yml",
        }
        or run["event"] not in {"push", "workflow_dispatch"}
        or run["head_branch"] != os.environ["DEFAULT_BRANCH"]
        or run["head_repository"]["full_name"].lower() != repository.lower()
        or (run_id != os.environ["GITHUB_RUN_ID"] and run["conclusion"] != "success")
    ):
        raise ValueError(
            "Use a successful staging delivery run from the default branch"
        )
    with tempfile.TemporaryDirectory() as directory:
        subprocess.run(
            [
                "gh",
                "run",
                "download",
                run_id,
                "--name",
                f"deployed-staging-{os.environ['RELEASE_RUN_ID']}",
                "--dir",
                directory,
            ],
            check=True,
        )
        receipt = json.loads((Path(directory) / "receipt.json").read_text())
    if receipt != json.loads(Path(os.environ["RELEASE_FILE"]).read_text()):
        raise ValueError("Staging release does not match the production candidate")


if __name__ == "__main__":
    main()
