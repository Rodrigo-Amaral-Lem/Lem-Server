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
    configured_ma = reading(answers, "MAN.11")
    result = {}
    if configured_ma is None or configured_ma <= 0:
        return result

    trip_ma = reading(answers, "MAN.12")
    if trip_ma is not None:
        # Both endpoints fail. Cross-multiplication keeps 50% and 105% exact.
        outside_trip_band = trip_ma * 100 >= configured_ma * 105 or trip_ma * 2 <= configured_ma
        result["MAN.12"] = ("NOK", TRIP_NOTE) if outside_trip_band else ("OK", "")

    hundredths_per_ohm = 100
    earth_hundredths = reading(answers, "MAN.18", hundredths_per_ohm)
    if earth_hundredths is not None:
        # Scale the form's temporary 50 V limit to hundredths of an ohm × mA.
        # Common/docs/report-contract.md describes the 25 V outdoor exception.
        scaled_voltage_limit = 50 * hundredths_per_ohm * 1000
        if earth_hundredths * configured_ma > scaled_voltage_limit:
            result["MAN.18"] = ("NOK", EARTH_FAIL_NOTE)
        elif earth_hundredths > 50 * hundredths_per_ohm:
            result["MAN.18"] = ("OK", EARTH_HIGH_NOTE)
        else:
            result["MAN.18"] = ("OK", "")
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
            if answer.get("assessment") != state or (
                untouched(answer) and answer.get("suggested_notes") != note
            ):
                clear_suggestion(answer)
            answer.update(assessment=state, auto_assessment=state)
            if offer_suggestions and note and not answer.get("notes", "").strip():
                answer.update(notes=note, suggested_notes=note)
        elif "auto_assessment" in answer:
            # A reading disappeared. Keep any assessment the technician has since changed.
            if answer.get("assessment") == answer["auto_assessment"]:
                answer.pop("assessment", None)
            answer.pop("auto_assessment", None)
            clear_suggestion(answer)
    return result
