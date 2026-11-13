#!/usr/bin/env python3
# Public one-off cleanup CLI: --installation PATH previews; --delete performs irreversible removal.
# Run from the new package outside the selected old installation, with Docker access.
# This is neither a normal deployment nor a backup tool; see deploy/README.md.

"""Remove one explicitly selected prototype before a clean Lem installation.

Run this from the NEW package, outside the old installation. No name of an older
product is embedded: Compose's resolved metadata supplies project/volume names.
The default is a plan; --delete performs the requested irreversible prototype reset.
SSH accounts, host directories outside the installation and DNS are not modified.
"""

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path


def plan(installation, run):
    source = Path(installation).expanduser()
    if source.is_symlink():
        raise ValueError("Use the actual installation directory, not a symbolic link")
    try:
        target = source.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError("Installation directory does not exist or cannot be resolved") from error
    if target in (
        Path("/"),
        Path.home(),
        Path("/opt"),
        Path("/home"),
        Path("/srv"),
        Path("/usr"),
        Path("/var"),
    ):
        raise ValueError("Refusing a host/system/home root")
    if target in Path(__file__).resolve().parents:
        raise ValueError(
            "Run this script from the new package, outside the installation being removed"
        )
    if not all(
        (target / p).is_file()
        for p in ("deploy/compose.yml", "deploy/.env", "server/run.py", "shared/checklist-v1.json")
    ):
        raise ValueError("Directory is not a complete prototype installation")
    compose = [
        "sudo",
        "docker",
        "compose",
        "--project-directory",
        str(target / "deploy"),
        "-f",
        str(target / "deploy/compose.yml"),
        "--env-file",
        str(target / "deploy/.env"),
    ]
    config = json.loads(run(compose + ["config", "--format", "json"]))
    if set(config.get("services", {})) != {"api", "db", "proxy"} or set(
        config.get("volumes", {})
    ) != {"pgdata", "caddydata", "caddyconfig"}:
        raise ValueError("Unexpected services/volumes: review this installation manually")
    project = config.get("name", "")
    volumes = [v.get("name", "") for v in config["volumes"].values()]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project) or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", v) for v in volumes
    ):
        raise ValueError("Invalid Compose project or volume name")
    return target, compose, project, volumes


def execute(installation, delete=False):
    def run(command):
        return subprocess.run(command, check=True, stdout=subprocess.PIPE, text=True).stdout

    target, compose, project, volumes = plan(installation, run)
    print("Prototype directory:", target)
    print("Compose project:", project)
    print("Database and HTTPS volumes:", ", ".join(volumes))
    if not delete:
        print("Plan only. Add --delete to remove this prototype and its data/certificates.")
        return
    # Docker refuses to remove any volume still referenced by another container.
    # If any step fails the directory/configuration remains available for recovery.
    run(compose + ["down", "--volumes", "--remove-orphans", "--rmi", "local"])
    existing = set(run(["sudo", "docker", "volume", "ls", "--format", "{{.Name}}"]).splitlines())
    for volume in volumes:
        if volume in existing:
            run(["sudo", "docker", "volume", "rm", volume])
    shutil.rmtree(target)
    print("Prototype removed. Install the new Lem package in ~/apps/Lem.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", required=True, type=Path)
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()
    try:
        execute(args.installation, args.delete)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit("Prototype removal stopped: " + str(error))
