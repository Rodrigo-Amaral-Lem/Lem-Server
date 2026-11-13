from pathlib import Path
import runpy
import pytest

resolve = runpy.run_path(str(Path(__file__).resolve().parents[1] / "find-installation.py"))[
    "resolve_target"
]


def installation(home, name, domain="example.test"):
    root = home / "apps" / name
    for path, data in [
        ("deploy/.env", "LEM_DOMAIN=" + domain),
        ("deploy/compose.yml", "services: {}"),
        ("server/run.py", ""),
        ("shared/checklist-v1.json", "{}"),
    ]:
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(data)
    return root


def test_existing_folder_name_does_not_matter(tmp_path):
    target = installation(tmp_path, "Previous installation")
    assert resolve("auto", tmp_path) == target


def test_ambiguous_installs_need_explicit_path(tmp_path):
    installation(tmp_path, "One")
    installation(tmp_path, "Two")
    with pytest.raises(ValueError):
        resolve("auto", tmp_path)


def test_domain_can_disambiguate(tmp_path):
    installation(tmp_path, "One", "one.test")
    target = installation(tmp_path, "Two", "two.test")
    assert resolve("auto", tmp_path, "two.test") == target


def test_explicit_path_is_respected(tmp_path):
    assert resolve("apps/Selected", tmp_path) == tmp_path / "apps/Selected"


def test_missing_install_does_not_create_one(tmp_path):
    with pytest.raises(ValueError):
        resolve("auto", tmp_path)
    assert not (tmp_path / "apps").exists()
