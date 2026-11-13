# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

# Import pipeline: bounded workbook -> sheet/header preview -> validated records.
# No macro execution or formula calculation; data_only reads cached cell values.

"""Deterministic, explicit XLSX mappings. No macros or formula evaluation."""

from io import BytesIO
from zipfile import ZipFile, BadZipFile
import openpyxl
from fastapi import HTTPException
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import iterparse as safe_iterparse
from xml.etree.ElementTree import ParseError
from openpyxl.xml import DEFUSEDXML

if not DEFUSEDXML:
    raise RuntimeError("Excel imports require defusedxml protection")

from .import_profiles import (
    FIELDS,
    STATIONS_COLUMNS,
    STATIONS_PROFILE,
    is_stations_header,
    recommended,
)


# Bound archive expansion and row counts before reading cell data.
def workbook(data):
    try:
        with ZipFile(BytesIO(data)) as z:
            if sum(x.file_size for x in z.infolist()) > 100 * 1024 * 1024:
                raise ValueError("Ficheiro descomprimido demasiado grande")
            if len(z.infolist()) > 2000:
                raise ValueError("Demasiadas entradas no Excel")
            # Validate OOXML before openpyxl chooses lxml/ElementTree internally.
            # Streaming + clear avoids constructing a second full XML tree.
            for entry in z.infolist():
                if entry.filename.lower().endswith((".xml", ".rels")):
                    with z.open(entry) as part:
                        for _, element in safe_iterparse(part, events=("end",), forbid_dtd=True):
                            element.clear()
        w = openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=True)
        if sum(s.max_row or 0 for s in w) > 100000:
            w.close()
            raise ValueError("Máximo 100000 linhas")
        if (
            any((s.max_column or 0) > 256 for s in w)
            or sum((s.max_row or 0) * (s.max_column or 0) for s in w) > 2000000
        ):
            w.close()
            raise ValueError("Máximo 256 colunas e 2000000 células")
        return w
    except (BadZipFile, ValueError, KeyError, OSError, DefusedXmlException, ParseError) as e:
        raise HTTPException(422, f"Excel inválido: {e}")


def analyse(data):
    w = workbook(data)
    try:
        sheets = []
        for s in w:
            # Workbooks not written by Excel may omit <dimension>; max_row/max_column
            # are then None. Read the normal preview window, not a single cell.
            sample = [
                [str(c) if c is not None else "" for c in row]
                for row in s.iter_rows(
                    min_row=1,
                    max_row=min(12, s.max_row or 12),
                    max_col=min(60, s.max_column or 60),
                    values_only=True,
                )
            ]
            if s.max_row is None or s.max_column is None:
                # Without declared bounds rows are padded: trim the unused tail.
                width = max((i + 1 for row in sample for i, c in enumerate(row) if c), default=1)
                sample = [row[:width] for row in sample]
                while len(sample) > 1 and not any(sample[-1]):
                    sample.pop()
            profile = None
            for row, header in enumerate(sample, 1):
                profile = recommended(header, s.title, row)
                if profile:
                    break
            sheets.append(
                {
                    "name": s.title,
                    "rows": s.max_row,
                    "sample": sample,
                    "recommended_mapping": profile,
                }
            )
        return {"sheets": sheets, "fields": FIELDS}
    except (DefusedXmlException, ParseError, ValueError, TypeError) as e:
        raise HTTPException(422, "Excel inválido") from e
    finally:
        w.close()


# Preserve leading zeroes for numeric identifiers with simple Excel zero masks.
def cell_text(cell, identifier=False):
    value = cell.value
    if value is None:
        return ""
    if identifier and isinstance(value, (int, float)) and not isinstance(value, bool):
        mask = cell.number_format or ""
        if set(mask) == {"0"} and len(mask) <= 32 and float(value).is_integer():
            return str(int(value)).zfill(len(mask))
        if float(value).is_integer():
            return str(int(value))
    return str(value).strip()


