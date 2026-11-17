# SPDX-License-Identifier: GPL-3.0-only
"""Upgrade editable v6 maintenance reports without dropping old notes/evidence.

Closed reports keep their saved template. A correction is an editable copy and
therefore upgrades normally; the source remains protected by the correction transaction.
"""
from copy import deepcopy
from decimal import Decimal
import re
from .maintenance_rules import apply_assessments

LABELS = {'EST.01': 'Fotografias do interior e exterior do carregador e sua limpeza antes da tarefa executada (visualizar todos os componentes internos e externos)', 'EST.13': 'Verifique se o tarifário está visível e em boas condições', 'EST.16': 'Verifique se os autocolantes da DGEG estão visíveis e em boas condições'}
AFTER_LABEL = "Fotografias do interior e exterior do carregador e sua limpeza depois da tarefa executada (visualizar todos os componentes internos e externos)"
CURRENT_HELP = "1 A = 1000 mA · 0,03 A = 30 mA"


def upgrade_checkpoint(key):
    return {"MAN.21": "MAN.22", "EST.10": "EST.18"}.get(key, key)


def upgrade_template(template):
    if template.get("version") != 6:
        return template
    result = deepcopy(template)
    result["version"] = 7
    for section in result["sections"]:
        section["items"] = [i for i in section["items"] if i["id"] != "EST.10"]
        for item in section["items"]:
            key = item["id"]
            if key in LABELS:
                item["label"] = LABELS[key]
            if key == "EST.15":
                item.update(na_notes_default="Sem POS", na_notes_required=True)
            if key in ("MAN.11", "MAN.12"):
                item.update(measurement_unit="mA", measurement_digits=6, measurement_decimals=0, help=CURRENT_HELP)
            if key == "MAN.18":
                item.pop("help", None)
            if key == "MAN.21":
                item["id"] = "MAN.22"
        if section["id"] == "MAN":
            section["items"].insert(len(section["items"]) - 1,
                dict(id="MAN.21", label=AFTER_LABEL, kind="status", photo_required=True, na_notes_required=True))
    return result


def upgrade_payload(payload):
    result = deepcopy(payload)
    answers = result.setdefault("answers", {})
    if "MAN.21" in answers:
        old = answers.pop("MAN.21")
        if "MAN.22" not in answers:
            answers["MAN.22"] = old
        elif answers["MAN.22"] != old:
            # Defensive collision handling: retain the complete former answer.
            result.setdefault("retired_answers", {})["MAN.21"] = old
            # Earlier clients stored null for untouched optional notes.
            existing = answers["MAN.22"] if isinstance(answers["MAN.22"], dict) else {"value": "", "notes": ""}
            old_text = old if isinstance(old, dict) else {}
            answers["MAN.22"] = existing
            texts = [existing.get("notes", ""), old_text.get("value", ""), old_text.get("notes", "")]
            existing["notes"] = "\n".join(dict.fromkeys(t for t in texts if t))
    if "EST.10" in answers:
        old = answers.pop("EST.10")
        result.setdefault("retired_answers", {})["EST.10"] = old
        if isinstance(old, dict) and old.get("notes", "").strip():
            general = answers.get("EST.18")
            if not isinstance(general, dict):
                general = {"value": "", "notes": ""}
                answers["EST.18"] = general
            texts = [general.get("notes", ""), old["notes"]]
            general["notes"] = "\n".join(dict.fromkeys(t for t in texts if t))
    for key in ("MAN.11", "MAN.12"):
        answer = answers.get(key)
        if not isinstance(answer, dict):
            continue
        raw = answer.get("value", "").strip()
        from .measurements import not_applicable
        if not raw or not_applicable(raw):
            continue
        # Never guess whether a unitless legacy "1" means 1 A or 1 mA.
        match = re.fullmatch(r"([0-9]{1,9}(?:[.,][0-9]{1,6})?)\s*(mA|A)", raw, re.I)
        if match:
            value = Decimal(match[1].replace(",", ".")) * (1000 if match[2].lower() == "a" else 1)
            if value == value.to_integral_value() and 0 <= value <= 999999:
                answer["value"] = f"{int(value)} mA"
                continue
        answer["previous_value"] = answer.get("value", "")
        answer["value"] = ""
        answer.pop("assessment", None)
        answer.pop("auto_assessment", None)
    result["answers"] = apply_assessments(answers, offer_suggestions=True)
    return result


def persist_upgrade(db, report, current_template=None, *, correction=False):
    """Called under the report's write lock; metadata and template move atomically."""
    if (report.status == "draft" and report.template.get("version", 0) < 6
            and current_template is not None):
        from sqlalchemy import select, update
        from .models import Photo, ReportCorrection
        from .legacy_correction import project
        editing = db.get(ReportCorrection, report.id)
        if correction or (editing and editing.state == "editing"):
            photos = [{"checkpoint": key} for key in db.scalars(
                select(Photo.checkpoint).where(Photo.report_id == report.id))]
            schema, payload, mapping = project(report.template, report.payload, photos,
                                               report.snapshot, current_template)
            for old, new in mapping.items():
                db.execute(update(Photo).where(Photo.report_id == report.id, Photo.checkpoint == old)
                           .values(checkpoint=new))
            report.template, report.payload = schema, payload
        return
    if report.status != "draft" or report.template.get("version") != 6 or report.template.get("frozen_upload"):
        return
    from sqlalchemy import update
    from .models import Photo
    from .checklist import corrected_draft_payload, corrected_draft_template
    original = report.template
    report.payload = corrected_draft_payload(report.payload, original, report.status)
    for old, new in (("MAN.21", "MAN.22"), ("EST.10", "EST.18")):
        db.execute(update(Photo).where(Photo.report_id == report.id, Photo.checkpoint == old)
                   .values(checkpoint=new))
    report.template = corrected_draft_template(original, report.status)
