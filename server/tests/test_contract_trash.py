# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

from app.main import TEMPLATE
"""Admin-only retention, offline mutation rejection and FK-complete physical purge."""
import uuid
from datetime import timedelta
from io import BytesIO
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select, func
from app.compatibility import VERSION
from app.models import (Contract, ContractTrash, Charger, Report, DeletedReport,
                        OwnershipTransfer, Photo, Assignment, ImportBatch, Audit, SessionToken)
from app.contract_trash import utc_now
from test_workflow import setup, upload, answers


def populated(setup):
    c, h, contract, app = setup
    cid = contract["id"]
    assert upload(c, h, cid, True).status_code == 200
    charger = c.get(f"/api/contracts/{cid}/chargers", headers=h).json()[0]
    rid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    response = c.put(f"/api/reports/{rid}", headers=h, json=dict(checklist_version=TEMPLATE["version"],
        charger_id=charger["id"], base_revision=0, payload={"answers": answers()}))
    assert response.status_code == 200, response.text
    image = BytesIO(); Image.new("RGB", (8, 8), "cyan").save(image, format="PNG")
    response = c.put(f"/api/reports/{rid}/photos/{pid}", headers=h,
        data={"checkpoint": "EST.01", "base_revision": 1},
        files={"file": ("photo.png", image.getvalue(), "image/png")})
    assert response.status_code == 200, response.text
    return cid, charger["id"], rid, pid, image.getvalue()


def trash(c, h, cid, word="APAGAR"):
    return c.post(f"/api/contracts/{cid}/trash", headers=h, json={"confirmation": word})


def test_confirmation_authorization_and_exact_restore(setup):
    c, h, _, app = setup
    cid, charger, rid, pid, data = populated(setup)
    # Every privileged endpoint is enforced server-side, not just hidden in QML.
    tech = c.post("/api/users", headers=h, json=dict(email="tech@lem.pt", password="StrongPassword123!", name="Tech")).json()["id"]
    token = c.post("/api/login", json=dict(email="tech@lem.pt", password="StrongPassword123!")).json()["token"]
    th = {"Authorization": "Bearer " + token}
    c.put(f"/api/contracts/{cid}/users/{tech}", headers=h)
    assert trash(c, th, cid).status_code == 403
    assert c.get("/api/admin/deleted-contracts", headers=th).status_code == 403
    assert c.post(f"/api/contracts/{cid}/restore", headers=th).status_code == 403
    for word in ["", "apagar", " APAGAR", "APAGAR ", "SIM"]:
        assert trash(c, h, cid, word).status_code == 422
    assert c.post(f"/api/contracts/{cid}/trash", headers=h, json={}).status_code == 422
    original = c.get(f"/api/reports/{rid}", headers=h).json()
    deleted = trash(c, h, cid)
    assert deleted.status_code == 200, deleted.text
    # An identical retry after a lost response cannot extend 30 days.
    assert trash(c, h, cid).json() == deleted.json()
    assert c.get("/api/contracts", headers=h).json() == []
    assert c.get("/api/contracts", headers=th).json() == []
    listing = c.get("/api/admin/deleted-contracts", headers=h).json()
    assert len(listing) == 1 and listing[0]["id"] == cid
    from datetime import datetime
    assert datetime.fromisoformat(listing[0]["purge_after"]) - datetime.fromisoformat(listing[0]["deleted_at"]) == timedelta(days=30)
    # Reads, exports, importing, writes and photos cannot bypass the contract gate.
    for path in [f"/api/contracts/{cid}/chargers", f"/api/reports/{rid}",
                 f"/api/reports/{rid}/pdf", f"/api/reports/{rid}/bundle",
                 f"/api/reports/{rid}/photos/{pid}"]:
        assert c.get(path, headers=h).status_code == 423, path
    assert upload(c, h, cid, True).status_code == 423
    assert c.put(f"/api/contracts/{cid}/users/{tech}", headers=h).status_code == 423
    assert c.put(f"/api/reports/{rid}", headers=h, json=dict(checklist_version=TEMPLATE["version"],charger_id=charger,
        base_revision=2, payload={"answers": {}})).status_code == 423
    assert c.put(f"/api/reports/{uuid.uuid4()}", headers=h, json=dict(checklist_version=TEMPLATE["version"],charger_id=charger,
        base_revision=0, payload={"answers": {}})).status_code == 423
    # Neither an existing session nor closing a report can bypass retention.
    for headers in (h, th):
        assert c.get(f"/api/reports/{rid}/exists", headers=headers).status_code == 423
        assert c.get(f"/api/reports/{rid}/photos/{pid}", headers={**headers,"Range":"bytes=0-3"}).status_code == 423
        assert c.put(f"/api/reports/{rid}",headers=headers,json=dict(
            checklist_version=TEMPLATE["version"],charger_id=charger,base_revision=original['revision'],
            status="closed",payload=original['payload'])).status_code == 423
    assert c.delete(f"/api/reports/{rid}?base_revision=2", headers=h).status_code == 423
    assert c.delete(f"/api/reports/{rid}/photos/{pid}?base_revision=2", headers=h).status_code == 423
    assert c.put(f"/api/reports/{rid}/photos/{uuid.uuid4()}", headers=h,
        data={"checkpoint":"EST.01", "base_revision":2},
        files={"file":("photo.png",data,"image/png")}).status_code == 423
    assert c.post(f"/api/contracts/{cid}/restore", headers=h).status_code == 200
    assert c.post(f"/api/contracts/{cid}/restore", headers=h).status_code == 200
    assert c.get(f"/api/reports/{rid}", headers=h).json() == original
    assert c.get(f"/api/reports/{rid}/photos/{pid}", headers=h).content == data
    assert c.get(f"/api/contracts/{cid}/chargers", headers=th).status_code == 200
    assert c.get("/api/admin/deleted-contracts", headers=h).json() == []


