"""Accept release artifacts only from trusted CI runs in this repository."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def validate_release(release, run, repository):
    if release.get("sha") != run["head_sha"] or str(release.get("run_id")) != str(
        run["id"]
    ):
        raise ValueError("Release metadata does not match the CI run")
    for component in ["backend", "frontend"]:
        pattern = rf"ghcr\.io/{re.escape(repository.lower())}/credit-{component}@sha256:[0-9a-f]{{64}}"
        if not re.fullmatch(pattern, release.get(component, "")):
            raise ValueError(f"Invalid {component} image digest/repository")


def main():
    run_id = os.environ["RELEASE_RUN_ID"]
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise ValueError("run_id must be a positive integer")
    repository = os.environ["GH_REPO"]
    run = json.loads(
        subprocess.check_output(
            ["gh", "api", f"repos/{repository}/actions/runs/{run_id}"], text=True
        )
    )
    same_run = run_id == os.environ["GITHUB_RUN_ID"]
    if (
        run["path"] != ".github/workflows/ci-cd.yml"
        or run["event"] not in {"push", "workflow_dispatch"}
        or run["head_branch"] != os.environ["DEFAULT_BRANCH"]
        or run["head_repository"]["full_name"].lower() != repository.lower()
        or (not same_run and run["conclusion"] != "success")
    ):
        raise ValueError("Use a successful CI run from the default branch")
    # The current CI run is still in progress while its deploy jobs execute.
    with tempfile.TemporaryDirectory() as directory:
        subprocess.run(
            ["gh", "run", "download", run_id, "--name", "release", "--dir", directory],
            check=True,
        )
        release = json.loads((Path(directory) / "release.json").read_text())
    validate_release(release, run, repository)
    Path(os.environ["RELEASE_FILE"]).write_text(json.dumps(release, indent=2) + "\n")
    # Manifests must come from the same tested commit as the images.
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
        stream.write(f"sha={run['head_sha']}\n")


if __name__ == "__main__":
    main()
