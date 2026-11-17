# SPDX-License-Identifier: GPL-3.0-only
"""Keep pre-v6 answers readable without guessing the meaning of reused item IDs."""
from copy import deepcopy

from .checklist import for_charger


def project(template, payload, photos, snapshot, current_template):
    """Return a current correction form, preserved answers and photo checkpoint map.

    Very old forms used different identifiers (even EST.02 could mean serial).
    Keep that content in optional historical notes and require current questions
    to be reviewed. Separate answer/observation fields retain the 8000-char limit.
    This pure projection is shared by GET and the transactional write migration.
    """
    current = for_charger(current_template, snapshot.get("charger", {}))
    items = {item["id"]: item for section in template.get("sections", [])
             for item in section.get("items", [])}
    previous = payload.get("answers", {})
    keys = sorted(set(previous) | {photo["checkpoint"] for photo in photos})
    answers, historical_items, mapping = {}, [], {}
    for number, old_key in enumerate(keys, 1):
        key = f"ANT.{number:03d}"
        mapping[old_key] = key
        old = previous.get(old_key)
        old = old if isinstance(old, dict) else {}
        label = items.get(old_key, {}).get("label", old_key)
        assessment = old.get("assessment", "")
        historical_items.append(dict(
            id=key, kind="notes", optional=True, photo_required=False,
            label=f"{old_key} — {label} — Resposta anterior",
            help=f"Avaliação anterior: {assessment}" if assessment else ""))
        answers[key] = {"value": "", "notes": old.get("value", ""),
                        "legacy_metadata": {k: deepcopy(v) for k, v in old.items()
                                            if k not in ("value", "notes")}}
        if old.get("notes"):
            note_key = key + ".OBS"
            historical_items.append(dict(id=note_key, kind="notes", optional=True, photo_required=False,
                label=f"{old_key} — {label} — Observação anterior"))
            answers[note_key] = {"value": "", "notes": old["notes"]}
    if historical_items:
        current["sections"].append(dict(id="ANT", title="ANT - Registos anteriores",
                                        items=historical_items))
    result = deepcopy(payload)
    result["answers"] = answers
    result["legacy_form_version"] = template.get("version")
    # Old signatures remain evidence; they cannot sign unanswered current fields.
    if result.get("signatures"):
        result["legacy_signatures"] = result.pop("signatures")
    return current, result, mapping
