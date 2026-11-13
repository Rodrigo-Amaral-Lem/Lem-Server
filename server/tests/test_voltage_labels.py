"""Updated voltage instructions also apply when exporting an older frozen template."""

import copy
import shutil
import subprocess

import pytest
from app.main import TEMPLATE
from app.pdf_report import build_pdf


def test_voltage_ranges_and_frozen_report_pdf(tmp_path):
    labels = {
        item["id"]: item["label"] for section in TEMPLATE["sections"] for item in section["items"]
    }
    assert all("360-440 V" in labels[f"MAN.0{i}"] for i in (4, 5, 6))
    assert all("207-253 V" in labels[f"MAN.{i:02d}"] for i in (7, 8, 9))
    frozen = copy.deepcopy(TEMPLATE)
    for section in frozen["sections"]:
        for item in section["items"]:
            item["label"] = (
                item["label"].replace("360-440 V", "380-420 V").replace("207-253 V", "200-250 V")
            )
    original = copy.deepcopy(frozen)
    report = dict(
        id="voltage-test",
        revision=4,
        status="closed",
        technician="Técnico",
        started="2026-11-01T10:00:00Z",
        finished="2026-11-01T11:00:00Z",
        snapshot={
            "charger": {"external_id": "PCVE-001"},
            "contract": {"name": "Test", "company": "Test"},
        },
        payload={"answers": {}},
        template=frozen,
    )
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(build_pdf(report, []))
    assert frozen == original, "Display corrections must not mutate the frozen template"
    if not shutil.which("pdftotext"):
        pytest.skip("Poppler required for PDF text assertion")
    text = subprocess.check_output(["pdftotext", str(pdf), "-"], text=True)
    assert text.count("360-440 V") == 3
    assert text.count("207-253 V") == 3
    assert "380-420 V" not in text and "200-250 V" not in text
