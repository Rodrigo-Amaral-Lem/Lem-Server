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
from .checklist import connector_text

if not DEFUSEDXML:
    raise RuntimeError("Excel imports require defusedxml protection")

from .import_profiles import (
    FIELDS,
    obsolete_station_field,
    clean_station_details,
    is_contract_notes,
    is_operator_header,
    normalize_contract_notes,
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
        return {"sheets": sheets, "fields": [*FIELDS, "contract_notes"]}
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
def parse(data, mapping, *, contract_notes=True):
    w = workbook(data)
    formulas = None
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
        # Reserve the optional membership field before the Stations profile
        # rebuilds its fixed A–N mapping. Global imports must exclude it too.
        note_headers = {label for label in header if is_contract_notes(label)}
        for key, label in columns.items():
            if is_contract_notes(key) and label:
                if not isinstance(label, str) or label not in header:
                    raise HTTPException(422, f"Cabeçalho de Notas inexistente: {label}")
                note_headers.add(label)
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
            # while only the fields marked required need populated cells.
            operator_headers = [label for label in header if is_operator_header(label)]
            if len(operator_headers) > 1:
                raise HTTPException(422, "Cabeçalho de operador repetido")
            columns = {field: label for _, field, label, _ in STATIONS_COLUMNS}
            if operator_headers:
                columns["operator"] = operator_headers[0]

        if not columns.get("external_id"):
            raise HTTPException(422, "Mapeie external_id")
        # Reserve the notes column even for a legacy custom mapping such as
        # extra.Notas. Its contents must never enter the physical station or source JSON.
        note_indexes = [i for i, label in enumerate(header) if label in note_headers]
        if len(note_indexes) > 1:
            raise HTTPException(422, "Cabeçalho de Notas ambíguo")
        note_index = next(iter(note_indexes), None)
        indexes = {}
        ignored_headers = {label for key, label in columns.items() if obsolete_station_field(key) and isinstance(label, str)}
        for key, label in columns.items():
            if is_contract_notes(key):
                continue
            if label and label in note_headers:
                raise HTTPException(422, "A coluna de Notas é exclusiva do ID no contrato")
            if not label or obsolete_station_field(key) or obsolete_station_field(label):
                continue
            if header.count(label) != 1:
                raise HTTPException(422, f"Cabeçalho inexistente ou ambíguo: {label}")
            if key not in FIELDS and not key.startswith("extra."):
                raise HTTPException(422, f"Atributo não permitido: {key}")
            indexes[key] = header.index(label)
        records = []
        seen = set()
        errors = []
        if contract_notes and note_index is not None:
            # Read formula metadata alongside cached results from the same
            # validated bytes. Never evaluate formulas or mistake a missing
            # cached result for the administrator deliberately clearing a note.
            formulas = openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=False)
            note_cells = formulas[sheet].iter_rows(min_row=row + 1, max_row=min(s.max_row or 100001, 100001),
                                                   min_col=note_index + 1, max_col=note_index + 1)
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
            note = ""
            if formulas is not None:
                formula_cell, = next(note_cells)
                value_cell = values[note_index] if note_index < len(values) else None
                raw = value_cell.value if value_cell is not None else None
                if formula_cell.data_type == "f" and (raw is None or value_cell.data_type == "e"):
                    cell = formula_cell.coordinate
                    errors.append({"row": num, "cell": cell, "field": "contract_notes",
                                   "error": f"{cell}: recalcule a fórmula de Notas e guarde o Excel."})
                    continue
                note = normalize_contract_notes("" if raw is None else str(raw))
            if not note and not any(c.value is not None and str(c.value).strip()
                                    for i, c in enumerate(relevant) if i != note_index):
                continue
            rec = {
                key: (
                    cell_text(values[i], key in ["external_id", "serial", "cpe"])
                    if i < len(values)
                    else ""
                )
                for key, i in indexes.items()
            }
            ext = rec.pop("external_id", "")
            # Canonical display terminology; source below retains the original cell.
            if "connectors" in rec:
                rec["connectors"] = connector_text(rec["connectors"])
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
                if label and label not in ignored_headers and label not in note_headers and not obsolete_station_field(label)
            }
            rec = clean_station_details(rec)
            # Preview the same spelling that the live station will display, even
            # when a custom column name supplied the postal address.
            for field in ("address", "region", "manufacturer", "energy_supply"):
                if field in indexes:
                    rec["source"][header[indexes[field]]] = rec[field]
            record = {"external_id": ext, "details": rec}
            if contract_notes and note_index is not None:
                record["contract_notes"] = note
            records.append(record)
        return {
            "records": records,
            "errors": errors,
            "count": len(records),
            "profile": STATIONS_PROFILE if standard else None,
        }
    except (DefusedXmlException, ParseError, ValueError, TypeError) as e:
        raise HTTPException(422, "Excel inválido") from e
    finally:
        if formulas is not None:
            formulas.close()
        w.close()
