from app.main import TEMPLATE
"""Deletion permissions, revision conflicts, photos and stale-client protection."""

import uuid
from io import BytesIO
from PIL import Image
from sqlalchemy import select, func
from test_workflow import setup, upload, answers
from app.models import Report, Photo, DeletedReport, Audit


def technician(c, h, cid, email, assigned=True):
    uid = c.post(
        "/api/users",
        headers=h,
        json={"email": email, "password": "TechPassword123!", "name": email, "role": "tech"},
    ).json()["id"]
    if assigned:
        assert c.put(f"/api/contracts/{cid}/users/{uid}", headers=h).status_code == 200
    token = c.post("/api/login", json={"email": email, "password": "TechPassword123!"}).json()[
        "token"
    ]
    return {"Authorization": "Bearer " + token}


def test_delete_permissions_photos_retry_and_stale_recreation(setup):
    c, h, contract, app = setup
    cid = contract["id"]
    upload(c, h, cid, True)
    charger = c.get(f"/api/contracts/{cid}/chargers", headers=h).json()[0]
    owner = technician(c, h, cid, "owner@test.pt")
    other = technician(c, h, cid, "other@test.pt")
    outsider = technician(c, h, cid, "outside@test.pt", False)
    rid = str(uuid.uuid4())
    url = "/api/reports/" + rid
    body = {
        "checklist_version": TEMPLATE["version"], "charger_id": charger["id"],
        "base_revision": 0,
        "status": "draft",
        "payload": {"answers": {}},
    }
    assert c.put(url, headers=owner, json=body).status_code == 200
    image = BytesIO()
    Image.new("RGB", (32, 24), "green").save(image, "PNG")
    pid = str(uuid.uuid4())
    assert (
        c.put(
            url + "/photos/" + pid,
            headers=owner,
            files={"file": ("photo.png", image.getvalue())},
            data={"checkpoint": "EST.01", "base_revision": "1"},
        ).status_code
        == 200
    )
    assert c.delete(url + "?base_revision=1").status_code in (401, 403)
    assert c.delete(url + "?base_revision=1", headers=outsider).status_code == 403
    assert c.delete(url + "?base_revision=1", headers=other).status_code == 403
    assert c.delete(url + "?base_revision=0", headers=owner).status_code == 409
    assert c.get(url, headers=owner).status_code == 200
    assert c.get(url + "/photos/" + pid, headers=owner).status_code == 200
    assert c.delete(url + "?base_revision=1", headers=owner).json() == {"id": rid, "deleted": True}
    assert c.delete(url + "?base_revision=1", headers=owner).status_code == 200
    assert c.get(url, headers=owner).status_code == 410
    assert c.get(url + "/exists", headers=owner).status_code == 410
    assert c.get(url + "/photos/" + pid, headers=owner).status_code == 410
    assert c.put(url, headers=owner, json=body).status_code == 410
    assert c.delete(url + "?base_revision=1", headers=other).status_code == 403
    assert c.get(url, headers=outsider).status_code == 403
    row = c.get(f"/api/contracts/{cid}/chargers", headers=h).json()[0]
    assert row["reports"] == [] and row["deleted_reports"] == [rid]
    with app.state.factory() as db:
        assert db.get(Report, rid) is None and db.get(Photo, pid) is None
        assert db.get(DeletedReport, rid)
        assert (
            db.scalar(
                select(func.count())
                .select_from(Audit)
                .where(Audit.action == "report.delete", Audit.target == rid)
            )
            == 1
        )


def test_only_admin_can_delete_closed_reports_and_others_drafts(setup):
    c, h, contract, app = setup
    cid = contract["id"]
    upload(c, h, cid, True)
    charger = c.get(f"/api/contracts/{cid}/chargers", headers=h).json()[0]
    owner = technician(c, h, cid, "owner@test.pt")
    body = {
        "checklist_version": TEMPLATE["version"], "charger_id": charger["id"],
        "base_revision": 0,
        "status": "closed",
        "payload": {"answers": answers()},
    }
    rid = str(uuid.uuid4())
    url = "/api/reports/" + rid
    assert c.put(url, headers=owner, json=body).status_code == 200
    assert c.delete(url + "?base_revision=1", headers=owner).status_code == 403
    assert c.delete(url + "?base_revision=1", headers=h).status_code == 200
    assert c.delete(url + "?base_revision=1", headers=owner).status_code == 403
    rid = str(uuid.uuid4())
    url = "/api/reports/" + rid
    assert c.put(url, headers=owner, json={**body, "status": "draft"}).status_code == 200
    assert c.delete(url + "?base_revision=1", headers=h).status_code == 200
