"""Integration tests against the supplied workbook (opt-in path, never shipped).

Set STATIONS_TEST_WORKBOOK to the original Import_Sheet workbook.
Validated with the supplied V0-2 XLSM on 2026-11-10.
Fixtures alter only in-memory XML bytes; the user's file is never rewritten.
"""

import os, json, hashlib
from pathlib import Path
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
import xml.etree.ElementTree as ET
import pytest
from test_workflow import setup
from app.importer import analyse, parse
from app.import_profiles import STATIONS_COLUMNS

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@pytest.fixture
def source():
    path = os.getenv("STATIONS_TEST_WORKBOOK")
    if not path:
        pytest.skip("Set STATIONS_TEST_WORKBOOK for real-template integration checks")
    return Path(path).read_bytes()


def profile(source):
    result = analyse(source)
    return next(s["recommended_mapping"] for s in result["sheets"] if s["name"] == "Stations")


def change_cell(data, cell, value=None):
    out = BytesIO()
    with ZipFile(BytesIO(data)) as z, ZipFile(out, "w", ZIP_DEFLATED) as dst:
        for info in z.infolist():
            content = z.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                root = ET.fromstring(content)
                node = root.find(f'.//s:c[@r="{cell}"]', NS)
                assert node is not None
                for child in list(node):
                    node.remove(child)
                node.attrib.pop("t", None)
                if value is not None:
                    ET.SubElement(node, "{" + NS["s"] + "}v").text = str(value)
                content = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            dst.writestr(info, content)
    return out.getvalue()


def test_real_file_autodetect_and_preserve_data(source):
    mapping = profile(source)
    assert mapping["sheet"] == "Stations" and mapping["header_row"] == 1
    assert len(mapping["required_fields"]) == 10
    r = parse(source, mapping)
    assert r["count"] == 51 and r["errors"] == []
    first = r["records"][0]
    assert first["external_id"] == "TRANS_AVR_QVE1_C1"
    assert first["details"]["connectors"] == "CCS2, CCS2"
    assert first["details"]["power_unit"] == "kVA"
    assert first["details"]["power_limit"] == "94"
    assert first["details"]["energy_supply"] == "MT"
    assert first["details"]["region"] == "Norte"
    assert first["details"]["latitude"] == "40.64373215616929"
    assert first["details"]["source"]["Tomadas / Fichas"] == "CCS2, CCS2"


@pytest.mark.parametrize("column", [c for c, _, _, required in STATIONS_COLUMNS if required])
def test_each_required_column_reports_exact_cell(source, column):
    data = change_cell(source, column + "2")
    r = parse(data, profile(source))
    assert r["count"] == 50
    assert any(e["cell"] == column + "2" for e in r["errors"])


def test_optional_columns_may_be_empty_and_zero_limit_is_present(source):
    data = source
    for column in ["C", "D", "E", "G"]:
        data = change_cell(data, column + "2")
    data = change_cell(data, "M2", 0)
    r = parse(data, profile(source))
    assert r["count"] == 51 and not r["errors"]
    assert r["records"][0]["details"]["power_limit"] == "0"


def test_profile_cannot_be_bypassed_by_remapping(source):
    mapping = profile(source)
    mapping["columns"] = {"external_id": "Station ID"}
    r = parse(change_cell(source, "F2"), mapping)
    assert any(e["cell"] == "F2" for e in r["errors"])


def test_station_import_is_atomic_and_reimport_updates(source, setup):
    c, h, contract, app = setup
    cid = contract["id"]
    url = "/api/contracts/" + cid + "/import"
    mapping = profile(source)
    preview = c.post(
        url, headers=h, files={"file": ("stations.xlsm", source)},
        data={"mapping": json.dumps({"sheet": "Stations", "header_row": 1, "columns": {}}), "commit": "false"},
    )
    assert preview.status_code == 200, preview.text
    assert len(preview.json()["records"]) == preview.json()["count"] == 51
    assert preview.json()["records"][-1]["external_id"] == parse(source, mapping)["records"][-1]["external_id"]
    bad = c.post(
        url,
        headers=h,
        files={"file": ("stations.xlsm", change_cell(source, "N3"))},
        data={"mapping": json.dumps(mapping), "commit": "true"},
    )
    assert bad.status_code == 422
    assert c.get("/api/contracts/" + cid + "/chargers", headers=h).json() == []
    ok = c.post(
        url,
        headers=h,
        files={"file": ("stations.xlsm", source)},
        data={"mapping": json.dumps(mapping), "commit": "true"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"created": 51, "updated": 0}
    again = c.post(
        url,
        headers=h,
        files={"file": ("stations.xlsm", source)},
        data={"mapping": json.dumps(mapping), "commit": "true"},
    )
    assert again.json() == {"created": 0, "updated": 51}
