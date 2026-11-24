# SPDX-License-Identifier: GPL-3.0-only
"""Upgrade editable v6 maintenance reports without dropping old notes/evidence.

Closed reports keep their saved template. A correction is an editable copy and
therefore upgrades normally; the source remains protected by the correction transaction.
"""
from copy import deepcopy
from decimal import Decimal
import re

from .maintenance_rules import apply_assessments
from .measurements import not_applicable

LABELS = {
    "EST.01": (
        "Fotografias do interior e exterior do carregador e sua limpeza antes da tarefa executada "
        "(visualizar todos os componentes internos e externos)"
    ),
    "EST.13": "Verifique se o tarifário está visível e em boas condições",
    "EST.16": "Verifique se os autocolantes da DGEG estão visíveis e em boas condições",
}
AFTER_LABEL = (
    "Fotografias do interior e exterior do carregador e sua limpeza depois da tarefa executada "
    "(visualizar todos os componentes internos e externos)"
)
CURRENT_HELP = "1 A = 1000 mA · 0,03 A = 30 mA"


def v7_checkpoint(key):
    return {"MAN.21": "MAN.22", "EST.10": "EST.18"}.get(key, key)


def upgrade_v6_template(template):
    if template.get("version") != 6:
        return template
    result = deepcopy(template)
    result["version"] = 7
    for section in result["sections"]:
        section["items"] = [item for item in section["items"] if item["id"] != "EST.10"]
        for item in section["items"]:
            key = item["id"]
            if key in LABELS:
                item["label"] = LABELS[key]
            if key == "EST.15":
                item.update(
                    label=item["label"].replace("(POS)", "(TPM)"),
                    na_notes_default="Sem TPM",
                    na_notes_required=True,
                )
            if key in ("MAN.11", "MAN.12"):
                item.update(
                    measurement_unit="mA",
                    measurement_digits=6,
                    measurement_decimals=0,
                    help=CURRENT_HELP,
                )
            if key == "MAN.18":
                item.pop("help", None)
            if key == "MAN.21":
                item["id"] = "MAN.22"
        if section["id"] == "MAN":
            # The new after-work photograph precedes the general notes that moved to MAN.22.
            after_work_photo = {
                "id": "MAN.21",
                "label": AFTER_LABEL,
                "kind": "status",
                "photo_required": True,
                "na_notes_required": True,
            }
            section["items"].insert(len(section["items"]) - 1, after_work_photo)
    return result



def trip_time_checkpoint(key):
    """Map a v7 checkpoint to its v8 meaning, including evidence references."""
    if key == "MAN.12":
        return "MAN.13"
    if re.fullmatch(r"MAN\.(1[3-9]|2[0-2])", key):
        return f"MAN.{int(key[4:]) + 2}"
    return key


def upgrade_checkpoint(key, version=6):
    if version == 6:
        key = v7_checkpoint(key)
    return trip_time_checkpoint(key) if version in (6, 7) else key


def upgrade_template(template):
    result = upgrade_v6_template(template)
    if result.get("version") != 7:
        return result
    from .checklist import TEMPLATE
    current = {item["id"]: item for group in TEMPLATE["sections"] if group["id"] == "MAN"
               for item in group["items"]}
    result = deepcopy(result)
    for section in result["sections"]:
        if section["id"] != "MAN":
            continue
        items = []
        for item in section["items"]:
            old = item["id"]
            item["id"] = trip_time_checkpoint(old)
            if item["id"] != old:
                item["measurement_key"] = old
            if old == "MAN.12":
                item["nok_notes_default"] = current["MAN.13"]["nok_notes_default"]
            items.append(item)
            if old in ("MAN.11", "MAN.12"):
                items.append(deepcopy(current["MAN.12" if old == "MAN.11" else "MAN.14"]))
        section["items"] = items
    result["version"] = 8
    return result


def trip_time_payload(payload):
    result = deepcopy(payload)
    for field in ("answers", "signatures"):
        if isinstance(result.get(field), dict):
            result[field] = {trip_time_checkpoint(key): value for key, value in result[field].items()}
    trip = result.get("answers", {}).get("MAN.13")
    if isinstance(trip, dict) and trip.get("assessment") == "NOK":
        from .maintenance_rules import TRIP_NOTE, untouched
        if untouched(trip) and trip["suggested_notes"] == TRIP_NOTE:
            trip["notes"] = trip["suggested_notes"] = "Proteção diferencial não dispara"
    return result


