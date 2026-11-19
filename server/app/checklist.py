# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Freeze one load-test question per physical connector when creating an intervention."""

from copy import deepcopy
from collections import Counter
import re

ALIASES = {
    "AC": "AC (tipo não indicado)",
    "T2": "AC Type 2",
    "ACTYPE2": "AC Type 2",
    "ACTIPO2": "AC Type 2",
    "TYPE2": "AC Type 2",
    "TIPO2": "AC Type 2",
    "MENNEKES": "AC Type 2",
    "MENNEKESTYPE2": "AC Type 2",
    "MENNEKESTIPO2": "AC Type 2",
    "CCS": "CCS",
    "CCS2": "CCS2",
    "CCSTYPE2": "CCS2",
    "CCSCOMBO2": "CCS2",
    "COMBO2": "CCS2",
    "CCS1": "CCS1",
    "CHADEMO": "CHAdeMO",
}


def connector_text(value):
    """Rename stored connector terminology without changing counts or other text."""
    return re.sub(r"\bMennekes\s+(?:Type|Tipo)\s*2\b", "AC Type 2", value, flags=re.I)


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
        # Inherit the complete prototype: newly added rules/help must apply to
        # every physical connector, without maintaining a second field allowlist.
        item = deepcopy(prototype)
        item.update(
            id=f"{prefix}.01.{index:02d}",
            label="Teste de carga do conector " + name + suffix,
            kind=prototype.get("kind", "status"),
            photo_required=prototype.get("photo_required", False),
            na_notes_required=prototype.get("na_notes_required", False),
            na_notes_default=prototype.get("na_notes_default", ""),
        )
        items.append(item)
    for section in source["sections"]:
        if section["id"] == prefix:
            section["items"] = items + [
                i for i in section["items"] if not i["id"].startswith(prefix + ".01.")
            ]
    source["load_test_connectors"] = names
    return source


