from .measurements import format_value, outside
# PDF presentation of a frozen report, with Europe/Lisbon dates and resized image renditions.
# Original photograph bytes remain unchanged in the database and downloadable ZIP.

from io import BytesIO
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from xml.sax.saxutils import escape
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    Image,
    KeepTogether,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from PIL import Image as PILImage, ImageOps
from .images import IMAGE_ERRORS
from .exports import MissingEvidence

LOGO = Path(__file__).resolve().parents[2] / "shared" / "branding" / "lem-logo.png"
BRAND = colors.HexColor("#007c91")

FONTS = Path(__file__).resolve().parents[1] / "fonts"
for name, file in [("LEM", "DejaVuSans.ttf"), ("LEMB", "DejaVuSans-Bold.ttf")]:
    if name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(name, str(FONTS / file)))


# Save page drawing states, then replay them once the total page count is known.
class NumberedCanvas(Canvas):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.states = []

    def showPage(self):
        self.states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        for state in self.states:
            self.__dict__.update(state)
            self.setStrokeColor(colors.HexColor("#00a6bb"))
            self.setLineWidth(0.8)
            self.line(35, 39, A4[0] - 35, 39)
            self.setFont("LEM", 8)
            self.setFillColor(colors.HexColor("#667786"))
            self.drawString(35, 25, "LEM • Relatório de manutenção PCVE")
            self.drawRightString(A4[0] - 35, 25, f"{self._pageNumber} / {len(self.states)}")
            super().showPage()
        super().save()


def power_value(value):
    text = "" if value is None else str(value).strip()
    text = re.sub(r"\s*kVA\s*$", "", text, flags=re.IGNORECASE)
    return text + " kVA" if text else ""


