# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""No live SSH/Docker: exercise PowerShell packaging + remote flow with mocks.
Run: PWSH=/path/to/pwsh python -m pytest deploy/tests -q
"""

from pathlib import Path
import json, os, shutil, subprocess, sys, zipfile
import pytest

ROOT = Path(__file__).resolve().parents[2]
PWSH = os.getenv("PWSH") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(
    os.name != "posix" or not PWSH,
    reason="Remote-flow mocks require POSIX executable scripts, Bash and PowerShell 7",
)
MOCK = r"""#!/usr/bin/env python3
import json,os,sys,subprocess,shutil
from pathlib import Path
name=Path(sys.argv[0]).name;args=sys.argv[1:];home=Path(os.environ['HOME'])
with open(os.environ['DEPLOY_LOG'],'a') as f:f.write(json.dumps([name,*args])+'\n')
if name=='sudo':
    if args==['-v']:sys.exit(0)
    if args[:1]==['-n']:args=args[1:]
    os.execvp(args[0],args)
elif name=='ssh':
    sys.exit(subprocess.run(['bash','-c',args[-1]],cwd=home).returncode)
elif name=='scp':
    src=Path(args[-2]);dst=home/args[-1].split(':',1)[1];dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dst)
elif name=='docker':
    project='previousapp' if os.getenv('LEGACY_INSTALL') else 'lem'
    if args[:2]==['volume','inspect']:
        if os.getenv('FAIL_TLS') and args[-1].endswith('_caddydata'):sys.exit(1)
        if args[-1]=='lem_pgdata' and os.getenv('LEGACY_INSTALL') and not os.getenv('COLLISION'):sys.exit(1)
        print(json.dumps([{'Labels':{'com.docker.compose.project':'wrong' if os.getenv('FAIL_LABEL') else project,'com.docker.compose.volume':'pgdata'}}]))
    elif args[:2]==['volume','rm']:pass
    elif 'config' in args and '--format' in args:print(json.dumps({'name':project,'services':{'db':{'environment':{'POSTGRES_USER':'previous_user' if os.getenv('LEGACY_INSTALL') else 'lem','POSTGRES_DB':'previous_db' if os.getenv('LEGACY_INSTALL') else 'lem'}}},'volumes':{k:{'name':project+'_'+k} for k in ('pgdata','caddydata','caddyconfig')}}))
    elif 'build' in args and os.getenv('FAIL_BUILD'):sys.exit(7)
    elif 'pg_dump' in args:sys.stdout.buffer.write(b'PGDMP-mock-backup')
    elif 'version' in args:print('Docker Compose TEST')
