# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Exercise the actual installer release/repository selection without apt or Docker."""
from pathlib import Path
import subprocess
import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "install-ubuntu.sh"


@pytest.mark.parametrize("release,suite", [("24.04", "noble"), ("26.04", "resolute")])
def test_official_repository_matches_host_release(release, suite):
    source = INSTALLER.read_text()
    routing = source.split("source /etc/os-release\n", 1)[1].split("[[ ! -e .env ]]", 1)[0]
    repo = next(line.strip() for line in source.splitlines() if "Types: deb" in line)
    repo = repo.split(" | sudo tee", 1)[0]
    code = 'fail(){ exit 1; }; ID=ubuntu; VERSION_ID="$1";\n' + routing + repo
    result = subprocess.run(["bash", "-c", code, "installer-test", release], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert f"Suites: {suite}\n" in result.stdout
    assert "Architectures: " in result.stdout
    assert "Signed-By: /etc/apt/keyrings/docker.asc" in result.stdout


@pytest.mark.parametrize("system,release", [("ubuntu", "20.04"), ("debian", "26.04")])
def test_unsupported_hosts_fail_before_repository_setup(system, release):
    source = INSTALLER.read_text()
    routing = source.split("source /etc/os-release\n", 1)[1].split("[[ ! -e .env ]]", 1)[0]
    code = 'fail(){ exit 1; }; ID="$1"; VERSION_ID="$2";\n' + routing
    result = subprocess.run(["bash", "-c", code, "installer-test", system, release])
    assert result.returncode != 0


def test_credentials_shown_before_a_later_startup_failure(tmp_path):
    source = INSTALLER.read_text()
    block = source.split("lem_db_password=$(openssl rand -hex 24)", 1)[1]
    block = "lem_db_password=$(openssl rand -hex 24)" + block.split("sudo ufw allow", 1)[0]
    code = "set -e; umask 077; lem_domain=example.test; lem_admin_email=test@example.test; lem_admin_name='Rodrigo Amaral';\n" + block + "\nfalse\n"
    result = subprocess.run(["bash", "-c", code], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode != 0
    values = dict(line.split("=", 1) for line in (tmp_path / ".env").read_text().splitlines())
    assert "Password inicial: " + values["ADMIN_PASSWORD"] in result.stdout
    assert values["POSTGRES_PASSWORD"] not in result.stdout
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    assert "ADMIN_NAME='Rodrigo Amaral'" in (tmp_path / ".env").read_text()


@pytest.mark.parametrize("containers,volume,success", [("", False, True), ("abc", False, False), ("", True, False)])
def test_existing_database_or_containers_block_new_credentials(containers, volume, success):
    source = INSTALLER.read_text()
    block = source.split("# A fresh .env", 1)[1].split("# Random hex", 1)[0]
    block = block[block.index("[[ -z"):]
    code = '''set -e
fail(){ exit 9; }
sudo(){
 if [[ "$2" == ps ]]; then printf '%s' "$TEST_CONTAINERS"; return 0; fi
 if [[ "$2" == volume ]]; then return "$TEST_VOLUME_STATUS"; fi
 return 77
}
''' + block
    import os
    env = dict(os.environ, TEST_CONTAINERS=containers, TEST_VOLUME_STATUS="0" if volume else "1")
    result = subprocess.run(["bash", "-c", code], capture_output=True, text=True, env=env)
    assert (result.returncode == 0) == success


@pytest.mark.parametrize("name", ["Rodrigo Amaral", "João D'Ávila", 'Nome $ # "Teste"'])
def test_chosen_name_survives_dotenv_roundtrip(tmp_path, name):
    from dotenv import dotenv_values
    source = INSTALLER.read_text()
    block = source.split("lem_db_password=$(openssl rand -hex 24)", 1)[1]
    block = "lem_db_password=$(openssl rand -hex 24)" + block.split("sudo ufw allow", 1)[0]
    code = 'set -e; umask 077; lem_domain=example.test; lem_admin_email=test@example.test; lem_admin_name="$1";\n' + block
    result = subprocess.run(["bash", "-c", code, "test-name", name], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert dotenv_values(tmp_path / ".env")["ADMIN_NAME"] == name
