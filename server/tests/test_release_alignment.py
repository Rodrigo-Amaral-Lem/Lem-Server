# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""A client release must be admitted by its accompanying deployment defaults."""
import re
import os
import pytest
from pathlib import Path
from app.compatibility import ClientPolicy, VERSION, MINIMUM_CLIENT


def test_app_and_server_release_are_aligned(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    app_root = Path(os.environ.get("LEM_APP_REPO", str(root.parent / "App")))
    if not (app_root / "app/CMakeLists.txt").exists():
        pytest.skip("Release alignment requires sibling App repo or LEM_APP_REPO")
    cmake = (app_root / "app/CMakeLists.txt").read_text()
    version = re.search(r"project\(Lem VERSION ([0-9.]+)", cmake)[1]
    assert VERSION == version
    assert f"image: lem-api:{version}" in (root / "deploy/compose.yml").read_text()
    assert f'setApplicationVersion("{version}")' in (app_root / "app/src/main.cpp").read_text()
    assert f'QT_ANDROID_VERSION_NAME "{version}"' in cmake
    assert 'QT_ANDROID_VERSION_CODE 194' in cmake
    for name in [".env.example", "install-ubuntu.sh"]:
        assert f"LEM_LATEST_CLIENT_VERSION={version}" in (root / "deploy" / name).read_text()
        assert f"LEM_MIN_CLIENT_VERSION={MINIMUM_CLIENT}" in (root / "deploy" / name).read_text()
    assert f"LEM_LATEST_CLIENT_VERSION:-{version}" in (root / "deploy/compose.yml").read_text()
    assert f"LEM_MIN_CLIENT_VERSION:-{MINIMUM_CLIENT}" in (root / "deploy/compose.yml").read_text()
    monkeypatch.delenv("LEM_MIN_CLIENT_VERSION", raising=False)
    monkeypatch.delenv("LEM_LATEST_CLIENT_VERSION", raising=False)
    monkeypatch.delenv("LEM_UPDATE_URL", raising=False)
    result = ClientPolicy().evaluate(version)
    assert result["compatible"] and not result["update_available"]
