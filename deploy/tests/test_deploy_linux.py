# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Exercise Linux deployment without a live SSH server or Docker."""

from pathlib import Path
import base64, hashlib, json, os, subprocess, zipfile
import pytest
from test_deploy import deployment, calls


def invoke(deployment, *args, envextra=None, input_text=None):
    project, home, target, env, log = deployment
    return subprocess.run(
        ["bash", str(project / "deploy/deploy.sh"), *args],
        env={**env, **(envextra or {})},
        capture_output=True,
        text=True,
        timeout=45,
        input=input_text,
    )


def test_package_only_excludes_private_files_and_normalises_shell(deployment, tmp_path):
    project = deployment[0]
    (project / "deploy/example.sh").write_bytes(b"\xef\xbb\xbf#!/bin/bash\r\necho ok\r\n")
    output = tmp_path / "payload with spaces.zip"
    r = invoke(deployment, "--package-only", str(output))
    assert r.returncode == 0, r.stdout + r.stderr
    with zipfile.ZipFile(output) as z:
        names = z.namelist()
        assert "server/run.py" in names and ".dockerignore" in names
        assert "deploy/deploy.sh" in names and "deploy/deploy-bootstrap.py" in names
        assert not any(
            n.endswith((".env", ".db", ".db-wal", ".dump"))
            or "/.venv/" in n
            or n.startswith("app/")
            for n in names
        )
        assert z.read("deploy/example.sh") == b"#!/bin/bash\necho ok\n"
        assert hashlib.sha256(output.read_bytes()).hexdigest() in r.stdout
    assert calls(deployment) == []


def test_normal_update_preserves_data_uses_key_port_and_removes_stage(deployment):
    project, home, target, env, log = deployment
    oldenv = (target / "deploy/.env").read_bytes()
    key = project / "key with spaces"
    key.write_text("PRIVATE SSH KEY")
    r = invoke(deployment, "--key", str(key), "--port", "2222")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (
        target / "deploy/.env"
    ).read_bytes() == oldenv + b"LEM_LATEST_CLIENT_VERSION=0.7.0\nLEM_MIN_CLIENT_VERSION=0.6.1\n"
    assert (target / "server/run.py").read_bytes() == (project / "server/run.py").read_bytes()
    assert next((target / ".deploy-backups").glob("*/server/obsolete.py")).exists()
    assert next((target / "deploy/backups").glob("*.dump")).read_bytes().startswith(b"PGDMP")
    assert (target / "deploy/deploy.sh").stat().st_mode & 0o111
    assert (target / "deploy/.env").stat().st_mode & 0o077 == 0
    commands = calls(deployment)
    assert not any(c[:3] == ["docker", "volume", "rm"] for c in commands)
    assert all("-i" in c and str(key) in c for c in commands if c[0] in ("ssh", "scp"))
    assert all("2222" in c for c in commands if c[0] in ("ssh", "scp"))
    assert not list((home / ".cache").glob("lem-deploy-*"))
    assert "TEST_ADMIN_PASSWORD" not in r.stdout + r.stderr


def test_remote_path_with_spaces_and_shell_characters_is_only_data(deployment):
    project, home, target, env, log = deployment
    moved = target.with_name("installation with spaces; touch SHOULD_NOT_EXIST")
    target.rename(moved)
    r = invoke(deployment, "--remote-path", str(moved), "--domain", "test.example.org")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (moved / "server/run.py").read_bytes() == (project / "server/run.py").read_bytes()
    assert not (home / "SHOULD_NOT_EXIST").exists()


def test_build_failure_keeps_running_installation(deployment):
    r = invoke(deployment, envextra={"FAIL_BUILD": "1"})
    assert r.returncode != 0
    assert (deployment[2] / "server/obsolete.py").exists()
    assert not any(c[0] == "docker" and ("stop" in c or "rm" in c) for c in calls(deployment))
    assert list((deployment[1] / ".cache").glob("lem-deploy-*"))


def test_backup_failure_aborts_before_stop(deployment):
    commands = Path(deployment[3]["PATH"].split(os.pathsep)[0])
    docker = commands / "docker"
    script = docker.read_text().replace(
        "elif 'pg_dump' in args:sys.stdout.buffer.write(b'PGDMP-mock-backup')",
        "elif 'pg_dump' in args:sys.exit(9)",
    )
    docker.write_text(script)
    r = invoke(deployment)
    assert r.returncode != 0
    assert 'Backup falhou; API não parada' in r.stderr
    assert 'A criar backup da base de dados' in r.stdout
    assert not list((deployment[2] / 'deploy/backups').glob('*.dump'))
    assert (deployment[2] / "server/obsolete.py").exists()
    assert not any(c[0] == "docker" and ("stop" in c or "rm" in c) for c in calls(deployment))


