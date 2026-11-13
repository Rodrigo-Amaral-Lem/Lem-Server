# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

from app.main import TEMPLATE
"""Deterministic interleavings: deletion occurs before the photograph row lock."""
import uuid
from io import BytesIO
import pytest
from PIL import Image
from sqlalchemy import delete, select, func
from sqlalchemy.orm import Session
from app.models import Report, Photo, DeletedReport
from test_workflow import setup, upload


@pytest.mark.parametrize("operation", ["upload", "remove"])
def test_report_deleted_before_photo_lock_is_gone_not_internal_error(setup, monkeypatch, operation):
    client, headers, contract, app = setup
    upload(client, headers, contract["id"], True)
    charger = client.get(f'/api/contracts/{contract["id"]}/chargers', headers=headers).json()[0]
    rid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    result = client.put(f"/api/reports/{rid}", headers=headers, json={
        "checklist_version": TEMPLATE["version"], "charger_id": charger["id"], "base_revision": 0, "payload": {"answers": {}}})
    assert result.status_code == 200, result.text
    original = Session.scalar
    interleaved = []

    def remove_before_lock(session, statement, *args, **kwargs):
        entities = getattr(statement, "column_descriptions", [])
        if (not interleaved and getattr(statement, "_for_update_arg", None) is not None
                and entities and entities[0].get("entity") is Report):
            row = session.get(Report, rid)
            session.add(DeletedReport(id=rid, charger_id=row.charger_id,
                                      owner_id=row.owner_id, status=row.status))
            session.execute(delete(Photo).where(Photo.report_id == rid))
            session.execute(delete(Report).where(Report.id == rid)
                            .execution_options(synchronize_session=False))
            session.commit()
            interleaved.append(True)
        return original(session, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "scalar", remove_before_lock)
    path = f"/api/reports/{rid}/photos/{pid}"
    if operation == "upload":
        data = BytesIO()
        Image.new("RGB", (8, 8), "cyan").save(data, format="PNG")
        result = client.put(path, headers=headers, data={"checkpoint": "EST.01", "base_revision": "1"},
                            files={"file": ("capture.png", data.getvalue(), "image/png")})
    else:
        result = client.delete(path + "?base_revision=1", headers=headers)
    assert interleaved
    assert result.status_code == 410, result.text
    with app.state.factory() as db:
        assert db.get(Report, rid) is None
        assert db.get(DeletedReport, rid) is not None
        assert db.scalar(select(func.count(Photo.id)).where(Photo.report_id == rid)) == 0
