# Deployment packaging boundary: include only server/shared/deploy and .dockerignore.
# The distributable app/source archive is broader; this helper deliberately excludes client files.

"""Build a credential-free server payload; Python standard library only."""

import hashlib, re, sys, zipfile
from pathlib import Path

EXCLUDED = {
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "venv",
    "build",
    "backups",
    ".deploy-backups",
    ".git",
    "node_modules",
    "secrets",
    ".ssh",
}
SUFFIXES = {".pyc", ".db", ".dump", ".pem", ".key", ".pfx", ".p12", ".zip", ".tar", ".gz"}


def package(project, output):
    project = Path(project).resolve()
    output = Path(output).resolve()
    files = []
    for folder in ("server", "shared", "deploy"):
        if (project / folder).is_symlink():
            raise ValueError("Pasta simbólica não permitida: " + folder)
        for file in sorted((project / folder).rglob("*")):
            relative = file.relative_to(project)
            if EXCLUDED.intersection(relative.parts):
                continue
            if file.name == ".env" or (
                file.name.startswith(".env.") and file.name != ".env.example"
            ):
                continue
            if file.suffix.lower() in SUFFIXES or re.search(
                r"\.(db|sqlite|sqlite3)(-|$)", file.name, re.I
            ):
                continue
            if file.is_symlink():
                raise ValueError("Link simbólico não permitido: " + str(relative))
            if file.is_file():
                files.append(file)
    ignore = project / ".dockerignore"
    if ignore.is_symlink():
        raise ValueError("Link simbólico não permitido: .dockerignore")
    if ignore.is_file():
        files.append(ignore)
    if output in files:
        raise ValueError("O destino não pode substituir um ficheiro do projeto")
    with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            name = file.relative_to(project).as_posix()
            if "\\" in name:
                raise ValueError("Nome de ficheiro com separador inválido: " + name)
            content = file.read_bytes()
            if file.suffix == ".sh":
                content = (
                    content.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").encode()
                )
            entry = zipfile.ZipInfo(name)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = (0o100755 if file.suffix == ".sh" else 0o100644) << 16
            archive.writestr(entry, content)
    return hashlib.sha256(output.read_bytes()).hexdigest()


if __name__ == "__main__":
    try:
        print(package(*sys.argv[1:]))
    except (OSError, ValueError, TypeError) as error:
        raise SystemExit("Erro a criar pacote: " + str(error))