def test_backup_is_visible_bounded_and_uses_noninteractive_sudo(deployment):
    r = invoke(deployment)
    assert r.returncode == 0, r.stdout + r.stderr
    cmds = calls(deployment)
    dump = next(c for c in cmds if c[0] == 'docker' and 'pg_dump' in c)
    assert '--verbose' in dump and '--lock-wait-timeout=60s' in dump
    assert '--no-password' in dump and 'PGCONNECT_TIMEOUT=15' in dump
    assert len([c for c in cmds if c == ['sudo', '-v']]) == 2
    assert any(c[:3] == ['sudo', '-n', 'docker'] and 'pg_dump' in c for c in cmds)
    assert r.stdout.index('A criar backup') < r.stdout.index('Backup:')
    assert cmds.index(dump) < next(i for i, c in enumerate(cmds) if c[0] == 'docker' and 'stop' in c)


def test_reset_is_explicit_and_only_removes_database(deployment):
    r = invoke(deployment, "--reset-database")
    assert r.returncode == 0, r.stdout + r.stderr
    assert [c for c in calls(deployment) if c[:3] == ["docker", "volume", "rm"]] == [
        ["docker", "volume", "rm", "lem_pgdata"]
    ]
    assert not any("pg_dump" in c for c in calls(deployment))
    assert (deployment[2] / "deploy/.env").exists()


def test_domain_mismatch_aborts_before_build(deployment):
    r = invoke(deployment, "--domain", "wrong.example.org")
    assert r.returncode != 0
    assert not any(c[0] == "docker" and "build" in c for c in calls(deployment))


@pytest.mark.parametrize(
    "args",
    [
        ("--host", "bad;touch BAD"),
        ("--port", "0"),
        ("--port", "65536"),
        ("--port", "-1"),
        ("--user", "bad@user"),
        ("--bad-option",),
        ("--key",),
    ],
)
def test_invalid_arguments_do_not_connect(deployment, args):
    assert invoke(deployment, *args).returncode != 0
    assert calls(deployment) == []


@pytest.mark.parametrize("directory", [False, True])
def test_symlink_rejected_before_connect(deployment, tmp_path, directory):
    private = tmp_path / "private.txt"
    private.write_text("PRIVATE")
    if directory:
        outside = tmp_path / "private server"
        (deployment[0] / "server").rename(outside)
        (deployment[0] / "server").symlink_to(outside, target_is_directory=True)
    else:
        (deployment[0] / "server/linked.txt").symlink_to(private)
    r = invoke(deployment)
    assert r.returncode != 0
    assert calls(deployment) == []


