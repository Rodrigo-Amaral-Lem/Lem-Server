# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

# ZIP export helpers kept free of FastAPI/SQLAlchemy so they can be tested alone.
# The archive is written to a spooled temporary file one photograph at a time:
# memory use no longer grows with the total size of a report's photographs.

from shutil import copyfileobj
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED, ZIP_STORED
import json
from .report_naming import EVIDENCE_EXTENSIONS, evidence_stem, report_identifier

CHUNK = 1024 * 1024
ZIP_COPY_BLOCK = 256 * 1024


class MissingEvidence(ValueError):
    """An export cannot claim completeness when an original is unavailable."""


def write_bundle(target, doc, photos, pdf_bytes):
    """Write the named PDF, JSON and original photographs into target.

    photos need id, checkpoint, mime and data; data may be loaded lazily and may
    not be None: unavailable originals abort the entire export.
    Returns the archive size and leaves target positioned at its start.
    """
    with ZipFile(target, "w", ZIP_DEFLATED) as z:
        # Qt already compresses PDF images/fonts; avoid compressing them again.
        identifier = report_identifier(doc)
        z.writestr(identifier + ".pdf", pdf_bytes, compress_type=ZIP_STORED)
        z.writestr(identifier + ".json", json.dumps(doc, ensure_ascii=False, indent=2))
        for p in photos:
            ext = EVIDENCE_EXTENSIONS[p.mime]
            folder = "videos" if p.mime.startswith("video/") else "fotografias"
            name = f"{folder}/{p.checkpoint}/{evidence_stem(identifier, p.id, ext)}.{ext}"
            source = getattr(p, "source_path", None)
            # Reuse PDF staging in bounded blocks; JPEG/PNG/video are already compressed.
            if source is not None:
                info = ZipInfo.from_file(source, name)
                info.compress_type = ZIP_STORED
                # ZipFile.write copies in 8 KiB blocks. Larger buffered copies reduce
                # local read/write calls while keeping one bounded block in memory.
                with open(source, "rb") as original, z.open(info, "w") as entry:
                    copyfileobj(original, entry, ZIP_COPY_BLOCK)
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
