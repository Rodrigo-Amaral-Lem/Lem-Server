# SPDX-License-Identifier: GPL-3.0-only
"""A first upload can follow a concurrent Excel update to another membership."""
from io import BytesIO
from uuid import uuid4

import pytest
from openpyxl import load_workbook

from app.main import TEMPLATE
from test_stations_055 import close, shared
from test_workflow import answers, setup, upload, xlsx


@pytest.mark.parametrize("edited, changed, expected", [
    (False, True, 200), (True, True, 409), (True, False, 200),
])
def test_offline_identity(setup, edited, changed, expected):
    c, h, first, second, charger, _, _app = shared(setup)
    initial = {key: charger["details"].get(key, "") for key in ("manufacturer", "model", "serial")}
    payload = {"answers": answers()}
    submitted = "Técnico 90" if edited else initial["model"]
    payload["answers"]["EST.03"]["value"] = submitted
    body = dict(checklist_version=TEMPLATE["version"], charger_id=charger["id"], status="draft",
                base_revision=0, station_identity_at_start=initial, payload=payload)
    if changed:
        workbook = load_workbook(BytesIO(xlsx((charger["external_id"],))))
        workbook.active["B2"] = "Excel 80"
        stream = BytesIO()
        workbook.save(stream)
        assert upload(c, h, second["id"], True, stream.getvalue()).status_code == 200

    rid = str(uuid4())
    created = c.put("/api/reports/" + rid, json=body, headers=h)
    assert created.status_code == 200, created.text
    assert created.json()["snapshot"]["charger"]["model"] == initial["model"]
    body["base_revision"] = created.json()["revision"]
    # A repeated body cannot change the permanently captured opening reference.
    body["station_identity_at_start"] = dict(initial, model="Excel 80")
    result = close(c, h, rid, body)
    assert result.status_code == expected, result.text
    for contract in first, second:
        row = c.get("/api/contracts/" + contract["id"] + "/chargers", headers=h).json()[0]
        assert row["details"]["model"] == ("Excel 80" if changed else submitted)
    current = c.get("/api/reports/" + rid, headers=h).json()
    assert current["payload"]["answers"]["EST.03"]["value"] == submitted
    assert current["status"] == ("closed" if expected == 200 else "draft")