@pytest.mark.parametrize(
    "name", ["server/../escape", "/server/escape", "deploy/.env", "server\\..\\escape"]
)
def test_bootstrap_rejects_unsafe_archive_before_docker(deployment, name):
    project, home, target, env, log = deployment
    release = "a" * 32
    stage = home / ".cache" / ("lem-deploy-" + release)
    stage.mkdir(parents=True)
    archive = stage / "payload.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(name, "INVALID")
    c = {
        "release": release,
        "transfer_dir": ".cache/lem-deploy-" + release,
        "archive_sha": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "remote_path": "auto",
        "reset": False,
        "domain": "",
    }
    arg = base64.b64encode(json.dumps(c).encode()).decode()
    r = subprocess.run(
        ["python3", str(project / "deploy/deploy-bootstrap.py"), arg],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0
    assert calls(deployment) == []
    assert (target / "server/obsolete.py").exists()


def test_bootstrap_rejects_corrupt_transfer(deployment):
    project, home, target, env, log = deployment
    release = "b" * 32
    stage = home / ".cache" / ("lem-deploy-" + release)
    stage.mkdir(parents=True)
    (stage / "payload.zip").write_bytes(b"CORRUPT")
    c = {
        "release": release,
        "transfer_dir": ".cache/lem-deploy-" + release,
        "archive_sha": "0" * 64,
        "remote_path": "auto",
        "reset": False,
        "domain": "",
    }
    arg = base64.b64encode(json.dumps(c).encode()).decode()
    r = subprocess.run(
        ["python3", str(project / "deploy/deploy-bootstrap.py"), arg],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0 and "SHA256" in r.stderr
    assert calls(deployment) == []


def legacy_env(deployment):
    p = deployment[2] / "deploy/.env"
    p.write_text(
        "PREVIOUS_DOMAIN=test.example.org\nPOSTGRES_PASSWORD=TEST_DATABASE\nADMIN_EMAIL=test@example.org\nADMIN_PASSWORD=TEST_ADMIN_PASSWORD\n"
    )
    p.chmod(0o600)
    return p.read_bytes()


def test_legacy_branding_requires_explicit_reset_before_build(deployment):
    before = legacy_env(deployment)
    r = invoke(deployment, envextra={"LEGACY_INSTALL": "1"})
    assert r.returncode != 0 and "--reset-database" in r.stderr
    assert (deployment[2] / "deploy/.env").read_bytes() == before
    assert not any(
        c[0] == "docker" and ("build" in c or "stop" in c or "rm" in c) for c in calls(deployment)
    )


def test_legacy_reset_renames_project_and_database_preserving_https(deployment):
    before = legacy_env(deployment)
    target = deployment[2]
    r = invoke(deployment, "--reset-database", envextra={"LEGACY_INSTALL": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    env = (target / "deploy/.env").read_text()
    assert "PREVIOUS_DOMAIN" not in env and "LEM_DOMAIN=test.example.org" in env
    assert "COMPOSE_PROJECT_NAME=lem" in env
    assert "LEM_TLS_DATA_VOLUME=previousapp_caddydata" in env
    assert "LEM_TLS_CONFIG_VOLUME=previousapp_caddyconfig" in env
    assert "ADMIN_PASSWORD=TEST_ADMIN_PASSWORD" in env
    assert next((target / ".deploy-backups").glob("*/deploy/.env")).read_bytes() == before
    cmds = calls(deployment)
    assert [c for c in cmds if c[:3] == ["docker", "volume", "rm"]] == [
        ["docker", "volume", "rm", "previousapp_pgdata"]
    ]
    assert any(c[0] == "docker" and "rm" in c and "proxy" in c for c in cmds)
    assert all("lem" in c for c in cmds if c[0] == "docker" and "up" in c)
    assert "POSTGRES_DB: lem" in (target / "deploy/compose.yml").read_text()
    assert "USER lem" in (target / "deploy/Dockerfile").read_text()
    assert "TEST_ADMIN_PASSWORD" not in r.stdout + r.stderr
    assert (target / "deploy/.env").stat().st_mode & 0o077 == 0


def test_legacy_collision_does_not_delete_another_lem_database(deployment):
    before = legacy_env(deployment)
    r = invoke(deployment, "--reset-database", envextra={"LEGACY_INSTALL": "1", "COLLISION": "1"})
    assert r.returncode != 0
    assert not any(c[:3] == ["docker", "volume", "rm"] for c in calls(deployment))
    assert (deployment[2] / "deploy/.env").read_bytes() == before


def test_legacy_failed_build_preserves_old_env_and_running_installation(deployment):
    before = legacy_env(deployment)
    r = invoke(deployment, "--reset-database", envextra={"LEGACY_INSTALL": "1", "FAIL_BUILD": "1"})
    assert r.returncode != 0
    assert (deployment[2] / "deploy/.env").read_bytes() == before
    assert (deployment[2] / "server/obsolete.py").exists()
    assert not any(c[0] == "docker" and ("stop" in c or "rm" in c) for c in calls(deployment))


def test_legacy_foreign_volume_refuses_reset(deployment):
    legacy_env(deployment)
    r = invoke(deployment, "--reset-database", envextra={"LEGACY_INSTALL": "1", "FAIL_LABEL": "1"})
    assert r.returncode != 0
    assert not any(c[:3] == ["docker", "volume", "rm"] for c in calls(deployment))
    assert (deployment[2] / "server/obsolete.py").exists()


def test_missing_https_volume_aborts_migration_before_reset(deployment):
    before = legacy_env(deployment)
    r = invoke(deployment, "--reset-database", envextra={"LEGACY_INSTALL": "1", "FAIL_TLS": "1"})
    assert r.returncode != 0 and "HTTPS" in r.stderr
    assert (deployment[2] / "deploy/.env").read_bytes() == before
    assert not any(
        c[0] == "docker" and ("build" in c or "stop" in c or "rm" in c) for c in calls(deployment)
    )


def test_backup_does_not_attach_or_consume_ssh_stdin(deployment):
    commands = Path(deployment[3]["PATH"].split(os.pathsep)[0])
    docker = commands / "docker"
    original = "elif 'pg_dump' in args:sys.stdout.buffer.write(b'PGDMP-mock-backup')"
    replacement = """elif 'pg_dump' in args:
        if '--interactive=false' not in args:
            sys.stderr.write('Compose would attach SSH stdin')
            sys.exit(20)
        if sys.stdin.buffer.read(1):
            sys.stderr.write('Backup consumed SSH stdin')
            sys.exit(21)
        sys.stdout.buffer.write(b'PGDMP-mock-backup')"""
    assert original in docker.read_text()
    docker.write_text(docker.read_text().replace(original, replacement))
    r = invoke(deployment, input_text='SSH input must not reach the backup\n')
    assert r.returncode == 0, r.stdout + r.stderr
    assert next((deployment[2] / "deploy/backups").glob("*.dump")).read_bytes() == b'PGDMP-mock-backup'
