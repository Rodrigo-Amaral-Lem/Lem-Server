# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Measurement format and voltage compliance. Out-of-range readings remain recordable."""
import re

VOLTAGES = {f"PC02.{i:02d}" for i in range(5, 12)}

def canonical(key):
    if re.fullmatch(r"MAN\.\d{2}", key):
        return f"PC02.{int(key[4:]) + 1:02d}"
    return key

def digits(key):
    key = canonical(key)
    return 3 if key in VOLTAGES else 5 if key == "PC02.19" else 0

def unit(key):
    key = canonical(key)
    return "V" if key in VOLTAGES else "Ω" if key == "PC02.19" else ""

def not_applicable(value):
    text = " ".join(value.split()).upper().replace("Ã", "A").replace("Á", "A")
    return text in ("NA", "N/A", "NAO APLICAVEL")

def format_value(key, value):
    value = value.strip()
    if key == "MAN.18" and value.upper() in ("OK", "NOK"):
        return value.upper()
    if not digits(key):
        return value
    if not_applicable(value):
        return "Não Aplicável"
    match = re.fullmatch(r"([0-9]{1," + str(digits(key)) + r"}(?:[.,][0-9]{1,2})?)(?:\s*" + unit(key) + r")?", value, re.I)
    if not match:
        raise ValueError(f"{key}: máximo {digits(key)} algarismos inteiros e 2 casas decimais, ou Não Aplicável")
    return match[1].replace(".", ",") + " " + unit(key)

def outside(key, value):
    key = canonical(key)
    if key not in VOLTAGES or not_applicable(value):
        return False
    try:
        n = float(format_value(key, value).split()[0].replace(",", "."))
    except ValueError:
        return False
    if key == "PC02.11":
        return n >= 50
    low, high = (360, 440) if key in {"PC02.05", "PC02.06", "PC02.07"} else (207, 253)
    return not low <= n <= high