def _joined_notes(*texts):
    # dict.fromkeys retains the first occurrence of each note in its original order.
    return "\n".join(dict.fromkeys(text for text in texts if text))


def upgrade_payload(payload):
    result = deepcopy(payload)
    answers = result.setdefault("answers", {})
    if "MAN.21" in answers:
        former_answer = answers.pop("MAN.21")
        if "MAN.22" not in answers:
            answers["MAN.22"] = former_answer
        elif answers["MAN.22"] != former_answer:
            # Keep the full former answer as well as its text in the visible general notes.
            result.setdefault("retired_answers", {})["MAN.21"] = former_answer
            # Earlier clients stored null for untouched optional notes.
            general_notes = answers["MAN.22"]
            if not isinstance(general_notes, dict):
                general_notes = {"value": "", "notes": ""}
                answers["MAN.22"] = general_notes
            former_fields = former_answer if isinstance(former_answer, dict) else {}
            general_notes["notes"] = _joined_notes(
                general_notes.get("notes", ""),
                former_fields.get("value", ""),
                former_fields.get("notes", ""),
            )

    if "EST.10" in answers:
        former_answer = answers.pop("EST.10")
        result.setdefault("retired_answers", {})["EST.10"] = former_answer
        if isinstance(former_answer, dict) and former_answer.get("notes", "").strip():
            general = answers.get("EST.18")
            if not isinstance(general, dict):
                general = {"value": "", "notes": ""}
                answers["EST.18"] = general
            general["notes"] = _joined_notes(general.get("notes", ""), former_answer["notes"])

    for key in ("MAN.11", "MAN.12"):
        answer = answers.get(key)
        if not isinstance(answer, dict):
            continue
        raw = answer.get("value", "").strip()
        if not raw or not_applicable(raw):
            continue
        # Never guess whether a unitless legacy "1" means 1 A or 1 mA.
        match = re.fullmatch(r"([0-9]{1,9}(?:[.,][0-9]{1,6})?)\s*(mA|A)", raw, re.I)
        if match:
            unit_scale = 1000 if match[2].lower() == "a" else 1
            milliamps = Decimal(match[1].replace(",", ".")) * unit_scale
            if milliamps == milliamps.to_integral_value() and 0 <= milliamps <= 999999:
                answer["value"] = f"{int(milliamps)} mA"
                continue
        # Keep the original text for confirmation when it has no exact current-form reading.
        answer["previous_value"] = answer.get("value", "")
        answer["value"] = ""
        answer.pop("assessment", None)
        answer.pop("auto_assessment", None)
    result["answers"] = apply_assessments(answers, offer_suggestions=True)
    return result


def persist_upgrade(db, report, current_template=None, *, correction=False):
    """Called under the report's write lock; metadata and template move atomically."""
    if (
        report.status == "draft"
        and report.template.get("version", 0) < 6
        and current_template is not None
    ):
        from sqlalchemy import select, update
        from .models import Photo, ReportCorrection
        from .legacy_correction import project
        editing = db.get(ReportCorrection, report.id)
        if correction or (editing and editing.state == "editing"):
            checkpoints = select(Photo.checkpoint).where(Photo.report_id == report.id)
            photos = [{"checkpoint": key} for key in db.scalars(checkpoints)]
            schema, payload, mapping = project(
                report.template, report.payload, photos, report.snapshot, current_template
            )
            for old_checkpoint, new_checkpoint in mapping.items():
                db.execute(
                    update(Photo)
                    .where(Photo.report_id == report.id, Photo.checkpoint == old_checkpoint)
                    .values(checkpoint=new_checkpoint)
                )
            report.template, report.payload = schema, payload
        return
    if report.status != "draft" or report.template.get("version") not in (6, 7) or report.template.get("frozen_upload"):
        return
    from sqlalchemy import update
    from .models import Photo
    from .checklist import corrected_draft_payload, corrected_draft_template
    original = report.template
    report.payload = corrected_draft_payload(report.payload, original, report.status)
    # One CASE expression maps each row from its original checkpoint exactly once.
    from sqlalchemy import case
    keys = ["EST.10", *(f"MAN.{number}" for number in range(12, 23))]
    mapping = {key: upgrade_checkpoint(key, original["version"]) for key in keys}
    db.execute(update(Photo).where(Photo.report_id == report.id, Photo.checkpoint.in_(mapping))
               .values(checkpoint=case(mapping, value=Photo.checkpoint, else_=Photo.checkpoint)))
    report.template = corrected_draft_template(original, report.status)
