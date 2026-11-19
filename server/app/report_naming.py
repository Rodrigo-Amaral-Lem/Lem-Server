# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Public report identifiers; internal UUIDs remain stable for synchronization."""
import re
import unicodedata
from urllib.parse import quote

EVIDENCE_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "video/mp4": "mp4", "video/3gpp": "3gp"}


def component(value, fallback, maximum):
    text = unicodedata.normalize("NFC", str(value or ""))
    text = re.sub(r'[\s<>:"/\\|?*\x00-\x1f\x7f]+', "-", text).strip(" .-_")
    while len(text.encode("utf-8")) > maximum:
        text = text[:-1]
    return text.rstrip(" .-_") or fallback


def report_identifier(report):
    snapshot = report.get("snapshot") or {}
    charger = component((snapshot.get("charger") or {}).get("external_id"), "Carregador", 72)
    contract = component((snapshot.get("contract") or {}).get("name"), "Contrato", 112)
    return f"{charger}_{contract}"


def evidence_stem(identifier, evidence_id, extension):
    # Keep the full stable evidence ID and respect the same byte limit as App.
    suffix = f"_{evidence_id}"
    limit = max(1, 250 - len(f"{suffix}.{extension}".encode("utf-8")))
    return component(identifier, "Report", limit) + suffix


def content_disposition(identifier, extension, *, inline=False):
    filename = identifier + "." + extension
    ascii_identifier = unicodedata.normalize("NFKD", identifier).encode("ascii", "ignore").decode("ascii")
    fallback = component(ascii_identifier, "Report", 249 - len(extension)) + "." + extension
    disposition = "inline" if inline else "attachment"
    return f'{disposition}; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename, safe="")}'
