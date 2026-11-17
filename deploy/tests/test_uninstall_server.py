# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Exercise destructive boundaries with fake Docker; no host services are used."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import pytest

pytest.importorskip("fcntl", reason="Server uninstallation requires POSIX file locking")

spec = importlib.util.spec_from_file_location("uninstall", Path(__file__).parents[1] / "uninstall-server.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


@pytest.fixture
def env(tmp_path, monkeypatch):
    target = tmp_path / "Lem"
    for name in ("deploy/.env", "deploy/compose.yml", "deploy/Caddyfile", "server/run.py", "shared/checklist-v1.json", "deploy/backups/old.dump"):
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    config = dict(name="lem", services={k: {} for k in ("api", "db", "proxy")},
                  volumes={k: {"name": "lem_" + k} for k in ("pgdata", "caddydata", "caddyconfig")})
    container = dict(Id="our-container", Config={"Labels": {"com.docker.compose.project.config_files": str(target / "deploy/compose.yml"),
                                       "com.docker.compose.service": "db"}}, Mounts=[{"Type": "volume", "Name": "lem_pgdata", "Destination": "/var/lib/postgresql/data"}])
    state = dict(config=config, container=container, shared=False, calls=[], fail_backup=False, fail_volume=False)
    def fake_run(cmd):
        state["calls"].append(cmd)
        if cmd[-3:] == ["config", "--format", "json"]: return json.dumps(config)
        if "inspect" in cmd: return json.dumps([container])
        if "volume" in cmd and "ls" in cmd: return "lem_pgdata\nlem_caddydata\nlem_caddyconfig\n"
        if "ps" in cmd:
            return "other-container\n" if state["shared"] and any(x.startswith("volume=") for x in cmd) else "our-container\n"
        raise AssertionError(cmd)
    def fake_subprocess(cmd, **kwargs):
        state["calls"].append(cmd)
        if "pg_dump" in cmd:
            if state["fail_backup"]: raise subprocess.CalledProcessError(1, cmd)
            kwargs["stdout"].write(b"database backup")
        if "volume" in cmd and "rm" in cmd and state["fail_volume"]:
            raise subprocess.CalledProcessError(1, cmd)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(m, "run", fake_run)
    monkeypatch.setattr(m.subprocess, "run", fake_subprocess)
    monkeypatch.setattr(m.os, "chdir", lambda path: None)
    return target, state


def confirm(monkeypatch, *answers):
    iterator = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _: next(iterator))


def mutations(state):
    return [c for c in state["calls"] if "down" in c or "pg_dump" in c or ("volume" in c and "rm" in c)]


def test_preview_is_read_only(env):
    target, state = env
    m.execute(target)
    assert target.exists() and not mutations(state)


def test_stop_retains_files_and_volumes(env, monkeypatch):
    target, state = env
    confirm(monkeypatch, "PARAR")
    m.execute(target, "stop")
    assert target.exists()
    assert [c[-1] for c in mutations(state)] == ["down"]


@pytest.mark.parametrize("answers", [("apagar",), ("APAGAR", "/wrong/path")])
def test_confirmation_prevents_mutation(env, monkeypatch, answers):
    target, state = env
    confirm(monkeypatch, *answers)
    with pytest.raises(ValueError): m.execute(target, "purge")
    assert target.exists() and not mutations(state)


def test_purge_backup_precedes_down_and_preserves_old_backups(env, monkeypatch):
    target, state = env
    confirm(monkeypatch, "APAGAR", str(target))
    m.execute(target, "purge")
    assert not target.exists()
    recovery, = target.parent.glob("Lem-uninstall-backup-*")
    assert (recovery / "database.dump").read_bytes() == b"database backup"
    assert (recovery / "previous-backups/old.dump").read_text() == "fixture"
    assert (recovery / ".env").stat().st_mode & 0o777 == 0o600
    steps = mutations(state)
    assert "pg_dump" in steps[0] and steps[1][-1] == "down"
    assert len(steps) == 5


