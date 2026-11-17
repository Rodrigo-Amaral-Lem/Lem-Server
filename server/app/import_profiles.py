# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

# Built-in layouts describe field meaning, exact headers and which source cells are required.
# Profile IDs are persisted data and deliberately retain their original spelling.

"""Versioned source layouts; required columns are enforced by the server."""

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
    "manufacturer",
    "model",
    "serial",
    "location",
    "address",
    "cpe",
    "power",
    "site_id",
    "postal_code",
    "latitude",
    "longitude",
    "region",
    "energy_supply",
    "power_limit",
    "connectors",
]


def is_stations_header(header):
    return any(str(h).strip().casefold() == "station id" for h in header)


def recommended(header, sheet, row):
    if not is_stations_header(header):
        return None
    return {
        "profile": STATIONS_PROFILE,
        "sheet": sheet,
        "header_row": row,
        "columns": {field: label for _, field, label, _ in STATIONS_COLUMNS},
        "required_fields": [field for _, field, _, required in STATIONS_COLUMNS if required],
    }
