# SPDX-License-Identifier: GPL-3.0-only
"""Mirror the client rules with exact integer comparisons and explicit note provenance."""
from copy import deepcopy
from decimal import Decimal
from .measurements import format_value, not_applicable

TRIP_NOTE = "Valor de intensidade de disparo diferencial fora do calibre configurado"
EARTH_FAIL_NOTE = "Valor de terra de proteção elevado e acima do limite para o calibre da proteção diferencial configurada"
EARTH_HIGH_NOTE = "Valor de terra de proteção elevado mas dentro de limites para o calibre da proteção diferencial configurada"


def untouched(answer):
    return bool(answer.get("suggested_notes")) and answer.get("suggested_notes") == answer.get("notes")


def clear_suggestion(answer):
    if untouched(answer):
        answer["notes"] = ""
    answer.pop("suggested_notes", None)


def reading(answers, key, scale=1):
    answer = answers.get(key, {})
    if not isinstance(answer, dict) or "previous_value" in answer:
        return None
    try:
        value = format_value(key, answer.get("value", ""))
        if not_applicable(value) or value in ("OK", "NOK"):
            return None
        return int(Decimal(value.split()[0].replace(",", ".")) * scale)
    except (ValueError, ArithmeticError, IndexError):
        return None


def calculate(answers):
    configured = reading(answers, "MAN.11")
    result = {}
    if configured is None or configured <= 0:
        return result
    trip = reading(answers, "MAN.12")
    if trip is not None:
        failed = trip * 100 >= configured * 105 or trip * 2 <= configured
        result["MAN.12"] = ("NOK" if failed else "OK", TRIP_NOTE if failed else "")
    earth = reading(answers, "MAN.18", 100)
    if earth is not None:
        # Temporary 50 V criterion requested by the user. See the documented
        # 25 V exception for publicly accessible outdoor charging installations.
        failed = earth * configured > 50 * 100000
        result["MAN.18"] = ("NOK" if failed else "OK",
                            EARTH_FAIL_NOTE if failed else EARTH_HIGH_NOTE if earth > 5000 else "")
    return result


def apply_assessments(answers, offer_suggestions=False):
    result = deepcopy(answers)
    calculated = calculate(answers)
    for key in ("MAN.12", "MAN.18"):
        if not isinstance(result.get(key), dict):
            continue
        answer = result[key]
        if key in calculated:
            state, note = calculated[key]
            if answer.get("assessment") != state or (untouched(answer) and answer.get("suggested_notes") != note):
                clear_suggestion(answer)
            answer.update(assessment=state, auto_assessment=state)
            if offer_suggestions and note and not answer.get("notes", "").strip():
                answer.update(notes=note, suggested_notes=note)
        elif "auto_assessment" in answer:
            if answer.get("assessment") == answer["auto_assessment"]:
                answer.pop("assessment", None)
            answer.pop("auto_assessment", None)
            clear_suggestion(answer)
    return result
