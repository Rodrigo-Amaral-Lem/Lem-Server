# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Freeze one load-test question per physical connector when creating an intervention."""

from copy import deepcopy
from collections import Counter
import re

ALIASES = {
    "AC": "AC (tipo não indicado)",
    "T2": "Mennekes Type 2",
    "TYPE2": "Mennekes Type 2",
    "TIPO2": "Mennekes Type 2",
    "MENNEKES": "Mennekes Type 2",
    "MENNEKESTYPE2": "Mennekes Type 2",
    "MENNEKESTIPO2": "Mennekes Type 2",
    "CCS": "CCS",
    "CCS2": "CCS2",
    "CCSTYPE2": "CCS2",
    "CCSCOMBO2": "CCS2",
    "COMBO2": "CCS2",
    "CCS1": "CCS1",
    "CHADEMO": "CHAdeMO",
}


def connector_names(value):
    raw = value if isinstance(value, list) else [value]
    result = []
    for entry in raw:
        if not isinstance(entry, str) or entry.strip().upper() == "N/A":
            continue
        # Missing-value markers must not become fictional connectors N and A.
        entry = re.sub(r"\bN\s*/\s*A\b", "NA", entry, flags=re.I)
        for token in re.split(r"[,;|/\n]+", entry):
            text = " ".join(token.split())
            if not text or text.upper() in ("NA", "N/A", "NÃO INDICADO", "-"):
                continue
            count = 1
            prefix = re.fullmatch(r"(\d+)\s*[x×]\s*(.+)", text, re.I)
            suffix = re.fullmatch(r"(.+?)\s*[x×]\s*(\d+)", text, re.I)
            if prefix:
                count, text = int(prefix[1]), prefix[2].strip()
            elif suffix:
                text, count = suffix[1].strip(), int(suffix[2])
            if text.upper() in ("NA", "N/A", "NÃO INDICADO", "-"):
                continue
            if not 1 <= count <= 64 or len(result) + count > 64 or len(text) > 80:
                raise ValueError("Configuração de conectores inválida (máximo 64 conectores).")
            if not re.search(r"[A-Za-z]", text):
                raise ValueError("Tipo de conector inválido.")
            key = re.sub(r"[^A-Z0-9]", "", text.upper())
            text = ALIASES.get(key, text)
            # Repeated unknown types use the spelling of their first occurrence.
            text = next((x for x in result if x.casefold() == text.casefold()), text)
            result.extend([text] * count)
    return result


def for_charger(template, details):
    names = connector_names(details.get("connectors"))
    source = deepcopy(template)
    counts = Counter(names)
    seen = Counter()
    prefix = "CARG" if any(s["id"] == "CARG" for s in source["sections"]) else "PC03"
    prototype = next((i for s in source["sections"] if s["id"] == prefix
                      for i in s["items"] if i["id"].startswith(prefix + ".01.")), {})
    items = []
    for index, name in enumerate(names or ["tipo não indicado"], 1):
        seen[name] += 1
        suffix = f" #{seen[name]}" if counts[name] > 1 else ""
        items.append(
            dict(
                id=f"{prefix}.01.{index:02d}",
                label="Teste de carga do conector " + name + suffix,
                kind="status",
                photo_required=prototype.get("photo_required", False),
                na_notes_required=prototype.get("na_notes_required", False),
            )
        )
    for section in source["sections"]:
        if section["id"] == prefix:
            section["items"] = items + [
                i for i in section["items"] if not i["id"].startswith(prefix + ".01.")
            ]
    source["load_test_connectors"] = names
    return source


def corrected_draft_template(template, status):
    """Apply current v6 draft rules/order and optional notes, preserving closed history."""
    if status != "draft" or template.get("version") != 6:
        return template
    result = deepcopy(template)
    for section in result["sections"]:
        for item in section["items"]:
            if item["id"] in ("MAN.12", "MAN.18"):
                item["reading_required"] = True
            if item["id"] == "MAN.12":
                item.update(photo_required=True, photo_always=True)
            if item["id"] == "MAN.19":
                item["kind"] = "status"
    order = ['SEG','SITE','QVE','EST','MAN','CARG','FIN']
    result['sections'].sort(key=lambda s: order.index(s['id']) if s['id'] in order else len(order))
    for section in result['sections']:
        if section['id'] == 'SEG':
            for item in section['items']:
                if item['id'] in {f'SEG.{n:02d}' for n in range(2,7)}:
                    item.update(notes_disabled=True, media_disabled=True)
            if not any(i['id']=='SEG.07' for i in section['items']):
                section['items'].append(dict(id='SEG.07',label='Observações gerais de segurança',kind='notes',photo_required=False,optional=True))
    for group, key, label in [('QVE','QVE.14','QVE - Observações gerais'), ('MAN','MAN.21','Observações gerais da manutenção')]:
        for section in result['sections']:
            if section['id']==group and not any(i['id']==key for i in section['items']):
                section['items'].append(dict(id=key,label=label,kind='notes',photo_required=False,optional=True))
    return result
