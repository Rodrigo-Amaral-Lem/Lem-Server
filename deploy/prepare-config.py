# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

# Internal config writer used by remote deployment; see deploy/README.md.
# Preserves existing secrets/domain and selected TLS-volume identities, writing a new private output file.
# CLI --domain ENV_FILE prints only the configured domain. Never log full input/output .env contents.

# Prepare a private replacement .env while keeping configured secrets and HTTPS volumes.
# Only selected deployment identifiers are replaced; the file contents must not be logged.

"""Normalise an existing deployment configuration without exposing credentials."""

from pathlib import Path
import json, re, sys, runpy


def read_env(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def domain_from_env(path):
    values = read_env(path)
    # Older releases used a different prefix; accept a single unambiguous domain.
    domains = {v for k, v in values.items() if k.endswith("_DOMAIN") and v}
    if len(domains) != 1:
        raise ValueError("Domínio ausente ou ambíguo no .env existente")
    domain = next(iter(domains))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}", domain):
        raise ValueError("Domínio inválido")
    return domain


def prepare(old_env, metadata_file, output):
    domain = domain_from_env(old_env)
    metadata = json.loads(Path(metadata_file).read_text())
    replacements = {"LEM_DOMAIN": domain, "COMPOSE_PROJECT_NAME": "lem"}
    values = read_env(old_env)
    # Promote the shipped compatibility bounds, including measurement validation changes.
    release = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "server/app/compatibility.py")
    )
    parse = release["version_tuple"]
    configured = values.get("LEM_LATEST_CLIENT_VERSION", release["VERSION"])
    minimum = values.get("LEM_MIN_CLIENT_VERSION", release["MINIMUM_CLIENT"])
    if parse(configured) is None or parse(minimum) is None:
        raise ValueError("Versão de cliente inválida no .env")
    newest = max((configured, release["VERSION"]), key=parse)
    minimum = max((minimum, release["MINIMUM_CLIENT"]), key=parse)
    if parse(minimum) > parse(newest):
        raise ValueError("A versão mínima excede a última versão suportada")
    replacements["LEM_LATEST_CLIENT_VERSION"] = newest
    replacements["LEM_MIN_CLIENT_VERSION"] = minimum
    for key, volume in [
        ("LEM_TLS_DATA_VOLUME", "caddydata"),
        ("LEM_TLS_CONFIG_VOLUME", "caddyconfig"),
    ]:
        name = metadata["volumes"][volume]["name"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ValueError("Nome de volume HTTPS inválido")
        replacements[key] = name
    lines = Path(old_env).read_text().splitlines()
    result = []
    written = set()
    for line in lines:
        key = (
            line.split("=", 1)[0].strip()
            if "=" in line and not line.lstrip().startswith("#")
            else ""
        )
        if key.endswith("_DOMAIN") and key != "LEM_DOMAIN":
            continue
        if key in replacements:
            if key not in written:
                result.append(key + "=" + replacements[key])
                written.add(key)
        else:
            result.append(line)
    result.extend(k + "=" + v for k, v in replacements.items() if k not in written)
    with Path(output).open("x") as f:
        f.write("\n".join(result) + "\n")
    Path(output).chmod(0o600)


if __name__ == "__main__":
    try:
        if sys.argv[1] == "--domain":
            print(domain_from_env(sys.argv[2]))
        else:
            prepare(*sys.argv[1:])
    except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
        raise SystemExit("Configuração LEM inválida: " + str(e))