# Build an in-memory PDF from report_json output and Photo rows.
# Escape all supplied text before giving it to ReportLab's markup-aware Paragraph.
def build_pdf(r, photos):
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="BodyLEM",
            fontName="LEM",
            fontSize=8.4,
            leading=12,
            textColor=colors.HexColor("#263238"),
            wordWrap="CJK",
        )
    )
    styles.add(
        ParagraphStyle(
            name="HeadLEM",
            fontName="LEMB",
            fontSize=11,
            leading=15,
            spaceBefore=14,
            spaceAfter=7,
            textColor=colors.HexColor("#007c91"),
        )
    )
    styles.add(
        ParagraphStyle(name="TitleLEM", fontName="LEMB", fontSize=17, leading=23, textColor=BRAND)
    )

    styles.add(ParagraphStyle(name="MeasurementNOK", parent=styles["BodyLEM"],
                              textColor=colors.HexColor("#a12626")))

    def p(text, style="BodyLEM"):
        return Paragraph(escape(str(text or "")).replace("\n", "<br/>"), styles[style])

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=35,
        rightMargin=35,
        topMargin=38,
        bottomMargin=45,
        title="Relatório de manutenção PCVE",
        author="LEM",
    )
    # The shared master is bundled by Docker and Qt; never fetch branding over the network.
    heading = Table(
        [[Image(str(LOGO), width=62, height=62), p("RELATÓRIO DE MANUTENÇÃO", "TitleLEM")]],
        colWidths=[82, 443],
        hAlign="LEFT",
    )
    heading.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
            ]
        )
    )
    flow = [heading]
    snap = r["snapshot"]
    c = snap["charger"]
    contract = snap["contract"]
    a = r["payload"].get("answers", {})

    def date(value):
        if not value:
            return "Em curso"
        try:
            return (
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                .astimezone(ZoneInfo("Europe/Lisbon"))
                .strftime("%d-%m-%Y %H:%M:%S")
            )
        except ValueError:
            return value

    rows = [
        ["Contrato", contract["name"]],
        ["Cliente", contract["company"]],
        ["ID do carregador", c["external_id"]],
        ["Local", c.get("location", "")],
        ["Morada", c.get("address", "")],
        ["CPE", c.get("cpe", "")],
        ["Fabricante / Modelo", c.get("manufacturer", "") + " / " + c.get("model", "")],
        ["Número de série", c.get("serial", "")],
        ["Potência", power_value(c.get("power"))],
        ["Report / Revisão", r["id"] + " / " + str(r["revision"])],
        ["Estado", "FECHADO" if r["status"] == "closed" else "RASCUNHO"],
        ["Técnico", r["technician"]],
        ["Início", date(r["started"])],
        ["Fim", date(r["finished"])],
    ]
    extra = [
        ("Código postal", "postal_code"),
        ("Latitude", "latitude"),
        ("Longitude", "longitude"),
        ("CCDR", "region"),
        ("Fornecimento de energia", "energy_supply"),
        ("Limitação de potência", "power_limit"),
        ("Tomadas / fichas", "connectors"),
    ]
    insertion = next(i for i, row in enumerate(rows) if row[0] == "Report / Revisão")
    rows[insertion:insertion] = [
        [label, power_value(c[key]) if key == "power_limit" else c[key]]
        for label, key in extra
        if c.get(key) not in (None, "")
    ]
    # Imported addresses/location text may be longer than one page. Allow a
    # metadata row to split instead of failing the complete PDF/ZIP export.
    t = Table([[p(k), p(v)] for k, v in rows], colWidths=[125, 400], hAlign="LEFT", splitInRow=1)
    t.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#e3f4f7")),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#dbe3e7")),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    flow.append(t)
    flow.append(p("ELABORADO POR • LEM", "HeadLEM"))
    for section in r["template"]["sections"]:
        flow.append(p(section["title"], "HeadLEM"))
        measurements_origin = r.get("payload", {}).get("measurement_copy", {})
        if section["id"] == "MAN" and measurements_origin:
            flow.append(p("Medições copiadas de " + measurements_origin.get("source_station_id", "") + " · " + measurements_origin.get("copied_at", "")))
        origin = r.get("payload", {}).get("qve_copy", {})
        if section["id"] == "QVE" and origin:
            flow.append(p("QVE copiado de " + origin.get("source_station_id", "") + " · " + origin.get("copied_at", "")))
        for item in section["items"]:
            answer = a.get(item["id"], {})
            raw_value = answer.get("value", "")
            try:
                value = format_value(item["id"], raw_value) or "Por preencher"
            except ValueError:
                value = raw_value or "Por preencher"
            if answer.get("assessment"):
                value += " · " + answer["assessment"]
            nok = outside(item["id"], raw_value)
            if nok:
                value += " · NOK — fora dos limites"
            row = Table(
                [
                    [
                        p(
                            item["label"]
                            .replace("380-420 V", "360-440 V")
                            .replace("200-250 V", "207-253 V")
                        ),
                        p(value, "MeasurementNOK" if nok else "BodyLEM"),
                    ]
                ],
                colWidths=[390, 135],
                hAlign="LEFT",
                # A text answer may hold 8000 characters in the narrow column:
                # let the row continue on the next page instead of failing.
                splitInRow=1,
            )
            row.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#dbe3e7")),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("TOPPADDING", (0, 0), (-1, -1), 5),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ]
                )
            )
            flow.append(row)
            if answer.get("notes"):
                flow.append(p("Observações: " + answer["notes"]))
            signature = r.get("payload", {}).get("signatures", {}).get(item["id"], {})
            if signature:
                flow.append(p("Assinatura de " + signature.get("signed_by", "") + " · " + signature.get("signed_at", "")))
            for photo in [x for x in photos if x.checkpoint == item["id"]]:
                # Database failures are not image errors. Missing originals abort
                # the export rather than silently producing incomplete evidence.
                original_bytes = photo.data
                if original_bytes is None:
                    raise MissingEvidence(
                        "Fotografia em falta durante a exportação; tente novamente"
                    )
                if getattr(photo, "mime", "image/jpeg").startswith("video/"):
                    flow.append(p("Evidência " + item["id"] + " · Vídeo (ficheiro incluído na exportação ZIP)"))
                    continue
                # PDF uses an oriented, resized rendition; database/ZIP retain original bytes.
                # Photographs stored before uploads were fully decoded may be damaged:
                # report that in the document instead of failing the whole export.
                try:
                    with PILImage.open(BytesIO(original_bytes)) as original:
                        original.draft("RGB", (1400, 1400))
                        with ImageOps.exif_transpose(original).convert("RGB") as im:
                            im.thumbnail((1400, 1400))
                            buf = BytesIO()
                            im.save(buf, format="JPEG", quality=85)
                            w, h = im.size
                except IMAGE_ERRORS:
                    flow.append(
                        p(
                            "Evidência "
                            + item["id"]
                            + " • imagem ilegível; não foi possível incluí-la no PDF"
                        )
                    )
                    continue
                buf.seek(0)
                is_signature = bool(signature) and getattr(photo, "id", None) == signature.get("photo_id")
                scale = min((200 if is_signature else 255) / w, (100 if is_signature else 185) / h)
                flow.extend(
                    [
                        Spacer(1, 5),
                        Image(buf, width=w * scale, height=h * scale, hAlign="LEFT"),
                        p("Assinatura de " + signature["signed_by"] + " · " + signature["signed_at"] if is_signature else "Evidência " + item["id"]),
                        Spacer(1, 5),
                    ]
                )
    doc.build(flow, canvasmaker=NumberedCanvas)
    return buffer.getvalue()
