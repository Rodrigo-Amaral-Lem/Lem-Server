# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Per-connector load tests use the same frozen IDs in validation and PDF exports."""

import json, uuid
from pathlib import Path
from io import BytesIO
import pytest
from pypdf import PdfReader
from app.checklist import for_charger, connector_names
from app.main import TEMPLATE
from app.models import Charger
from test_workflow import setup, upload, answers

CASES = json.loads(
    (Path(__file__).resolve().parents[2] / "shared/connector-cases.json").read_text()
)


@pytest.mark.parametrize("case", CASES)
def test_shared_connector_cases(case):
    if case.get("error"):
        with pytest.raises(ValueError):
            connector_names(case["input"])
        return
    generated = for_charger(TEMPLATE, {"connectors": case["input"]})
    assert generated["load_test_connectors"] == case["names"]
    items = next(s["items"] for s in generated["sections"] if s["id"] == "CARG")
    count = max(1, len(case["names"]))
    assert len(items) == count + 3
    assert [i["id"] for i in items[:count]] == [f"CARG.01.{n:02d}" for n in range(1, count + 1)]
    if "labels" in case:
        assert [i["label"] for i in items[:count]] == case["labels"]


def test_connector_tests_validate_independently_and_export_frozen_labels(setup):
    c, h, contract, app = setup
    upload(c, h, contract["id"], True)
    charger = c.get(f"/api/contracts/{contract['id']}/chargers", headers=h).json()[0]
    with app.state.factory() as db:
        row = db.get(Charger, charger["id"])
        row.details = {**row.details, "connectors": "CCS2, CCS2", "postal_code": "1000-001 Lisboa"}
        db.commit()
    rid = str(uuid.uuid4())
    path = "/api/reports/" + rid
    payload = answers()
    payload.pop("CARG.01.01")
    payload["CARG.01.01"] = {"value": "OK", "notes": ""}
    body = dict(checklist_version=TEMPLATE["version"],
        charger_id=charger["id"],
        base_revision=0,
        status="draft",
        payload={"answers": payload},
        load_test_connectors=["CCS2", "CCS2"],
    )
    r = c.put(path, headers=h, json=body)
    assert r.status_code == 200, r.text
    frozen = r.json()["template"]
    assert frozen["load_test_connectors"] == ["CCS2", "CCS2"]
    rejected = c.put(path, headers=h, json={**body, "base_revision": 1, "status": "closed"})
    assert rejected.status_code == 422 and "CARG.01.02" in rejected.text
    payload["CARG.01.02"] = {"value": "NOK", "notes": ""}
    assert (
        c.put(path, headers=h, json={**body, "base_revision": 1, "status": "closed"}).status_code
        == 422
    )
    payload["CARG.01.02"]["notes"] = "Segundo conector sem iniciar carga."
    from PIL import Image
    buf = BytesIO(); Image.new("RGB", (40, 30), "cyan").save(buf, format="PNG")
    for checkpoint in ("CARG.01.01", "CARG.01.02"):
        photo = c.put(path + "/photos/" + str(uuid.uuid4()), headers=h,
            data={"checkpoint":checkpoint, "base_revision":"1"},
            files={"file":("connector.png",buf.getvalue(),"image/png")})
        assert photo.status_code == 200, photo.text
    closed = c.put(path, headers=h, json={**body, "base_revision": 1, "status": "closed"})
    assert closed.status_code == 200, closed.text
    data = c.get(path + "/pdf", headers=h).content
    text = " ".join(p.extract_text() for p in PdfReader(BytesIO(data)).pages)
    assert "CCS2 #1" in text and "CCS2 #2" in text and "Segundo conector" in text
    assert "Código postal" in text and "1000-001 Lisboa" in text
    with app.state.factory() as db:
        row = db.get(Charger, charger["id"])
        row.details = {**row.details, "connectors": "Mennekes Type 2"}
        db.commit()
    assert c.get(path, headers=h).json()["template"] == frozen
    mismatch = c.put("/api/reports/" + str(uuid.uuid4()), headers=h, json=body)
    assert mismatch.status_code == 409
    body["load_test_connectors"] = ["Mennekes Type 2"]
    body["payload"] = {"answers": {"CARG.01.01": {"value": "OK"}}}
    single = c.put("/api/reports/" + str(uuid.uuid4()), headers=h, json=body)
    assert single.status_code == 200, single.text
    group = next(s for s in single.json()["template"]["sections"] if s["id"] == "CARG")
    assert group["items"][0]["label"] == "Teste de carga do conector Mennekes Type 2"
    # Removed fixed AC/CCS/CHAdeMO checkpoint IDs are no longer accepted.
    bad = {**body, "payload": {"answers": {"CARG.02": {"value": "OK"}}}}
    assert c.put("/api/reports/" + str(uuid.uuid4()), headers=h, json=bad).status_code == 422
