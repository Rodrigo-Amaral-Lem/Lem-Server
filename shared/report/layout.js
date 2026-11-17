// SPDX-License-Identifier: GPL-3.0-only
// The single PDF layout used by native Qt and the server's isolated Qt worker.
// Coordinates and font sizes are points; all fonts/artwork ship with this file.
function renderReport(report, styles, host) {
    "use strict";
    var snapshot = report.snapshot || {}, charger = snapshot.charger || {}, contract = snapshot.contract || {};
    var payload = report.payload || {}, answers = payload.answers || {};
    var presentation = report.presentation || {issuer: "Lem", client_logos: contract.report_logos || []};
    var style = styles[presentation.issuer] || styles.Lem;
    var left = 35, width = 525, bottom = 790, y = 35, page = 1;
    var ink = "#263238", muted = "#52616a";
    function str(value) {
        if (value === undefined || value === null) return "";
        if (typeof value === "boolean") return value ? "Sim" : "Não";
        return typeof value === "object" ? JSON.stringify(value) : String(value);
    }
    // Render the current connector name in frozen snapshots/templates too.
    // Answers, observations, item IDs and original evidence remain untouched.
    function connectorText(value) {
        return str(value).replace(/\bMennekes\s+(?:Type|Tipo)\s*2\b/gi, "AC Type 2");
    }
    function footer() {
        host.rect(left, 803, width, 1, style.accent);
        host.text(style.label + " · Relatório de manutenção PCVE", left, 813, 8, false, muted);
        host.text("Página " + page, 510, 813, 8, false, muted);
    }
    function next() {
        footer(); host.page(); page++; y = 35;
        host.text(style.label + " · " + str(charger.external_id), left, y, 9, true, style.primary);
        host.rect(left, y + 18, width, 1, style.accent); y += 32;
    }
    function room(height) { if (y + height > bottom) next(); }
    function text(value, size, bold, color) {
        size = size || 9;
        var lines = host.wrap(str(value), width, size, !!bold);
        for (var i = 0; i < lines.length; i++) {
            room(size + 5); host.text(lines[i], left, y, size, !!bold, color || ink); y += size + 5;
        }
        y += 3;
    }
    function heading(value) {
        room(58); y += 8;
        var lines = host.wrap(str(value), width - 20, 11, true);
        for (var i = 0; i < lines.length; i++) {
            room(23); host.rect(left, y, width, 23, style.primary);
            host.text(lines[i], left + 10, y + 4, 11, true, "#ffffff"); y += 23;
        }
        host.rect(left, y, width, 2, style.accent); y += 12;
    }
    function row(label, value) {
        if (!str(value).trim()) return;
        var labels = host.wrap(label, 116, 9, true), lines = host.wrap(str(value), width - 145, 9, false);
        var n = Math.max(labels.length, lines.length);
        for (var i = 0; i < n; i++) {
            room(22); host.rect(left, y, 135, 22, style.pale);
            host.text(labels[i] || "", left + 7, y + 4, 9, true, ink);
            host.text(lines[i] || "", left + 142, y + 4, 9, false, ink);
            host.rect(left, y + 21.5, width, 0.5, "#dbe3e7"); y += 22;
        }
    }
    function image(source, x, top, w, h) { host.image(source, x, top, w, h); }
    image("data:" + style.png, left, 35, 165, 66);
    var logos = (presentation.client_logos || []).filter(function(logo) { return logo.id !== "Lem" && logo.id !== "CSMTEC"; }).slice(0, 2);
    for (var l = 0; l < logos.length; l++) image("data:" + logos[l].png, left + width - logos.length * 150 + l * 150, 35, 140, 66);
    y = 118;
    text("RELATÓRIO DE MANUTENÇÃO", 17, true, style.primary);
    host.rect(left, y + 1, width, 3, style.accent); y += 18;
    row("Entidade", style.label);
    row("Contrato", contract.name); row("Cliente", contract.company); row("ID do carregador", charger.external_id);
    var fields = [["address", "Morada"], ["postal_code", "Código postal"], ["cpe", "CPE"], ["manufacturer", "Fabricante"],
        ["model", "Modelo"], ["serial", "Número de série"], ["power", "Potência"], ["power_limit", "Limitação de potência"],
        ["connectors", "Tomadas / fichas"], ["latitude", "Latitude"], ["longitude", "Longitude"], ["region", "CCDR"], ["energy_supply", "Fornecimento de energia"]];
    fields.forEach(function(field) {
        var value = str(charger[field[0]]).trim();
        if (field[0] === "connectors") value = connectorText(value);
        if (field[0] === "power" || field[0] === "power_limit") value = value.replace(/\s*kVA\s*$/i, "").trim();
        if (value && (field[0] === "power" || field[0] === "power_limit")) value += " kVA";
        row(field[1], value);
    });
    row("Report / Revisão", report.identifier + " / " + str(report.revision || 0));
    row("Estado", report.status === "closed" ? "FECHADO" : "RASCUNHO");
    row("Técnico", report.technician || (answers["FIN.02"] || {}).value);
    row("Início", host.date(payload.started || report.started || ""));
    row("Fim", host.date(payload.finished || report.finished || ""));
    row("Report de origem", payload.source_report_id);
    (report.template.sections || []).forEach(function(section) {
        heading(section.title);
        var origin = section.id === "MAN" ? payload.measurement_copy : section.id === "QVE" ? payload.qve_copy : null;
        if (origin) text((section.id === "MAN" ? "Medições copiadas de " : "QVE copiado de ") + str(origin.source_station_id) + " · " + host.date(origin.copied_at || ""));
        (section.items || []).forEach(function(item) {
            var id = item.id, answer = answers[id] || {}, raw = str(answer.value), label = connectorText(item.label);
            if (/^(MAN\.0[4-6]|PC02\.0[5-7])$/.test(id)) label = label.replace("380-420 V", "360-440 V");
            if (/^(MAN\.0[7-9]|PC02\.(08|09|10))$/.test(id)) label = label.replace("200-250 V", "207-253 V");
            room(52); text(id + " · " + label, 10, true);
            if (item.kind === "notes") {
                if (raw.trim()) text(raw);
                else if (!str(answer.notes).trim()) text("Sem observações");
            } else {
                // Old frozen current readings may be unitless; never relabel them as mA.
                var measurementId = Number(report.template.version) < 7 && /^(MAN\.11|MAN\.12)$/.test(id) ? "legacy-" + id : id;
                var formatted = Measurements.format(measurementId, raw), nok = Measurements.outside(measurementId, raw);
                text("Resposta: " + ((formatted === null ? raw : formatted) || "Por preencher") + (nok ? " · NOK — fora dos limites" : ""), 9, nok, nok ? "#a12626" : ink);
            }
            if (answer.assessment) text("Avaliação: " + answer.assessment);
            if (str(answer.notes).trim()) text("Observações: " + answer.notes);
            var signature = (payload.signatures || {})[id];
            if (signature) text("Assinatura de " + str(signature.signed_by) + " · " + host.date(signature.signed_at || ""), 9);
            (report.photos || []).filter(function(photo) { return photo.checkpoint === id; }).forEach(function(photo) {
                if (str(photo.mime).indexOf("video/") === 0) { text("Evidência " + id + " · Vídeo (ficheiro incluído na exportação ZIP)"); return; }
                var size = host.imageSize(photo.path);
                if (size.length !== 2 || !size[0] || !size[1]) { text("Evidência " + id + " · imagem ilegível; não foi possível incluí-la no PDF"); return; }
                var isSignature = signature && signature.photo_id === photo.id;
                var scale = Math.min((isSignature ? 240 : 380) / size[0], (isSignature ? 110 : 280) / size[1]);
                var w = size[0] * scale, h = size[1] * scale;
                room(h + 40); image(photo.path, left, y, w, h); y += h + 5;
                text(isSignature ? "Assinatura de " + str(signature.signed_by) + " · " + host.date(signature.signed_at || "") : "Evidência " + id, 8, false, muted);
            });
            y += 8;
        });
    });
    footer();
}
