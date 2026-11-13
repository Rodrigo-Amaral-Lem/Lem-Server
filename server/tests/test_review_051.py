# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

from app.main import TEMPLATE
"""Re-imports and unique-key races across separate administrative operations."""

import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import Charger, Contract, ImportProfile, Photo, Report, Assignment
from app.security import digest
from test_workflow import setup, upload, xlsx, MAPPING
from test_admin_cli import command
from test_contract_trash import populated


def test_reimport_preserves_fields_not_supplied_by_the_workbook(setup):
    c, h, contract, app = setup
    cid = contract["id"]
    assert upload(c, h, cid, True).status_code == 200
    charger = c.get(f"/api/contracts/{cid}/chargers", headers=h).json()[0]
    command(
        app,
        [
            "chargers",
            "update",
            cid,
            charger["id"],
            "--set",
            "latitude=40.12",
            "--set",
            "longitude=-8.5",
            "--set",
            "postal_code=1000-001",
            "--set",
            "connectors=CCS2, CCS2",
        ],
    )
    assert upload(c, h, cid, True).status_code == 200
    row = next(
        x
        for x in c.get(f"/api/contracts/{cid}/chargers", headers=h).json()
        if x["id"] == charger["id"]
    )
    assert row["details"]["latitude"] == 40.12
    assert row["details"]["postal_code"] == "1000-001"
    assert row["details"]["connectors"] == "CCS2, CCS2"
    rid = str(uuid.uuid4())
    result = c.put(
        f"/api/reports/{rid}",
        headers=h,
        json=dict(checklist_version=TEMPLATE["version"],charger_id=charger["id"], base_revision=0, payload={}),
    )
    assert result.status_code == 200
    assert result.json()["template"]["load_test_connectors"] == ["CCS2", "CCS2"]


def test_contract_created_after_precheck_returns_conflict(setup, monkeypatch):
    c, h, _, app = setup
    original = Session.commit
    raced = []

    def commit(db):
        if not raced and any(isinstance(v, Contract) and v.company == "Race" for v in db.new):
            raced.append(True)
            with app.state.factory() as other:
                other.add(
                    Contract(
                        id=str(uuid.uuid4()),
                        company="Race",
                        year=2026,
                        semester=1,
                        kind="TRE",
                        name="2026_1ºS_Race_TRE",
                    )
                )
                original(other)
        return original(db)

    monkeypatch.setattr(Session, "commit", commit)
    result = c.post(
        "/api/contracts", headers=h, json=dict(company="Race", year=2026, semester=1, kind="TRE")
    )
    assert raced
    assert result.status_code == 409


def test_profile_created_by_another_contract_does_not_break_import(setup, monkeypatch):
    c, h, contract, app = setup
    original = Session.get
    original_execute = Session.execute
    raced = []

    def get(db, entity, *args, **kwargs):
        result = original(db, entity, *args, **kwargs)
        if entity is ImportProfile and result is None and not raced:
            raced.append(True)
            # Deterministic same-transaction interleaving represents the unique
            # row already won by another import before our pending INSERT.
            db.connection().execute(
                ImportProfile.__table__.insert().values(company="Galp", mapping={"winner": True})
            )
        return result

    def execute(db, statement, *args, **kwargs):
        table = getattr(statement, "table", None)
        if (
            not raced
            and getattr(statement, "is_insert", False)
            and getattr(table, "name", None) == "import_profiles"
        ):
            raced.append(True)
            db.connection().execute(
                ImportProfile.__table__.insert().values(company="Galp", mapping={"winner": True})
            )
        return original_execute(db, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "get", get)
    monkeypatch.setattr(Session, "execute", execute)
    result = upload(c, h, contract["id"], True)
    assert result.status_code == 200, result.text
    assert raced
    with app.state.factory() as db:
        assert db.get(ImportProfile, "Galp").mapping == MAPPING


