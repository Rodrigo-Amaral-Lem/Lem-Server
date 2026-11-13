# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

from app.main import TEMPLATE
"""Ownership transfers revoke stale writes, including photo mutations."""

import uuid
from io import BytesIO
from PIL import Image
from sqlalchemy import select
from app.models import OwnershipTransfer, Report
from test_workflow import setup, upload, answers


def test_owner_transfer_and_offline_revocation(setup):
    c, admin, contract, app = setup
    cid = contract["id"]
    upload(c, admin, cid, True)
    charger = c.get("/api/contracts/" + cid + "/chargers", headers=admin).json()[0]["id"]

    def tech(name, assigned=True):
        email = name + "@example.test"
        password = "TestPassword123!"
        user = c.post(
            "/api/users", headers=admin, json={"name": name, "email": email, "password": password}
        ).json()["id"]
        if assigned:
            assert c.put(f"/api/contracts/{cid}/users/{user}", headers=admin).status_code == 200
        token = c.post("/api/login", json={"email": email, "password": password}).json()["token"]
        return user, {"Authorization": "Bearer " + token}

    a, ha = tech("A")
    b, hb = tech("B")
    outsider, _ = tech("C", False)
    rid = str(uuid.uuid4())
    url = "/api/reports/" + rid
    body = {
        "checklist_version": TEMPLATE["version"], "charger_id": charger,
        "base_revision": 0,
        "payload": {"answers": answers()},
        "status": "draft",
    }
    assert c.put(url, headers=ha, json=body).status_code == 200
    image = BytesIO()
    Image.new("RGB", (5, 5)).save(image, format="PNG")
    pid = str(uuid.uuid4())
    photo = url + "/photos/" + pid

    def attach(h, rev):
        return c.put(
            photo,
            headers=h,
            data={"checkpoint": "EST.01", "base_revision": rev},
            files={"file": ("x.png", image.getvalue())},
        )

    assert attach(ha, 1).status_code == 200
    # An administrator must explicitly take ownership before modifying/closing.
    assert c.put(url, headers=admin, json={**body, "base_revision": 1}).status_code == 403
    assert attach(admin, 1).status_code == 403
    owners = c.get(url + "/owners", headers=admin).json()
    assert (
        a in [u["id"] for u in owners]
        and b in [u["id"] for u in owners]
        and outsider not in [u["id"] for u in owners]
    )
    transfer = {"owner_id": b, "base_revision": 1}
    assert c.post(url + "/owner", headers=ha, json=transfer).status_code == 403
    assert (
        c.post(url + "/owner", headers=admin, json={**transfer, "owner_id": outsider}).status_code
        == 403
    )
    assert (
        c.post(url + "/owner", headers=admin, json={**transfer, "base_revision": 2}).status_code
        == 409
    )
    moved = c.post(url + "/owner", headers=admin, json=transfer)
    assert moved.status_code == 200, moved.text
    assert moved.json()["owner_id"] == b and moved.json()["revision"] == 2
    for status in ["draft", "closed"]:
        assert (
            c.put(url, headers=ha, json={**body, "base_revision": 1, "status": status}).status_code
            == 403
        )
    assert attach(ha, 1).status_code == 403
    assert c.delete(photo + "?base_revision=1", headers=ha).status_code == 403
    assert c.delete(url + "?base_revision=1", headers=ha).status_code == 403
    assert c.get(photo, headers=ha).content == image.getvalue()
    # Transfer back still invalidates the old revision, even for the same owner.
    assert (
        c.post(url + "/owner", headers=admin, json={"owner_id": a, "base_revision": 2}).status_code
        == 200
    )
    assert c.put(url, headers=ha, json={**body, "base_revision": 1}).status_code == 409
    newphoto = url + "/photos/" + str(uuid.uuid4())
    assert (
        c.put(
            newphoto,
            headers=ha,
            data={"checkpoint": "EST.01", "base_revision": 1},
            files={"file": ("x.png", image.getvalue())},
        ).status_code
        == 409
    )
    assert (
        c.put(url, headers=ha, json={**body, "base_revision": 3, "status": "closed"}).status_code
        == 200
    )
    assert (
        c.post(url + "/owner", headers=admin, json={"owner_id": b, "base_revision": 4}).status_code
        == 409
    )
    with app.state.factory() as db:
        history = list(
            db.scalars(
                select(OwnershipTransfer)
                .where(OwnershipTransfer.report_id == rid)
                .order_by(OwnershipTransfer.created)
            )
        )
        assert [(v.old_owner_id, v.new_owner_id) for v in history] == [(a, b), (b, a)]
        assert all(v.actor_id and v.created for v in history)
        assert db.get(Report, rid).owner_id == a


def test_upgrade_existing_database_preserves_reports(setup):
    from app.main import create_app
    from sqlalchemy import inspect

    c, h, contract, app = setup
    upload(c, h, contract["id"], True)
    charger = c.get("/api/contracts/" + contract["id"] + "/chargers", headers=h).json()[0]["id"]
    rid = str(uuid.uuid4())
    payload = {"answers": {"SITE.02": {"value": "OK", "notes": "Não perder"}}}
    assert (
        c.put(
            "/api/reports/" + rid,
            headers=h,
            json={"checklist_version": TEMPLATE["version"], "charger_id": charger, "base_revision": 0, "payload": payload},
        ).status_code
        == 200
    )
    with app.state.factory() as db:
        engine = db.get_bind()
        url = str(engine.url)
        OwnershipTransfer.__table__.drop(engine)
        assert "report_ownership_transfers" not in inspect(engine).get_table_names()
    upgraded = create_app(url)
    with upgraded.state.factory() as db:
        assert db.get(Report, rid).payload == payload
        assert "report_ownership_transfers" in inspect(db.get_bind()).get_table_names()