# Resolve explicit headers, collect row/cell errors, and return records without writing the DB.
def parse(data, mapping):
    w = workbook(data)
    try:
        sheet = mapping.get("sheet")
        if sheet not in w.sheetnames:
            raise HTTPException(422, "Folha não encontrada")
        raw_row = mapping.get("header_row", 1)
        # Do not silently truncate fractions or accept booleans as row numbers.
        if isinstance(raw_row, bool) or not isinstance(raw_row, (int, str)):
            raise HTTPException(422, "Linha de cabeçalho deve ser um número inteiro de 1 a 100")
        row = int(raw_row)
        if not 1 <= row <= 100:
            raise HTTPException(422, "Linha de cabeçalho deve ser 1 a 100")
        s = w[sheet]
        header = [
            str(c).strip() if c is not None else ""
            for c in next(
                s.iter_rows(
                    min_row=row,
                    max_row=row,
                    max_col=min(s.max_column or 256, 256),
                    values_only=True,
                ),
                (),
            )
        ]
        if not header or not any(header):
            raise HTTPException(422, "Linha de cabeçalho vazia ou fora dos dados da folha")
        standard = is_stations_header(header) or mapping.get("profile") == STATIONS_PROFILE
        columns = mapping.get("columns", {})
        if not isinstance(columns, dict):
            raise HTTPException(422, "Mapeamento de colunas inválido")
        if standard:
            # Validate the supplied layout, including optional column headers.
            # The caller cannot bypass mandatory checks by hiding/remapping a field.
            for col, field, label, required in STATIONS_COLUMNS:
                index = ord(col) - ord("A")
                if index >= len(header) or header[index] != label:
                    raise HTTPException(
                        422, f'Formato Stations: a coluna {col} deve ter o cabeçalho "{label}"'
                    )
                if columns.get(field) and columns[field] != label:
                    raise HTTPException(422, f"Formato Stations: {label} deve usar a coluna {col}")
            # A standard template has a deterministic mapping; import every field,
            # while only the user-designated ten fields require populated cells.
            columns = {field: label for _, field, label, _ in STATIONS_COLUMNS}

        if not columns.get("external_id"):
            raise HTTPException(422, "Mapeie external_id")
        indexes = {}
        for key, label in columns.items():
            if not label:
                continue
            if header.count(label) != 1:
                raise HTTPException(422, f"Cabeçalho inexistente ou ambíguo: {label}")
            if key not in FIELDS and not key.startswith("extra."):
                raise HTTPException(422, f"Atributo não permitido: {key}")
            indexes[key] = header.index(label)
        records = []
        seen = set()
        errors = []
        for num, values in enumerate(
            s.iter_rows(
                min_row=row + 1,
                max_row=min(s.max_row or 100001, 100001),
                max_col=min(s.max_column or 256, 256),
                values_only=False,
            ),
            row + 1,
        ):
            if num > 100000 or num * min(s.max_column or 256, 256) > 2000000:
                raise HTTPException(422, "Excel excede limites de linhas/células")
            # Ignore formatted but empty rows and annotations outside the data table.
            relevant = values[:14] if standard else values
            if not any(c.value is not None and str(c.value).strip() for c in relevant):
                continue
            rec = {
                key: (
                    cell_text(values[i], key in ["external_id", "serial", "cpe", "site_id"])
                    if i < len(values)
                    else ""
                )
                for key, i in indexes.items()
            }
            ext = rec.pop("external_id", "")
            missing = []
            if standard:
                for col, field, label, required in STATIONS_COLUMNS:
                    value = ext if field == "external_id" else rec.get(field, "")
                    if required and not value.strip():
                        cell = f"{col}{num}"
                        missing.append(
                            {
                                "row": num,
                                "column": col,
                                "cell": cell,
                                "field": field,
                                "label": label,
                                "id": ext,
                                "error": f"Atributo obrigatório vazio: {label} ({cell})",
                            }
                        )
                errors.extend(missing)
                if missing:
                    continue
                rec["power_unit"] = "kVA"
                rec["import_profile"] = STATIONS_PROFILE
            if not ext or len(ext) > 200 or ext in seen:
                errors.append(
                    {"row": num, "error": "ID vazio, demasiado longo ou duplicado", "id": ext}
                )
                continue
            seen.add(ext)
            rec["source"] = {
                label: cell_text(values[i]) if i < len(values) else ""
                for i, label in enumerate(header)
                if label
            }
            records.append({"external_id": ext, "details": rec})
        return {
            "records": records,
            "errors": errors,
            "count": len(records),
            "profile": STATIONS_PROFILE if standard else None,
        }
    except (DefusedXmlException, ParseError, ValueError, TypeError) as e:
        raise HTTPException(422, "Excel inválido") from e
    finally:
        w.close()