def test_photo_id_claimed_in_another_report_returns_conflict(setup, monkeypatch):
    c, h, _, app = setup
    _, charger, rid, pid, data = populated(setup)
    other_id = str(uuid.uuid4())
    assert (
        c.put(
            f"/api/reports/{other_id}",
            headers=h,
            json=dict(checklist_version=TEMPLATE["version"],charger_id=charger, base_revision=0, payload={}),
        ).status_code
        == 200
    )
    original = Session.commit
    raced = []
    new_pid = str(uuid.uuid4())

    def commit(db):
        pending = next((x for x in db.new if isinstance(x, Photo) and x.id == new_pid), None)
        if pending and not raced:
            raced.append(True)
            with app.state.factory() as other:
                other.add(
                    Photo(
                        id=new_pid,
                        report_id=other_id,
                        checkpoint="EST.01",
                        filename="other.png",
                        mime="image/png",
                        digest=pending.digest,
                        data=pending.data,
                    )
                )
                original(other)
        return original(db)

    monkeypatch.setattr(Session, "commit", commit)
    result = c.put(
        f"/api/reports/{rid}/photos/{new_pid}",
        headers=h,
        data={"checkpoint": "EST.01", "base_revision": "1"},
        files={"file": ("capture.png", data, "image/png")},
    )
    assert raced
    assert result.status_code == 409
    with app.state.factory() as db:
        assert db.get(Photo, new_pid).report_id == other_id


def test_simultaneous_assignment_is_idempotent(setup, monkeypatch):
    c, h, contract, app = setup
    user = c.post(
        "/api/users",
        headers=h,
        json=dict(email="assigned@lem.pt", name="Tech", password="StrongPassword123!", role="tech"),
    ).json()["id"]
    original_get, original_execute = Session.get, Session.execute
    raced = []

    def insert_winner(db):
        raced.append(True)
        db.connection().execute(
            Assignment.__table__.insert().values(user_id=user, contract_id=contract["id"])
        )

    def get(db, entity, *args, **kwargs):
        result = original_get(db, entity, *args, **kwargs)
        if entity is Assignment and result is None and not raced:
            insert_winner(db)
        return result

    def execute(db, statement, *args, **kwargs):
        table = getattr(statement, "table", None)
        if (
            not raced
            and getattr(statement, "is_insert", False)
            and getattr(table, "name", None) == "assignments"
        ):
            insert_winner(db)
        return original_execute(db, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "get", get)
    monkeypatch.setattr(Session, "execute", execute)
    result = c.put(f'/api/contracts/{contract["id"]}/users/{user}', headers=h)
    assert raced
    assert result.status_code == 200, result.text
    assert c.put(f'/api/contracts/{contract["id"]}/users/{user}', headers=h).status_code == 200


def test_mapped_blank_still_clears_while_other_fields_and_frozen_reports_survive(setup):
    c, h, contract, app = setup
    cid, charger, rid, pid, data = populated(setup)
    before = c.get(f"/api/reports/{rid}", headers=h).json()
    command(
        app,
        ["chargers", "update", cid, charger, "--set", "latitude=40.12", "--set", "longitude=-8.5"],
    )
    from io import BytesIO
    import openpyxl, json

    workbook = openpyxl.load_workbook(BytesIO(xlsx()))
    workbook.active.cell(1, 7, "Latitude")
    output = BytesIO()
    workbook.save(output)
    mapping = {**MAPPING, "columns": {**MAPPING["columns"], "latitude": "Latitude"}}
    response = c.post(
        f"/api/contracts/{cid}/import",
        headers=h,
        files={"file": ("stations.xlsx", output.getvalue())},
        data={"mapping": json.dumps(mapping), "commit": "true"},
    )
    assert response.status_code == 200, response.text
    with app.state.factory() as db:
        details = db.get(Charger, charger).details
        assert details["latitude"] == "", "Mapped blank is still an explicit imported update"
        assert details["longitude"] == -8.5
        assert db.get(Photo, pid).data == data
    assert c.get(f"/api/reports/{rid}", headers=h).json() == before
