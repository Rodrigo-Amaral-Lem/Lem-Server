# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

# ZIP export helpers kept free of FastAPI/SQLAlchemy so they can be tested alone.
# The archive is written to a spooled temporary file one photograph at a time:
# memory use no longer grows with the total size of a report's photographs.

from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED
import json
from .report_naming import report_identifier

CHUNK = 1024 * 1024


class MissingEvidence(ValueError):
    """An export cannot claim completeness when an original is unavailable."""


def write_bundle(target, doc, photos, pdf_bytes):
    """Write the named PDF, report.json and original photographs into target.

    photos need id, checkpoint, mime and data; data may be loaded lazily and may
    not be None: unavailable originals abort the entire export.
    Returns the archive size and leaves target positioned at its start.
    """
    with ZipFile(target, "w", ZIP_DEFLATED) as z:
        # Qt already compresses PDF images/fonts; avoid compressing them again.
        z.writestr(report_identifier(doc) + ".pdf", pdf_bytes, compress_type=ZIP_STORED)
        z.writestr("report.json", json.dumps(doc, ensure_ascii=False, indent=2))
        for p in photos:
            ext = {"image/jpeg": "jpg", "image/png": "png", "video/mp4": "mp4", "video/3gpp": "3gp"}[p.mime]
            folder = "videos" if p.mime.startswith("video/") else "fotografias"
            name = f"{folder}/{p.checkpoint}/{p.id}.{ext}"
            source = getattr(p, "source_path", None)
            # Reuse PDF staging in bounded blocks; JPEG/PNG/video are already compressed.
            if source is not None:
                z.write(source, name, compress_type=ZIP_STORED)
            else:
                data = p.data
                if data is None:
                    raise MissingEvidence("Fotografia em falta durante a exportação; tente novamente")
                z.writestr(name, data, compress_type=ZIP_STORED)
    size = target.tell()
    target.seek(0)
    return size


def file_chunks(source):
    """Yield a file in bounded blocks and always close it (also on disconnect)."""
    try:
        while True:
            block = source.read(CHUNK)
            if not block:
                break
            yield block
    finally:
        source.close()