elif name=='curl':sys.exit(0)
elif name=='sleep':sys.exit(0)
"""


@pytest.fixture
def deployment(tmp_path):
    if os.name != "posix":
        pytest.skip("SSH/Docker mocks require POSIX executable scripts")
    project = tmp_path / "project with spaces"
    project.mkdir()
    for folder in ["server", "shared", "deploy"]:
        shutil.copytree(
            ROOT / folder,
            project / folder,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "backups", "build"),
        )
    shutil.copyfile(ROOT / ".dockerignore", project / ".dockerignore")
    (project / "deploy/.env").write_text("PRIVATE_MUST_NOT_UPLOAD=secret")
    (project / "server/private.db").write_bytes(b"PRIVATE DATABASE")
    (project / "server/private.db-wal").write_bytes(b"PRIVATE DATABASE WAL")
    (project / "deploy/backups").mkdir()
    (project / "deploy/backups/old.dump").write_text("PRIVATE BACKUP")
    (project / "server/.venv").mkdir()
    (project / "server/.venv/config").write_text("PRIVATE VENV")
    home = tmp_path / "remote home"
    home.mkdir()
    target = home / "apps/LEM"
    (target / "deploy").mkdir(parents=True)
    for file in ["compose.yml", "Dockerfile", "Caddyfile"]:
        shutil.copyfile(ROOT / "deploy" / file, target / "deploy" / file)
    envfile = target / "deploy/.env"
    envfile.write_text(
        "LEM_DOMAIN=test.example.org\nPOSTGRES_PASSWORD=TEST_DATABASE\nADMIN_EMAIL=test@example.org\nADMIN_PASSWORD=TEST_ADMIN_PASSWORD\nCOMPOSE_PROJECT_NAME=lem\nLEM_TLS_DATA_VOLUME=lem_caddydata\nLEM_TLS_CONFIG_VOLUME=lem_caddyconfig\n"
    )
    envfile.chmod(0o600)
    (target / "server").mkdir()
    (target / "server/run.py").write_text("# old version")
    (target / "server/obsolete.py").write_text("# obsolete")
    (target / "shared").mkdir()
    (target / "shared/old.json").write_text("{}")
    (target / "shared/checklist-v1.json").write_text("{}")
    commands = tmp_path / "bin"
    commands.mkdir()
    for name in ["ssh", "scp", "sudo", "docker", "curl", "sleep"]:
        p = commands / name
        p.write_text(MOCK)
        p.chmod(0o755)
    log = tmp_path / "commands.log"
    env = dict(
        os.environ,
        HOME=str(home),
        PATH=str(commands) + os.pathsep + os.environ["PATH"],
        DEPLOY_LOG=str(log),
    )
    return project, home, target, env, log


def invoke(deployment, *args, envextra=None):
    project, home, target, env, log = deployment
    env = {**env, **(envextra or {})}
    return subprocess.run(
        [
            PWSH,
            "-NoProfile",
            "-File",
            str(project / "deploy/deploy.ps1"),
            "-ProjectPath",
            str(project),
            *args,
        ],
        text=True,
        capture_output=True,
        env=env,
        timeout=45,
    )


def calls(deployment):
    p = deployment[-1]
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_package_excludes_credentials_and_data(deployment, tmp_path):
    output = tmp_path / "package.zip"
    r = invoke(deployment, "-PackageOnly", str(output))
    assert r.returncode == 0, r.stdout + r.stderr
    with zipfile.ZipFile(output) as z:
        files = z.namelist()
        assert "server/run.py" in files and "shared/checklist-v1.json" in files
        assert ".dockerignore" in files
        assert not any(
            n.endswith((".env", ".db", ".db-wal", ".dump")) or "/.venv/" in n for n in files
        )
        assert not any(n.startswith("app/") for n in files)
        assert b"\r" not in z.read("deploy/remote-deploy.sh")
    assert calls(deployment) == []


def test_default_project_path_is_relative_to_script_not_working_directory(deployment, tmp_path):
    project, home, target, env, log = deployment
    output = tmp_path / "default-path-package.zip"
    # No -ProjectPath: exercise the same invocation used on the Windows laptop.
    r = subprocess.run(
        [
            PWSH,
            "-NoProfile",
            "-File",
            str(project / "deploy/deploy.ps1"),
            "-PackageOnly",
            str(output),
        ],
        cwd=home,
        env=env,
        text=True,
        capture_output=True,
        timeout=45,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    with zipfile.ZipFile(output) as z:
        assert z.read("server/run.py") == (project / "server/run.py").read_bytes()
    assert calls(deployment) == []


def test_normal_update_keeps_env_and_backs_up_database(deployment):
    project, home, target, env, log = deployment
    oldenv = (target / "deploy/.env").read_bytes()
    key = project / "key with spaces"
    key.write_text("PRIVATE KEY TEST - NEVER UPLOAD")
    r = invoke(deployment, "-KeyPath", str(key))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (
        target / "deploy/.env"
    ).read_bytes() == oldenv + b"LEM_LATEST_CLIENT_VERSION=0.12.0\nLEM_MIN_CLIENT_VERSION=0.11.2\n"
    assert (target / "server/run.py").read_bytes() == (project / "server/run.py").read_bytes()
    assert not (target / "server/obsolete.py").exists()
    assert next((target / ".deploy-backups").glob("*/server/obsolete.py")).exists()
    assert next((target / "deploy/backups").glob("*.dump")).read_bytes().startswith(b"PGDMP")
    assert not any(c[:3] == ["docker", "volume", "rm"] for c in calls(deployment))
    assert (target / "server/run.py").stat().st_mode & 0o444 == 0o444
    assert (target / "deploy/backup.sh").stat().st_mode & 0o111 == 0o111
    assert (target / "deploy/.env").stat().st_mode & 0o077 == 0
    assert "TEST_ADMIN_PASSWORD" not in r.stdout + r.stderr
    assert not list((home / ".cache").glob("lem-deploy-*"))


def test_reset_removes_only_project_database(deployment):
    r = invoke(deployment, "-ResetDatabase")
    assert r.returncode == 0, r.stdout + r.stderr
    commands = calls(deployment)
    removed = [c for c in commands if c[:3] == ["docker", "volume", "rm"]]
    assert removed == [["docker", "volume", "rm", "lem_pgdata"]]
    assert not any("pg_dump" in c for c in commands)
    assert not any("down" in c for c in commands)
    assert (deployment[2] / "deploy/.env").exists()


def test_build_failure_does_not_stop_or_delete(deployment):
    r = invoke(deployment, "-ResetDatabase", envextra={"FAIL_BUILD": "1"})
    assert r.returncode != 0
    assert (deployment[2] / "server/obsolete.py").exists()
    assert any("build" in c for c in calls(deployment) if c[0] == "docker")
    assert not any(("stop" in c or "rm" in c) for c in calls(deployment) if c[0] == "docker")


def test_foreign_volume_refuses_reset(deployment):
    r = invoke(deployment, "-ResetDatabase", envextra={"FAIL_LABEL": "1"})
    assert r.returncode != 0
    assert any(c[:3] == ["docker", "volume", "inspect"] for c in calls(deployment))
    assert not any(c[:3] == ["docker", "volume", "rm"] for c in calls(deployment))
    assert (deployment[2] / "server/obsolete.py").exists()


def test_domain_mismatch_aborts_before_build(deployment):
    # An explicit target reaches the remote domain guard. Auto discovery filters
    # by domain earlier, before Compose inspection, and is covered separately.
    oldenv = (deployment[2] / "deploy/.env").read_bytes()
    r = invoke(deployment, "-RemotePath", "apps/LEM", "-Domain", "other.example.org")
    assert r.returncode != 0
    assert "Domain difere" in r.stdout + r.stderr
    assert any("config" in c for c in calls(deployment) if c[0] == "docker")
    assert not any(
        action in c
        for c in calls(deployment)
        if c[0] == "docker"
        for action in ("build", "stop", "rm")
    )
    assert (deployment[2] / "deploy/.env").read_bytes() == oldenv
    assert (deployment[2] / "server/obsolete.py").exists()


def test_auto_discovery_domain_mismatch_stops_before_docker(deployment):
    oldenv = (deployment[2] / "deploy/.env").read_bytes()
    r = invoke(deployment, "-Domain", "other.example.org")
    assert r.returncode != 0
    assert "auto discovery requires exactly one matching installation" in r.stderr
    assert not any(c[0] == "docker" for c in calls(deployment))
    assert (deployment[2] / "deploy/.env").read_bytes() == oldenv
    assert (deployment[2] / "server/obsolete.py").exists()


def test_windows_zip_separators_are_normalized(deployment):
    project = deployment[0]
    path = project / "deploy/deploy.ps1"
    script = path.read_text(encoding="utf-8-sig")
    marker = "            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile"
    assert marker in script
    script = script.replace(
        marker, '            $entryName = $entryName.Replace("/", "\\")\n' + marker
    )
    path.write_text(script, encoding="utf-8-sig")
    r = invoke(deployment, "-ResetDatabase")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (deployment[2] / "server/run.py").read_bytes() == (
        project / "server/run.py"
    ).read_bytes()
    assert any(c[:3] == ["docker", "volume", "rm"] for c in calls(deployment))


@pytest.mark.parametrize(
    "name",
    [
        r"server\..\..\escape",
        r"deploy\.env",
        "/server/escape",
        "server/../escape",
        "server\\run.py",
    ],
)
def test_unsafe_or_duplicate_zip_paths_stop_before_docker(deployment, name):
    project = deployment[0]
    path = project / "deploy/deploy.ps1"
    script = path.read_text(encoding="utf-8-sig")
    marker = "    } finally { $zip.Dispose() }"
    assert marker in script
    script = script.replace(marker, "        [void]$zip.CreateEntry('" + name + "')\n" + marker)
    path.write_text(script, encoding="utf-8-sig")
    r = invoke(deployment, "-ResetDatabase")
    assert r.returncode != 0
    assert not any(c[0] == "docker" for c in calls(deployment))
    assert (deployment[2] / "server/obsolete.py").exists()
