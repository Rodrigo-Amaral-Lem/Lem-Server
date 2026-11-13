#!/usr/bin/env python3
# Linux host removal implementation; use uninstall-server.sh, and read deploy/UNINSTALL.md.
# Default preview; --stop retains files/volumes, --purge removes owned resources after confirmation/backup.
# --skip-backup only explicitly waives a fresh purge backup; retained configuration remains sensitive.

"""Stop or uninstall exactly one Lem installation. Default: preview only."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid


def run(command):
    return subprocess.run(command, check=True, stdout=subprocess.PIPE, text=True).stdout


def plan(installation):
    source = Path(installation).expanduser()
    if source.is_symlink():
        raise ValueError("Use the real installation path, not a symbolic link")
    target = source.resolve(strict=True)
    forbidden = {Path(p) for p in ("/", "/home", "/opt", "/srv", "/usr", "/var", "/etc", "/tmp", "/root")}
    if target in forbidden or target == Path.home() or target in Path.home().parents:
        raise ValueError("Refusing a system or home directory")
    markers = ("deploy/.env", "deploy/compose.yml", "server/run.py", "shared/checklist-v1.json")
    incomplete = not all((target / name).is_file() for name in markers)
    if incomplete:
        return incomplete_plan(target)
    for name in markers:
        path = target / name
        if not path.is_file() or path.is_symlink() or target not in path.resolve().parents:
            raise ValueError("Not a complete installation: " + name)
    docker = ["docker"] if os.geteuid() == 0 else ["sudo", "docker"]
    compose = docker + ["compose", "--project-directory", str(target / "deploy"),
                        "--env-file", str(target / "deploy/.env"), "-f", str(target / "deploy/compose.yml")]
    config = json.loads(run(compose + ["config", "--format", "json"]))
    if config.get("name") != "lem" or set(config.get("services", {})) != {"api", "db", "proxy"}:
        raise ValueError("Expected only the Lem api/db/proxy services")
    if set(config.get("volumes", {})) != {"pgdata", "caddydata", "caddyconfig"}:
        raise ValueError("Unexpected volumes; manual review required")
    volumes = {key: value.get("name", "") for key, value in config["volumes"].items()}
    if len(set(volumes.values())) != 3 or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) for name in volumes.values()):
        raise ValueError("Invalid or duplicate volume names")
    ids = run(docker + ["ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=lem"]).split()
    containers = json.loads(run(docker + ["inspect", *ids])) if ids else []
    for container in containers:
        labels = container["Config"].get("Labels") or {}
        files = labels.get("com.docker.compose.project.config_files", "").split(",")
        if len(files) != 1 or Path(files[0]).resolve() != target / "deploy/compose.yml":
            raise ValueError("Running project lem belongs to another installation: " + ",".join(files))
        service = labels.get("com.docker.compose.service")
        if service not in {"api", "db", "proxy"}:
            raise ValueError("Unexpected container in project lem")
        expected = {"db": {"pgdata"}, "proxy": {"caddydata", "caddyconfig"}, "api": set()}[service]
        mounted = {m["Name"] for m in container.get("Mounts", []) if m["Type"] == "volume"}
        if mounted != {volumes[k] for k in expected}:
            raise ValueError("Container volumes do not match the selected configuration")
    existing = set(run(docker + ["volume", "ls", "--format", "{{.Name}}"]).splitlines())
    for volume in volumes.values():
        if volume in existing:
            users = set(run(docker + ["ps", "-aq", "--no-trunc", "--filter", "volume=" + volume]).split())
            if users - set(ids):
                raise ValueError("Volume is used by another installation: " + volume)
    return target, docker, compose, volumes, existing


def incomplete_plan(target):
    """Recover ownership from containers, never guess a volume from its name."""
    deploy = target / "deploy"
    if not deploy.is_dir() or deploy.is_symlink() or not any(
        (target / name).is_file() for name in ("deploy/compose.yml", "server/run.py", "deploy/uninstall-server.py")
    ):
        raise ValueError("No recognizable Lem installation files in the selected directory")
    docker = ["docker"] if os.geteuid() == 0 else ["sudo", "docker"]
    ids = run(docker + ["ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=lem"]).split()
    containers = json.loads(run(docker + ["inspect", *ids])) if ids else []
    matched, volumes, database, skipped = [], {}, None, []
    destinations = {("db", "/var/lib/postgresql/data"): "pgdata",
                    ("proxy", "/data"): "caddydata", ("proxy", "/config"): "caddyconfig"}
    for container in containers:
        labels = container["Config"].get("Labels") or {}
        files = labels.get("com.docker.compose.project.config_files", "").split(",")
        if len(files) != 1 or Path(files[0]).resolve() != target / "deploy/compose.yml":
            skipped.append(container["Id"])
            continue
        service = labels.get("com.docker.compose.service")
        if service not in {"api", "db", "proxy"}:
            raise ValueError("Unexpected container service in selected installation")
        matched.append(container["Id"])
        if service == "db":
            if database is not None:
                raise ValueError("Multiple database containers; review manually")
            database = container["Id"]
        for mount in container.get("Mounts", []):
            if mount["Type"] != "volume":
                continue
            key = destinations.get((service, mount["Destination"]))
            if key is None:
                print("Keeping unrecognized volume:", mount["Name"])
                continue
            value = mount["Name"]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
                raise ValueError("Invalid volume name")
            if key in volumes and volumes[key] != value:
                raise ValueError("Conflicting volume ownership")
            volumes[key] = value
    existing = set(run(docker + ["volume", "ls", "--format", "{{.Name}}"]).splitlines())
    for key, volume in list(volumes.items()):
        users = set(run(docker + ["ps", "-aq", "--no-trunc", "--filter", "volume=" + volume]).split())
        if users - set(matched):
            print("Keeping shared volume:", volume)
            del volumes[key]
    if len(set(volumes.values())) != len(volumes):
        raise ValueError("Duplicate volume roles; review manually")
    print("Incomplete installation: using container ownership metadata; unproven volumes are retained.")
    if skipped:
        print("Keeping", len(skipped), "container(s) belonging to another installation.")
    if not matched:
        print("No matching containers found. Only selected installation files can be removed.")
    # A recovery descriptor replaces Compose commands, which require the missing .env.
    return target, docker, {"containers": matched, "database": database}, volumes, existing


def execute(installation, action=None, skip_backup=False):
    target, docker, compose, volumes, existing = plan(installation)
    print("Installation:", target)
    print("Project: lem; services: api, db, proxy")
    print("Database/HTTPS volumes:", ", ".join(volumes.values()))
    if action is None:
        print("Preview only. Use --stop to retain files/data, or --purge for complete removal.")
        return
    with (target / "deploy/.deploy.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another deploy/uninstall is running")
        # Validate again under the same lock used by deployment.
        plan(installation)
        if action == "stop":
            prompt = "Stop and remove Lem containers; retain files, database and certificates. Type PARAR: "
            word = "PARAR"
        else:
            print("PURGE deletes installation files, database/photos/videos and HTTPS volumes.")
            print("A recovery directory will retain configuration and existing backups outside the installation.")
            if skip_backup:
                print("WARNING: no new database backup will be made.")
            prompt, word = "Type APAGAR to confirm complete removal: ", "APAGAR"
        if input(prompt) != word:
            raise ValueError("Confirmation did not match; nothing changed")
        if action == "purge" and input("Type the full installation path to confirm: ") != str(target):
            raise ValueError("Path did not match; nothing changed")
        # Catch changed resources while confirmation was open.
        checked = plan(installation)
        if checked[2] != compose or checked[3] != volumes or checked[4].intersection(volumes.values()) != existing.intersection(volumes.values()):
            raise ValueError("Volumes changed during confirmation; retry")
        recovery = None
        if action == "purge":
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            recovery = target.parent / (target.name + "-uninstall-backup-" + stamp + "-" + uuid.uuid4().hex[:8])
            recovery.mkdir(mode=0o700)
            print("Recovery directory:", recovery, flush=True)
            for name in (".env", "compose.yml", "Caddyfile"):
                path = target / "deploy" / name
                if path.is_file() and not path.is_symlink() and target in path.resolve().parents:
                    shutil.copyfile(path, recovery / name)
                    (recovery / name).chmod(0o600)
            if not skip_backup and volumes.get("pgdata") in existing:
                print("Backing up database (including evidence); this can take time...", flush=True)
                partial = recovery / "database.dump.partial"
                with partial.open("xb") as output:
                    backup_command = (docker + ["exec", compose["database"]] if isinstance(compose, dict)
                                      else compose + ["exec", "-T", "db"])
                    subprocess.run(backup_command + ["pg_dump", "-U", "lem", "-d", "lem", "-Fc"],
                                   check=True, stdout=output)
                partial.rename(recovery / "database.dump")
                print("Database backup completed.", flush=True)
        # Never use down --volumes, prune, or remove other projects/images/packages.
        if isinstance(compose, dict):
            if compose["containers"]:
                subprocess.run(docker + ["stop", *compose["containers"]], check=True)
                subprocess.run(docker + ["rm", *compose["containers"]], check=True)
            print("Recovery mode retains Docker networks and any unproven volumes.")
        else:
            subprocess.run(compose + ["down"], check=True)
        if action == "stop":
            print("Lem stopped. Installation files, database and certificates retained.")
            return
        for volume in volumes.values():
            if volume in existing:
                subprocess.run(docker + ["volume", "rm", volume], check=True)
        backups = target / "deploy/backups"
        if backups.is_symlink():
            raise ValueError("Refusing a symbolic backups directory; installation files retained")
        if backups.exists():
            shutil.move(str(backups), str(recovery / "previous-backups"))
        os.chdir(target.parent)
        shutil.rmtree(target)
        print("Lem removed. Recovery files retained at:", recovery)
        print("Docker, SSH, firewall rules and unrelated services were not changed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", type=Path, default=Path(__file__).resolve().parents[1])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--stop", action="store_true", help="Remove containers; keep all files and volumes")
    group.add_argument("--purge", action="store_true", help="Remove containers, volumes and installation files")
    parser.add_argument("--skip-backup", action="store_true", help="With --purge only: explicitly skip the new database backup")
    args = parser.parse_args()
    if args.skip_backup and not args.purge:
        parser.error("--skip-backup requires --purge")
    try:
        os.umask(0o077)
        execute(args.installation, "purge" if args.purge else "stop" if args.stop else None, args.skip_backup)
    except (OSError, ValueError, KeyError, EOFError, subprocess.CalledProcessError) as error:
        raise SystemExit("Uninstall stopped: " + str(error))


if __name__ == "__main__":
    main()
