# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Updating server code must also admit its accompanying client release."""

import json
import runpy
from pathlib import Path
import pytest

prepare = runpy.run_path(str(Path(__file__).resolve().parents[1] / "prepare-config.py"))["prepare"]


@pytest.mark.parametrize(
    "latest", [
        "0.1.28", "0.1.45", "0.1.52", "0.1.57", "0.2.0", "0.2.1", "0.2.2",
        "0.2.3", "0.2.4", "0.2.5", "0.2.6", "0.3.0", "0.3.1", "0.3.2", "0.4.0", "0.4.1",
        "0.5.0", "0.5.1", "0.5.2", "0.5.3", "0.5.4", "0.5.5", "0.6.0", "0.6.1",
    ]
)
def test_promote_pinned_versions_without_changing_secrets(tmp_path, latest):
    env = tmp_path / "old.env"
    meta = tmp_path / "meta.json"
    out = tmp_path / "next.env"
    env.write_text(
        f"LEM_DOMAIN=test.example.org\nADMIN_PASSWORD=PRIVATE_FIXTURE\nLEM_MIN_CLIENT_VERSION=0.1.28\nLEM_LATEST_CLIENT_VERSION={latest}\n"
    )
    meta.write_text(
        json.dumps(
            {
                "volumes": {
                    "caddydata": {"name": "lem_caddydata"},
                    "caddyconfig": {"name": "lem_caddyconfig"},
                }
            }
        )
    )
    prepare(env, meta, out)
    result = out.read_text()
    assert "LEM_LATEST_CLIENT_VERSION=0.10.0\n" in result
    assert "LEM_MIN_CLIENT_VERSION=0.9.1\n" in result
    assert "ADMIN_PASSWORD=PRIVATE_FIXTURE\n" in result
    assert out.stat().st_mode & 0o077 == 0
