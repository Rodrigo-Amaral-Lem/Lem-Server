# SPDX-License-Identifier: GPL-3.0-only
"""Isolated Qt main thread. Same primitives as App/src/ReportPdf.cpp.

Only the bundled JavaScript runs here. User text is passed as data, never code.
"""
import json
import sys
from pathlib import Path
from PySide6.QtCore import QObject, Slot, QDateTime, QTimeZone, Qt, QRectF, QPointF, QSizeF, QMarginsF, QUrl
from PySide6.QtGui import (QGuiApplication, QFont, QFontDatabase, QFontMetricsF,
    QImage, QImageReader, QTextLayout, QTextOption, QPainter, QPdfWriter, QPageSize, QColor)
from PySide6.QtQml import QJSEngine

ROOT = Path(__file__).resolve().parents[2] / "shared/report"


def font(size, bold):
    f = QFont("DejaVu Sans")
    f.setPixelSize(size)
    f.setBold(bold)
    f.setHintingPreference(QFont.PreferNoHinting)
    f.setKerning(False)
    f.setFeature(QFont.Tag.fromString("liga"), 0)
    f.setStyleStrategy(QFont.PreferAntialias)
    return f


class ReportPainter(QObject):
    def __init__(self, pdf, painter):
        super().__init__()
        self.pdf, self.painter = pdf, painter
        self.last_source, self.last_image = None, QImage()
        self.ok = True

    def load(self, source):
        if source != self.last_source:
            self.last_source = source
            if source.startswith("data:"):
                from base64 import b64decode
                self.last_image = QImage.fromData(b64decode(source[5:]))
            else:
                url = QUrl(source)
                path = url.toLocalFile() if url.isLocalFile() else source
                reader = QImageReader(path)
                # Qt may recover a truncated JPEG without reporting an error.
                if bytes(reader.format()).lower() == b"jpeg" and b"\xff\xd9" not in Path(path).read_bytes():
                    self.last_image = QImage()
                    return self.last_image
                reader.setAutoTransform(True)
                size = reader.size()
                if size.width() > 1600 or size.height() > 1600:
                    reader.setScaledSize(size.scaled(1600, 1600, Qt.KeepAspectRatio))
                self.last_image = reader.read()
        return self.last_image

    @Slot(str, float, int, bool, result="QVariantList")
    def wrap(self, value, width, size, bold):
        result = []
        for paragraph in value.split("\n"):
            if not paragraph:
                result.append("")
                continue
            layout = QTextLayout(paragraph, font(size, bold))
            option = QTextOption()
            option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
            option.setUseDesignMetrics(True)
            layout.setTextOption(option)
            layout.beginLayout()
            # QTextLine offsets are UTF-16, including for supplementary characters.
            utf16 = paragraph.encode("utf-16-le")
            while True:
                line = layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(width)
                start = line.textStart() * 2
                result.append(utf16[start:start + line.textLength() * 2].decode("utf-16-le"))
            layout.endLayout()
        return result

    @Slot(str, float, float, int, bool, str)
    def text(self, value, x, y, size, bold, color):
        f = font(size, bold)
        self.painter.setFont(f)
        self.painter.setPen(QColor(color))
        self.painter.drawText(QPointF(x, y + QFontMetricsF(f).ascent()), value)

    @Slot(float, float, float, float, str)
    def rect(self, x, y, w, h, color):
        self.painter.fillRect(QRectF(x, y, w, h), QColor(color))

    @Slot(str, result="QVariantList")
    def imageSize(self, source):
        im = self.load(source)
        return [im.width(), im.height()]

    @Slot(str, float, float, float, float)
    def image(self, source, x, y, w, h):
        im = self.load(source)
        if not im.isNull():
            size = QSizeF(im.size()).scaled(w, h, Qt.KeepAspectRatio)
            self.painter.drawImage(QRectF(x, y + (h - size.height()) / 2, size.width(), size.height()), im)

    @Slot()
    def page(self):
        self.ok = self.pdf.newPage() and self.ok

    @Slot(str, result=str)
    def date(self, value):
        dt = QDateTime.fromString(value, Qt.ISODate)
        return dt.toTimeZone(QTimeZone(b"Europe/Lisbon")).toString("dd-MM-yyyy HH:mm:ss") if dt.isValid() and dt.timeSpec() != Qt.LocalTime else value


def render(manifest, destination):
    app = QGuiApplication.instance() or QGuiApplication(["report-worker"])
    for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
        if QFontDatabase.addApplicationFont(str(ROOT / "fonts" / name)) < 0:
            raise RuntimeError("Missing report font")
    report = json.loads(Path(manifest).read_text(encoding="utf-8"))
    pdf = QPdfWriter(str(destination))
    pdf.setResolution(144)
    pdf.setPageSize(QPageSize(QPageSize.A4))
    pdf.setPageMargins(QMarginsF(0, 0, 0, 0))
    pdf.setTitle(report["identifier"])
    pdf.setCreator("LEMApp shared report")
    painter = QPainter()
    if not painter.begin(pdf):
        raise RuntimeError("Cannot start PDF")
    painter.scale(2, 2)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    host = ReportPainter(pdf, painter)
    engine = QJSEngine()
    engine.globalObject().setProperty("report", engine.toScriptValue(report))
    engine.globalObject().setProperty("styles", engine.toScriptValue(json.loads((ROOT / "styles.json").read_text(encoding="utf-8"))))
    engine.globalObject().setProperty("host", engine.newQObject(host))
    script = "\n".join((ROOT / name).read_text(encoding="utf-8") for name in ("measurements.js", "layout.js"))
    result = engine.evaluate(script + "\nrenderReport(report, styles, host);", "report/layout.js")
    painter.end()
    if result.isError() or not host.ok:
        raise RuntimeError("PDF layout failed: " + result.toString())


if __name__ == "__main__":
    render(sys.argv[1], sys.argv[2])
