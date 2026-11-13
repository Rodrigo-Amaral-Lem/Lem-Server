# Runs on the remote host after SSH transfer. Validate archive bytes and every path
# before extraction, then locate the installation and hand off to remote-deploy.sh.

"""Internal SSH bootstrap used by deploy.sh; no external Python packages."""

import base64, hashlib, json, os, shutil, stat, sys, zipfile
from pathlib import Path, PurePosixPath

c = json.loads(base64.b64decode(sys.argv[1], validate=True))
import re

if not re.fullmatch(r"[a-f0-9]{32}", c["release"]):
    raise SystemExit("Release inválida")
if c["transfer_dir"] != ".cache/lem-deploy-" + c["release"]:
    raise SystemExit("Pasta temporária inválida")
staging = Path.home() / c["transfer_dir"]
archive = staging / "payload.zip"
if hashlib.sha256(archive.read_bytes()).hexdigest() != c["archive_sha"]:
    raise SystemExit("SHA256 do pacote nao corresponde; deploy cancelado")
source = staging / "source"
source.mkdir(mode=0o700)
with zipfile.ZipFile(archive) as z:
    if sum(i.file_size for i in z.infolist()) > 512 * 1024 * 1024:
        raise SystemExit("Pacote demasiado grande")
    entries = []
    seen = set()
    for i in z.infolist():
        # Accept historical Windows ZIP separators only after normalization;
        # traversal, absolute paths, symlinks and private files remain forbidden.
        name = i.filename.replace("\\", "/")
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or not p.parts:
            raise SystemExit("Caminho inseguro no pacote: " + repr(i.filename))
        if p.parts[0] not in ("server", "shared", "deploy", ".dockerignore"):
            raise SystemExit("Ficheiro fora das pastas de deployment: " + repr(i.filename))
        if p.parts[0] == ".dockerignore" and len(p.parts) != 1:
            raise SystemExit("Caminho invalido para .dockerignore")
        if ".env" in p.parts or "backups" in p.parts or stat.S_ISLNK(i.external_attr >> 16):
            raise SystemExit("Configuracao privada/link nao permitido no pacote")
        canonical = p.as_posix()
        if canonical in seen:
            raise SystemExit("Caminho duplicado no pacote: " + canonical)
        seen.add(canonical)
        entries.append((i, p, name.endswith("/")))
    # Do not call extractall on the original Windows names. Extract the exact
    # normalized and validated paths used above.
    for info, path, is_dir in entries:
        destination = source.joinpath(*path.parts)
        if is_dir:
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, destination.open("xb") as dst:
                shutil.copyfileobj(src, dst)
# ZIP extraction does not retain Unix modes. Set runtime-readable source modes
# for Docker COPY + non-root API, while the transfer parent remains private.
for path in source.rglob("*"):
    path.chmod(0o755 if path.is_dir() or path.suffix == ".sh" else 0o644)
import runpy

resolver = runpy.run_path(str(source / "deploy/find-installation.py"))["resolve_target"]
target = resolver(c["remote_path"], Path.home(), c["domain"])
os.execv(
    "/bin/bash",
    [
        "bash",
        str(source / "deploy/remote-deploy.sh"),
        str(target),
        "1" if c["reset"] else "0",
        c["release"],
        c["domain"],
    ],
)