def test_expiry_purges_all_dependents_and_leaves_other_contracts(setup, monkeypatch):
    c, h, _, app = setup
    cid, charger, rid, pid, _ = populated(setup)
    other = c.post("/api/contracts", headers=h, json=dict(company="Other", year=2026, semester=1, kind="TRE")).json()["id"]
    upload(c, h, other, True)
    with app.state.factory() as db:
        owner = db.get(Report, rid).owner_id
        db.add(Assignment(user_id=owner, contract_id=cid))
        removed = str(uuid.uuid4())
        db.add(DeletedReport(id=removed, charger_id=charger, owner_id=owner, status="draft"))
        for report_id in [rid, removed]:
            db.add(OwnershipTransfer(id=str(uuid.uuid4()),report_id=report_id,
                old_owner_id=owner,new_owner_id=owner,actor_id=owner))
        db.commit()
    trash(c, h, cid)
    with app.state.factory() as db:
        from datetime import datetime
        expiry = datetime.fromisoformat(db.get(ContractTrash, cid).purge_after)
    assert app.state.purge_expired_contracts(expiry-timedelta(microseconds=1)) == 0
    # Exact deadline is already too late, irrespective of sweep scheduling.
    with app.state.factory() as db:
        for token in db.scalars(select(SessionToken)):
            token.expires = (expiry + timedelta(days=1)).isoformat()
        db.commit()
    monkeypatch.setattr("app.main.now", lambda: expiry.isoformat())
    assert c.post(f"/api/contracts/{cid}/restore", headers=h).status_code == 410
    assert c.get("/api/admin/deleted-contracts", headers=h).json() == []
    assert app.state.purge_expired_contracts(expiry) == 1
    assert app.state.purge_expired_contracts(expiry) == 0
    with app.state.factory() as db:
        for cls in [Contract, ContractTrash]:
            assert db.get(cls,cid) is None
        assert db.get(Report,rid) is None and db.get(Photo,pid) is None
        assert db.get(Charger,charger) is None
        for cls in [DeletedReport,OwnershipTransfer,Assignment]:
            assert db.scalar(select(func.count()).select_from(cls)) == 0
        assert db.scalar(select(func.count()).select_from(ImportBatch).where(ImportBatch.contract_id==cid)) == 0
        assert db.get(Contract,other) is not None
        assert db.scalar(select(func.count()).select_from(Charger).where(Charger.contract_id==other)) == 2
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action=="contract.purge")) == 1
    assert c.post(f"/api/contracts/{cid}/restore",headers=h).status_code == 404
    assert c.put(f"/api/reports/{uuid.uuid4()}",headers=h,json=dict(checklist_version=TEMPLATE["version"],charger_id=charger,base_revision=0,payload={})).status_code == 404


def test_restore_before_deadline_and_worker_survives_restart(setup):
    c,h,_,app=setup
    cid,_,rid,_,_=populated(setup)
    trash(c,h,cid)
    assert c.post(f"/api/contracts/{cid}/restore",headers=h).status_code == 200
    assert app.state.purge_expired_contracts(utc_now()+timedelta(days=31)) == 0
    assert c.get(f"/api/reports/{rid}",headers=h).status_code == 200
    trash(c,h,cid)
    with app.state.factory() as db:
        db.get(ContractTrash,cid).purge_after=(utc_now()-timedelta(seconds=1)).isoformat()
        db.commit()
    # Real ASGI lifespan runs the periodic sweep, including after downtime.
    import time
    with TestClient(app) as live:
        for _ in range(100):
            with app.state.factory() as db:
                if db.get(Contract,cid) is None:break
            time.sleep(.01)
        else: raise AssertionError("startup retention sweep did not purge expired contract")


def test_existing_database_upgrade_adds_only_retention_table(setup):
    c,h,_,app=setup
    cid,_,rid,pid,data=populated(setup)
    engine=app.state.factory.kw["bind"]
    # Simulate the previous release's schema without destroying any old table.
    ContractTrash.__table__.drop(engine)
    from app.main import create_app
    upgraded=create_app(str(engine.url),bootstrap=False)
    with TestClient(upgraded, headers={"X-Lem-Client-Version": VERSION}) as live:
        assert live.get(f"/api/reports/{rid}",headers=h).status_code == 200
        assert trash(live,h,cid).status_code == 200
        assert live.post(f"/api/contracts/{cid}/restore",headers=h).status_code == 200
        assert live.get(f"/api/reports/{rid}/photos/{pid}",headers=h).content == data
