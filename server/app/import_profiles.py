# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

# Built-in layouts describe field meaning, exact headers and which source cells are required.
# Profile IDs are persisted data and deliberately retain their original spelling.

"""Versioned source layouts; required columns are enforced by the server."""

import re
import unicodedata

STATIONS_PROFILE = "lem-stations-v1"
# column, field, source header, required
STATIONS_COLUMNS = [
    ("A", "external_id", "Station ID", True),
    ("B", "address", "Morada", True),
    ("C", "postal_code", "Código Postal", False),
    ("D", "latitude", "Latitude", False),
    ("E", "longitude", "Longitude", False),
    ("F", "region", "CCDR", True),
    ("G", "cpe", "CPE Associado", False),
    ("H", "energy_supply", "Fornecimento de Energia", False),
    ("I", "serial", "Nº(s) Série", False),
    ("J", "manufacturer", "Fabricante", True),
    ("K", "model", "Modelo", False),
    ("L", "power", "Potência (kVA)", False),
    ("M", "power_limit", "Limitação de Potência (kVA)", False),
    ("N", "connectors", "Tomadas / Fichas", True),
]
FIELDS = [
    "external_id",
    "operator",
    "manufacturer",
    "model",
    "serial",
    "address",
    "cpe",
    "power",
    "postal_code",
    "latitude",
    "longitude",
    "region",
    "energy_supply",
    "power_limit",
    "connectors",
]


def is_operator_header(label):
    return str(label).strip().casefold() in {"operator", "current operator", "operador", "operador atual"}


def is_contract_notes(key):
    return str(key).removeprefix("extra.").strip().casefold() in {"notas", "contract_notes"}


def normalize_contract_notes(value):
    """Preserve multiline text verbatim unless the whole cell is visually empty."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    if not any(c.isprintable() and not c.isspace() and unicodedata.category(c) not in {"Mn", "Me"}
               and c not in "\u115f\u1160\u2800\u3164\uffa0" for c in text):
        return ""
    if "\x00" in text or len(text) > 32767:
        raise ValueError("Notas inválidas: máximo de 32767 caracteres e sem caracteres nulos.")
    return text


# Older import profiles and old catalogues may contain both mapped fields
# and arbitrary Excel headers. Keep the postal address and coordinates.
def obsolete_station_field(key):
    extra = str(key).startswith("extra.")
    key = str(key).removeprefix("extra.").casefold().replace("_", " ")
    if extra and key in {"id", "external id", "station id", "id do pcve", "id de pcve", "identificador"}:
        return True
    return " ".join(key.split()) in {
        "location", "localização", "localizacao", "local", "nome do local",
        "site name", "localização / nome do local", "localizacao / nome do local",
        "site id", "id do site",
    }


def clean_station_details(details, *, repair_legacy_cedillas=False):
    result = {k: v for k, v in (details or {}).items()
              if not obsolete_station_field(k) and not is_contract_notes(k)}
    # Some Portuguese workbook exports contain Ý/ý where a cedilla precedes
    # a/o/u. Identifiers, serials and technical model codes are left verbatim.
    if repair_legacy_cedillas:
        for field in ("address", "region", "manufacturer", "energy_supply"):
            value = result.get(field)
            if isinstance(value, str) and ("Ý" in value or "ý" in value):
                value = re.sub(r"(?<=[a-zà-öø-ÿ])Ý(?=[aouãõáàâóôú])", "ç", value)
                result[field] = re.sub(r"([Ýý])(?=[aouãõáàâóôú])",
                                       lambda match: "Ç" if match[1] == "Ý" else "ç", value, flags=re.I)
    if isinstance(result.get("source"), dict):
        result["source"] = {k: v for k, v in result["source"].items()
                            if not obsolete_station_field(k) and not is_contract_notes(k)}
    return result


def is_stations_header(header):
    return any(str(h).strip().casefold() == "station id" for h in header)


def recommended(header, sheet, row):
    if not is_stations_header(header):
        return None
    columns = {field: label for _, field, label, _ in STATIONS_COLUMNS}
    for label in header:
        if is_contract_notes(label):
            columns["contract_notes"] = label
        elif is_operator_header(label):
            columns["operator"] = label
    return {
        "profile": STATIONS_PROFILE,
        "sheet": sheet,
        "header_row": row,
        "columns": columns,
        "required_fields": [field for _, field, _, required in STATIONS_COLUMNS if required],
    }
