# Internal Python API: resolve_target(remote_path, home, domain=""). There is no standalone CLI.
# Reads candidate installation markers; explicit paths or unambiguous ~/apps discovery select the target.
# Raises ValueError for ambiguous/missing auto targets rather than selecting by historical folder name.

# Resolve the target from installation files and optionally its configured domain.
# Ambiguity is an error; folder names are not a reliable identity for deployed data.

"""Find an existing installation without depending on its historical folder name."""

from pathlib import Path


def resolve_target(remote_path, home, domain=""):
    home = Path(home)
    if remote_path != "auto":
        path = Path(remote_path).expanduser()
        return path if path.is_absolute() else home / path
    candidates = []
    for root in (home / "apps").glob("*"):
        if root.is_symlink():
            continue
        if not all(
            (root / p).is_file()
            for p in (
                "deploy/.env",
                "deploy/compose.yml",
                "server/run.py",
                "shared/checklist-v1.json",
            )
        ):
            continue
        if domain:
            import runpy

            reader = runpy.run_path(str(Path(__file__).with_name("prepare-config.py")))[
                "domain_from_env"
            ]
            try:
                if reader(root / "deploy/.env") != domain:
                    continue
            except (OSError, ValueError):
                continue
        candidates.append(root)
    if len(candidates) != 1:
        raise ValueError(
            "Indique --remote-path (Linux) ou -RemotePath (PowerShell): auto discovery requires exactly one matching installation under ~/apps."
        )
    return candidates[0]
