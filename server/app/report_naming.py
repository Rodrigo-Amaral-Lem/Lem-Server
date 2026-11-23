# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Public report identifiers; internal UUIDs remain stable for synchronization."""
import re
import unicodedata
from urllib.parse import quote

MAX_CHARGER_ID_BYTES = 72
MAX_CONTEXT_BYTES = 112
MAX_FILENAME_BYTES = 250

EVIDENCE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "video/mp4": "mp4",
    "video/3gpp": "3gp",
}


def component(value, fallback, maximum):
    text = unicodedata.normalize("NFC", str(value or ""))
    text = re.sub(r'[\s<>:"/\\|?*\x00-\x1f\x7f]+', "-", text).strip(" .-_")
    while len(text.encode("utf-8")) > maximum:
        text = text[:-1]
    return text.rstrip(" .-_") or fallback


def report_identifier(report):
    snapshot = report.get("snapshot") or {}
    charger_id = (snapshot.get("charger") or {}).get("external_id")
    charger = component(charger_id, "Carregador", MAX_CHARGER_ID_BYTES)
    reference = (snapshot.get("intervention") or {}).get("reference")
    if reference:
        return f"{charger}_{component(reference, 'Intervenção', MAX_CONTEXT_BYTES)}"
    contract_name = (snapshot.get("contract") or {}).get("name")
    return f"{charger}_{component(contract_name, 'Contrato', MAX_CONTEXT_BYTES)}"


def evidence_stem(identifier, evidence_id, extension):
    # The caller adds the extension. Reserve space for it and the full evidence ID.
    id_suffix = f"_{evidence_id}"
    suffix_bytes = len(f"{id_suffix}.{extension}".encode("utf-8"))
    prefix_budget = max(1, MAX_FILENAME_BYTES - suffix_bytes)
    return component(identifier, "Report", prefix_budget) + id_suffix


def content_disposition(identifier, extension, *, inline=False):
    suffix = "." + extension
    filename = identifier + suffix
    # The extended parameter keeps the original spelling. Older clients read the ASCII fallback.
    ascii_identifier = unicodedata.normalize("NFKD", identifier).encode("ascii", "ignore").decode("ascii")
    fallback = component(ascii_identifier, "Report", MAX_FILENAME_BYTES - len(suffix)) + suffix
    disposition = "inline" if inline else "attachment"
    encoded_filename = quote(filename, safe="")
    return (
        f'{disposition}; filename="{fallback}"; '
        f"filename*=UTF-8''{encoded_filename}"
    )
