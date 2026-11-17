# SPDX-License-Identifier: GPL-3.0-only
"""Single byte ranges for immutable evidence (RFC 9110, sections 13–15)."""
import re
from fastapi import HTTPException


def byte_range(value, size, if_range, etag):
    if not value or (if_range and if_range != etag):
        return None
    # Unsupported units/multipart requests may be served as a complete response.
    if not value.startswith("bytes=") or "," in value:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value) if len(value) < 128 else None
    if match:
        first, last = match.groups()
        if first:
            start = int(first)
            end = min(int(last), size - 1) if last else size - 1
        elif last and int(last) > 0:
            start, end = max(0, size - int(last)), size - 1
        else:
            start, end = size, -1
        if 0 <= start <= end < size:
            return start, end
    raise HTTPException(416, "Intervalo de evidência inválido", headers={"Content-Range": f"bytes */{size}"})
