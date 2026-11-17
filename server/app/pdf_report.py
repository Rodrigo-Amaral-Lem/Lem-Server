# SPDX-License-Identifier: GPL-3.0-only
"""Bounded, isolated Qt rendering with the same layout as the offline application."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from .exports import MissingEvidence
from .report_naming import report_identifier


def power_value(value):
    text = "" if value is None else str(value).strip()
    text = re.sub(r"\s*kVA\s*$", "", text, flags=re.IGNORECASE)
    return text + " kVA" if text else ""


def build_pdf(r, photos):
    # Each export gets private temporary paths; keep only one original in memory.
    # Worker isolation also places Qt GUI objects on their own process main thread.
    with TemporaryDirectory(prefix="lem-report-") as directory:
        root = Path(directory)
        report = dict(r, identifier=report_identifier(r), photos=[])
        for index, photo in enumerate(photos):
            path = getattr(photo, "source_path", None)
            if path is None:
                data = photo.data  # Propagate DB failures; never mask them as image errors.
                if not data:
                    raise MissingEvidence("Fotografia em falta durante a exportação; tente novamente")
                path = root / f"evidence-{index}"
                path.write_bytes(data)
                del data
            report["photos"].append({"id": getattr(photo, "id", ""), "checkpoint": photo.checkpoint,
                "mime": getattr(photo, "mime", "image/jpeg"), "path": path.as_uri()})
        manifest, output = root / "report.json", root / "report.pdf"
        manifest.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software", PYTHONUTF8="1")
        # An app/desktop Qt installation must not override the worker's PySide plugins.
        for key in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
            env.pop(key, None)
        command = [sys.executable, str(Path(__file__).with_name("pdf_worker.py")), str(manifest), str(output)]
        result = subprocess.run(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=180, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            raise RuntimeError("PDF worker failed: " + result.stderr.decode("utf-8", errors="replace")[-2000:])
        data = output.read_bytes()
        if not data.startswith(b"%PDF-"):
            raise RuntimeError("PDF worker returned an invalid document")
        # Same length-preserving Qt ToUnicode correction as native ReportPdf.cpp.
        return re.sub(rb"(?s)begincmap.*?endcmap", lambda m: m[0].replace(b"<0009>", b"<0020>").replace(b"<2126>", b"<03A9>"), data)
