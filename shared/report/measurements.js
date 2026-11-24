var Measurements = (function() {
// SPDX-License-Identifier: GPL-3.0-only
// Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.



function canonical(id) { return /^MAN\.\d{2}$/.test(id) ? "PC02." + ("0" + (Number(id.slice(4)) + 1)).slice(-2) : id; }
function milliamp(id) { return id === "MAN.11" || id === "MAN.12"; }
function integerReading(id) { return milliamp(id) || id === "ms"; }
function decimals(id) { return integerReading(id) ? "" : "(?:[.,][0-9]{1,2})?"; }
function digits(id) { if (integerReading(id)) return 6; id = canonical(id); return id === "PC02.19" ? 5 : /^PC02\.(0[5-9]|10|11)$/.test(id) ? 3 : 0; }
function unit(id) { if (id === "ms") return "ms"; return milliamp(id) ? "mA" : digits(id) === 5 ? "Ω" : digits(id) ? "V" : ""; }
function notApplicable(value) {
    const text = String(value || "").trim().replace(/\s+/g, " ").toUpperCase().replace(/Ã/g, "A").replace(/Á/g, "A");
    return text === "NA" || text === "N/A" || text === "NAO APLICAVEL";
}
function pattern(id) {
    if (!digits(id)) return /[\s\S]*/;
    return new RegExp("(?:[0-9]{1," + digits(id) + "}" + decimals(id) + "(?:\\s*" + unit(id) + ")?|NA|N/A|N[ãa]o Aplic[áa]vel)", "i");
}
// Keep units and the NA marker out of the editable text. They are presentation,
// while the stored answer retains the canonical value for sync and PDF export.
function editorText(id, value) {
    if (!digits(id)) return String(value || "");
    if (notApplicable(value) || (id === "MAN.18" && /^(OK|NOK)$/.test(String(value)))) return "";
    return String(value || "").replace(new RegExp("\\s*" + unit(id) + "$", "i"), "");
}
function editorPattern(id) {
    return digits(id) ? new RegExp("[0-9]{1," + digits(id) + "}" + decimals(id) + "") : /[\s\S]*/;
}
function format(id, value) {
    const text = String(value || "").trim();
    if (id === "MAN.18" && /^(OK|NOK)$/i.test(text)) return text.toUpperCase();
    if (!digits(id)) return text;
    if (notApplicable(text)) return "Não Aplicável";
    const match = new RegExp("^([0-9]{1," + digits(id) + "}" + decimals(id) + ")(?:\\s*" + unit(id) + ")?$", "i").exec(text);
    return match ? match[1].replace(".", ",") + " " + unit(id) : null;
}
function outside(id, value) {
    id = canonical(id);
    const text = format(id, value);
    if (digits(id) !== 3 || text === null || notApplicable(text)) return false;
    const n = Number(text.split(" ")[0].replace(",", "."));
    if (id === "PC02.11") return n >= 50;
    return ["PC02.05", "PC02.06", "PC02.07"].indexOf(id) >= 0 ? n < 360 || n > 440 : n < 207 || n > 253;
}

return {format:format, outside:outside};
})();
