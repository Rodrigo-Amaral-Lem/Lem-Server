# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Wrapper argument isolation with a fake Docker executable; no daemon required."""

import json, os, subprocess
from pathlib import Path


def test_wrapper_keeps_paths_and_arguments_separate(tmp_path):
    deploy = tmp_path / "project with spaces" / "deploy"
    deploy.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "manage-server.sh"
    script = deploy / source.name
    script.write_bytes(source.read_bytes())
    (deploy / ".env").write_text("PRIVATE=never-print-me\n")
    (deploy / "compose.yml").write_text("name: lem\n")
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text("#!/usr/bin/env python3\nimport sys,json\nprint(json.dumps(sys.argv[1:]))\n")
    docker.chmod(0o755)
    result = subprocess.run(
        ["bash", str(script), "users", "create", "--name", "A name; $(bad)", "--email", "x@lem.pt"],
        cwd=tmp_path,
        env={**os.environ, "PATH": str(binary) + ":" + os.environ["PATH"]},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    args = json.loads(result.stdout)
    assert args[:5] == [
        "compose",
        "--env-file",
        str(deploy / ".env"),
        "-f",
        str(deploy / "compose.yml"),
    ]
    assert args[5:10] == ["exec", "-T", "api", "python", "-m"]
    assert args[10:] == [
        "app.admin_cli",
        "users",
        "create",
        "--name",
        "A name; $(bad)",
        "--email",
        "x@lem.pt",
    ]
    assert "never-print-me" not in result.stdout
