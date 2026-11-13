"""Customer-facing PDF text excludes implementation metadata and original filenames."""

from io import BytesIO
from types import SimpleNamespace
import pytest
from pypdf import PdfReader
from app.pdf_report import build_pdf
from test_bug_regressions import _report, _png


@pytest.mark.parametrize("value,limit", [("94", "94 kVA"), ("94 kVA", 0), (94, None)])
def test_pdf_power_units_and_clean_evidence_labels(value, limit):
    report = _report()
    report["snapshot"]["charger"].update(
        power=value, power_unit="kVA", power_limit=limit, import_profile="lem-stations-v1"
    )
    pdf = build_pdf(
        report, [SimpleNamespace(checkpoint="EST.01", filename="private-upload.png", data=_png())]
    )
    text = " ".join(" ".join(page.extract_text().split()) for page in PdfReader(BytesIO(pdf)).pages)
    assert "Potência 94 kVA" in text
    assert "kVA kVA" not in text and "Unidade" not in text
    if limit is not None:
        expected = "0 kVA" if limit == 0 else "94 kVA"
        assert "Limitação de potência " + expected in text
    assert "import_profile" not in text and "lem-stations-v1" not in text
    assert "Exportado a partir" not in text
    assert "Evidência EST.01" in text and "private-upload.png" not in text


def test_damaged_evidence_retains_checkpoint_warning_without_filename():
    data = build_pdf(
        _report(),
        [SimpleNamespace(checkpoint="EST.01", filename="private-upload.png", data=b"invalid")],
    )
    text = " ".join(p.extract_text() for p in PdfReader(BytesIO(data)).pages)
    assert "Evidência EST.01" in text and "imagem ilegível" in text
    assert "private-upload.png" not in text
