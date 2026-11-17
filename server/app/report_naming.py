# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Public report identifiers; internal UUIDs remain stable for synchronization."""
from datetime import datetime
import re
import unicodedata
from urllib.parse import quote
from zoneinfo import ZoneInfo


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
    if report.get("status") != "closed":
        return f"{charger}_Rascunho_{contract}"
    stamp = "sem-data_sem-hora"
    for value in (report.get("finished"), (report.get("payload") or {}).get("finished")):
        if not value:
            continue
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None:
                continue
            stamp = date.astimezone(ZoneInfo("Europe/Lisbon")).strftime("%Y-%m-%d_%H-%M-%S")
            break
        except (ValueError, TypeError, AttributeError):
            continue
    return f"{charger}_{stamp}_{contract}"


def content_disposition(identifier, extension):
    filename = identifier + "." + extension
    fallback = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename, safe="")}'