def test_backup_failure_leaves_services_and_installation(env, monkeypatch):
    target, state = env
    state["fail_backup"] = True
    confirm(monkeypatch, "APAGAR", str(target))
    with pytest.raises(subprocess.CalledProcessError): m.execute(target, "purge")
    assert target.exists() and not any("down" in c for c in state["calls"])


def test_wrong_installation_refused(env):
    target, state = env
    state["container"]["Config"]["Labels"]["com.docker.compose.project.config_files"] = "/other/Lem/deploy/compose.yml"
    with pytest.raises(ValueError, match="another installation"): m.execute(target, "purge")
    assert not mutations(state)


def test_shared_volume_refused(env):
    target, state = env
    state["shared"] = True
    with pytest.raises(ValueError, match="another installation"): m.execute(target, "purge")
    assert not mutations(state)


def test_volume_failure_retains_installation(env, monkeypatch):
    target, state = env
    state["fail_volume"] = True
    confirm(monkeypatch, "APAGAR", str(target))
    with pytest.raises(subprocess.CalledProcessError): m.execute(target, "purge")
    assert target.exists() and (target / "deploy/backups/old.dump").exists()


def test_skip_backup_is_explicit(env, monkeypatch):
    target, state = env
    confirm(monkeypatch, "APAGAR", str(target))
    m.execute(target, "purge", skip_backup=True)
    assert not target.exists() and not any("pg_dump" in c for c in state["calls"])


def test_active_deploy_lock_prevents_stop(env, monkeypatch):
    target, state = env
    with (target / "deploy/.deploy.lock").open("a") as lock:
        m.fcntl.flock(lock, m.fcntl.LOCK_EX | m.fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="deploy/uninstall"): m.execute(target, "stop")
    assert not mutations(state)


def test_symlink_installation_refused(env):
    target, state = env
    link = target.parent / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"): m.execute(link)
    assert not mutations(state)


def test_missing_env_uses_exact_container_ids_and_backs_up(env, monkeypatch):
    target, state = env
    (target / "deploy/.env").unlink()
    confirm(monkeypatch, "APAGAR", str(target))
    m.execute(target, "purge")
    assert not target.exists()
    assert any("pg_dump" in c and "our-container" in c for c in state["calls"])
    assert any(c[-2:] == ["stop", "our-container"] for c in state["calls"])
    assert any(c[-2:] == ["rm", "our-container"] for c in state["calls"])
    removed = [c[-1] for c in state["calls"] if "volume" in c and "rm" in c]
    assert removed == ["lem_pgdata"]  # Unproven certificate volumes retained.
    assert not any("compose" in c for c in state["calls"])


def test_missing_env_other_installation_is_left_running(env, monkeypatch):
    target, state = env
    (target / "deploy/.env").unlink()
    state["container"]["Config"]["Labels"]["com.docker.compose.project.config_files"] = "/other/Lem/deploy/compose.yml"
    confirm(monkeypatch, "APAGAR", str(target))
    m.execute(target, "purge")
    assert not target.exists()
    assert not mutations(state)
    assert not any("stop" in c or "rm" in c for c in state["calls"])


def test_partial_shared_database_volume_is_retained(env, monkeypatch):
    target, state = env
    (target / "deploy/.env").unlink()
    state["shared"] = True
    confirm(monkeypatch, "APAGAR", str(target))
    m.execute(target, "purge")
    assert not target.exists()
    assert not mutations(state)
    assert any(c[-2:] == ["stop", "our-container"] for c in state["calls"])


def test_missing_env_backup_failure_aborts_before_container_removal(env, monkeypatch):
    target, state = env
    (target / "deploy/.env").unlink()
    state["fail_backup"] = True
    confirm(monkeypatch, "APAGAR", str(target))
    with pytest.raises(subprocess.CalledProcessError): m.execute(target, "purge")
    assert target.exists()
    assert not any("stop" in c or "rm" in c for c in state["calls"])


def test_arbitrary_incomplete_directory_refused(env):
    target, state = env
    for name in ("deploy/.env", "deploy/compose.yml", "server/run.py"):
        (target / name).unlink()
    with pytest.raises(ValueError, match="recognizable"): m.execute(target, "purge")
    assert not mutations(state)