def corrected_draft_template(template, status, *, upgrade=True):
    """Apply current v6 draft rules/order and optional notes, preserving closed history."""
    if status != "draft" or template.get("version") not in (6, 7):
        return template
    result = deepcopy(template)
    for section in result["sections"]:
        if section["id"] == "SITE":
            section["title"] = "SITE - Posto de carregamento"
        for item in section["items"]:
            if item["id"] in ("MAN.12", "MAN.18"):
                item["reading_required"] = True
            # Also remove the retired hint from templates already saved as v7.
            if item["id"] == "MAN.18":
                item.pop("help", None)
            if item["id"] == "MAN.12":
                item.update(photo_required=True, photo_always=False)
            if item["id"] == "SEG.01":
                item["na_notes_required"] = True
            if item["id"] == "MAN.02":
                item.update(kind="status", na_notes_required=False)
            if item["id"] == "MAN.19":
                item["kind"] = "status"
    current_rules = {'SITE.01': {'label': 'Verifique a limpeza geral do posto de carregamento'},
     'SITE.05': {'label': 'Verifique possíveis sinais de vandalismo (cabos cortados, fechadura forçada, carregador danificado, etc.)'},
     'SITE.02': {'na_notes_required': True, 'na_notes_default': 'Sem sinalização horizontal'},
     'SITE.03': {'na_notes_required': True, 'na_notes_default': 'Sem sinalética vertical'},
     'SITE.04': {'na_notes_required': True, 'na_notes_default': 'Sem proteção mecânica'},
     'QVE.02': {'na_notes_required': True,
                'na_notes_default': 'Não testado devido a veículo em carga',
                'label': 'Botoneira de emergência - Verifique o correto funcionamento'},
     'QVE.04': {'na_notes_required': True},
     'QVE.09': {'na_notes_required': True,
                'na_notes_default': 'Apertos não verificados devido a veículo em carga'},
     'QVE.10': {'na_notes_required': True, 'na_notes_default': 'QVE sem sinalética'},
     'QVE.11': {'na_notes_required': True, 'na_notes_default': 'Não testado devido a veículo em carga'},
     'QVE.12': {'na_notes_required': True, 'na_notes_default': 'Não testado devido a veículo em carga'},
     'EST.01': {'na_notes_required': True},
     'EST.16': {'na_notes_required': True},
     'EST.17': {'na_notes_required': True},
     'MAN.12': {'na_notes_required': True,
                'na_notes_default': 'Não testado devido a veículo em carga',
                'photo_always': False},
     'MAN.13': {'na_notes_required': True, 'na_notes_default': 'Não testado devido a veículo em carga'},
     'MAN.18': {'na_notes_required': True},
     'MAN.20': {'na_notes_required': True,
                'na_notes_default': 'Apertos não verificados devido a veículo em carga'},
     'MAN.02': {'na_notes_required': False},
     'FIN.03': {'kind': 'notes', 'optional': True, 'label': 'Observações gerais finais'},
     'FIN.02': {'signature_required': True, 'media_disabled': True, 'notes_visible': True},
     'QVE.01': {'label': 'Botoneira de emergência - Verifique integridade'},
     'QVE.03': {'na_notes_default': 'Botoneira sem dupla sinalização', 'label': 'Botoneira de emergência - Verifique a dupla sinalização'}}
    for section in result["sections"]:
        for item in section["items"]:
            item.update(current_rules.get(item["id"], {}))
            if item["id"] == "EST.15" and result["version"] == 7:
                item["label"] = item["label"].replace("(POS)", "(TPM)")
                if item.get("na_notes_default") == "Sem POS":
                    item["na_notes_default"] = "Sem TPM"
            # Apply to editable v6 drafts only; closed history keeps its saved rules.
            if "MAN.14" <= item["id"] <= "MAN.18":
                item.update(photo_required=True, photo_always=False, na_notes_required=True)
            if item["id"] in ("MAN.11", "MAN.12"):
                item["label"] = item["label"].replace("corrente", "intensidade")
            if item["id"].startswith("CARG.01."):
                item["na_notes_default"] = "Veículo usado não compatível"
    order = ['SEG','SITE','QVE','EST','MAN','CARG','FIN']
    result['sections'].sort(key=lambda s: order.index(s['id']) if s['id'] in order else len(order))
    for section in result['sections']:
        if section['id'] == 'SEG':
            for item in section['items']:
                if item['id'] in {f'SEG.{n:02d}' for n in range(2,7)}:
                    item.pop("notes_disabled", None)
                    item.update(notes_button_hidden=True, media_disabled=True)
                    if item["id"] in ("SEG.02", "SEG.04"):
                        item["na_notes_required"] = True
            if not any(i['id']=='SEG.07' for i in section['items']):
                section['items'].append(dict(id='SEG.07',label='Observações gerais de segurança',kind='notes',photo_required=False,optional=True))
    for group, key, label in [('CARG','CARG.07','Observações gerais dos ensaios de carregamento'), ('EST','EST.18','Observações gerais do equipamento'), ('SITE','SITE.06','Observações gerais do local'), ('QVE','QVE.14','QVE - Observações gerais'), ('MAN','MAN.22' if result.get('version') == 7 else 'MAN.21','Observações gerais da manutenção')]:
        for section in result['sections']:
            if section['id']==group and not any(i['id']==key for i in section['items']):
                section['items'].append(dict(id=key,label=label,kind='notes',photo_required=False,optional=True))
    from .maintenance_migration import upgrade_template
    return upgrade_template(result) if upgrade else result


def corrected_draft_payload(payload, template, status, *, upgrade=True):
    """Migrate editable legacy content; call with its ORIGINAL template/version."""
    if status != "draft" or template.get("version") not in (6, 7):
        return payload
    result = deepcopy(payload)
    answers = result.get("answers", {})
    if not isinstance(answers, dict):
        return result  # HTTP validation reports malformed input, not a migration error.
    answer = answers.get("FIN.03", {})
    value = answer.get("value", "") if isinstance(answer, dict) else ""
    if isinstance(value, str) and value and isinstance(answer.get("notes", ""), str):
        notes = answer.get("notes", "")
        answer["notes"] = value if not notes or notes == value else value + "\n" + notes
        answer["value"] = ""
    if upgrade and template.get("version") == 6:
        from .maintenance_migration import upgrade_payload
        result = upgrade_payload(result)
    return result
