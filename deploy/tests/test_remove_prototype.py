# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Never accesses real Docker or deletes host data."""

import importlib.util
import json
from pathlib import Path
import pytest

script = Path(__file__).resolve().parents[1] / "remove-prototype.py"
spec = importlib.util.spec_from_file_location("remove_prototype", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def prototype(tmp_path):
    target = tmp_path / "old-prototype"
    for file in ["deploy/compose.yml", "deploy/.env", "server/run.py", "shared/checklist-v1.json"]:
        p = target / file
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("fixture")
    return target


def config():
    return dict(
        name="prototype",
        services={k: {} for k in ["api", "db", "proxy"]},
        volumes={k: {"name": "prototype_" + k} for k in ["pgdata", "caddydata", "caddyconfig"]},
    )


def test_plan_uses_exact_metadata_and_treats_path_as_argument(prototype):
    calls = []
    result = module.plan(prototype, lambda command: (calls.append(command) or json.dumps(config())))
    assert result[0] == prototype and result[2] == "prototype"
    assert result[3] == ["prototype_pgdata", "prototype_caddydata", "prototype_caddyconfig"]
    assert calls[0][-3:] == ["config", "--format", "json"]
    assert prototype.exists()


@pytest.mark.parametrize("path", ["/", "/opt", "/home", "/srv", "/usr", "/var"])
def test_system_roots_rejected(path):
    with pytest.raises(ValueError):
        module.plan(path, lambda _: pytest.fail("Docker must not be called"))


def test_missing_markers_and_symlink_rejected(tmp_path, prototype):
    with pytest.raises(ValueError):
        module.plan(tmp_path, lambda _: pytest.fail("Must not inspect Docker"))
    link = tmp_path / "link"
    link.symlink_to(prototype, target_is_directory=True)
    with pytest.raises(ValueError):
        module.plan(link, lambda _: pytest.fail("Must not inspect Docker"))


def test_unknown_resources_and_invalid_names_rejected(prototype):
    for key, value in [
        ("services", {"other": {}}),
        ("name", "unsafe;name"),
        ("volumes", {"other": {"name": "anything"}}),
    ]:
        data = config()
        data[key] = value
        with pytest.raises(ValueError):
            module.plan(prototype, lambda _: json.dumps(data))


def test_default_only_plans_and_does_not_delete(prototype, monkeypatch):
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda cmd, **kw: (calls.append(cmd) or SimpleNamespace(stdout=json.dumps(config()))),
    )
    module.execute(prototype)
    assert prototype.exists() and len(calls) == 1


def test_explicit_delete_is_scoped_and_stops_on_volume_failure(prototype, monkeypatch):
    from types import SimpleNamespace

    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[-3:] == ["config", "--format", "json"]:
            return SimpleNamespace(stdout=json.dumps(config()))
        if cmd[2:5] == ["volume", "ls", "--format"]:
            return SimpleNamespace(
                stdout="prototype_caddydata\nprototype_caddyconfig\nother_volume\n"
            )
        if cmd[:4] == ["sudo", "docker", "volume", "rm"]:
            raise module.subprocess.CalledProcessError(1, cmd)
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(module.subprocess.CalledProcessError):
        module.execute(prototype, delete=True)
    assert prototype.exists()
    assert not any("other_volume" in c for c in calls)


def test_explicit_delete_removes_only_selected_directory(prototype, monkeypatch):
    from types import SimpleNamespace

    sibling = prototype.parent / "keep"
    sibling.mkdir()
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[-3:] == ["config", "--format", "json"]:
            return SimpleNamespace(stdout=json.dumps(config()))
        if cmd[2:5] == ["volume", "ls", "--format"]:
            return SimpleNamespace(stdout="prototype_caddydata\nother_volume\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(module.subprocess, "run", run)
    module.execute(prototype, delete=True)
    assert not prototype.exists() and sibling.exists()
    assert ["sudo", "docker", "volume", "rm", "prototype_caddydata"] in calls
    assert not any("other_volume" in c for c in calls)
