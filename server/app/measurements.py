# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Measurement format and voltage compliance. Out-of-range readings remain recordable."""
import re

VOLTAGES = {f"PC02.{i:02d}" for i in range(5, 12)}


def canonical(key):
    if re.fullmatch(r"MAN\.\d{2}", key):
        return f"PC02.{int(key[4:]) + 1:02d}"
    return key


def milliamp(key):
    return key in ("MAN.11", "MAN.12")


def digits(key):
    # Current readings are specific to MAN.11/12. Other MAN fields reuse the PC02 rules.
    if milliamp(key) or key == "ms":
        return 6
    key = canonical(key)
    if key in VOLTAGES:
        return 3
    if key == "PC02.19":
        return 5
    return 0


def unit(key):
    if key == "ms":
        return "ms"
    if milliamp(key):
        return "mA"
    key = canonical(key)
    if key in VOLTAGES:
        return "V"
    if key == "PC02.19":
        return "Ω"
    return ""


def not_applicable(value):
    text = " ".join(value.split()).upper().replace("Ã", "A").replace("Á", "A")
    return text in ("NA", "N/A", "NAO APLICAVEL")


def format_value(key, value, *, checkpoint=None):
    value = value.strip()
    if key == "MAN.18" and value.upper() in ("OK", "NOK"):
        return value.upper()
    max_digits = digits(key)
    if not max_digits:
        return value
    if not_applicable(value):
        return "Não Aplicável"

    expected_unit = unit(key)
    optional_fraction = "" if milliamp(key) or key == "ms" else r"(?:[.,][0-9]{1,2})?"
    number_pattern = rf"[0-9]{{1,{max_digits}}}{optional_fraction}"
    match = re.fullmatch(rf"({number_pattern})(?:\s*{expected_unit})?", value, re.I)
    if not match:
        if milliamp(key) or key == "ms":
            detail = f"6 algarismos, sem casas decimais, em {expected_unit}"
        else:
            detail = f"{max_digits} algarismos inteiros e 2 casas decimais"
        raise ValueError(f"{checkpoint or key}: máximo {detail}, ou Não Aplicável")
    return match[1].replace(".", ",") + " " + expected_unit


def outside(key, value):
    key = canonical(key)
    if key not in VOLTAGES or not_applicable(value):
        return False
    try:
        voltage = float(format_value(key, value).split()[0].replace(",", "."))
    except ValueError:
        return False
    if key == "PC02.11":
        return voltage >= 50
    low, high = (360, 440) if key in {"PC02.05", "PC02.06", "PC02.07"} else (207, 253)
    return not low <= voltage <= high
