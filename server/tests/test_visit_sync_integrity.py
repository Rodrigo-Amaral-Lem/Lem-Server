# SPDX-License-Identifier: GPL-3.0-only
"""Ordinary field writes, late acknowledgements and office imports on both supported databases."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO
from threading import Event
from uuid import uuid4

import openpyxl
import pytest
from PIL import Image
from sqlalchemy import event

from app import main, work_orders
from test_contract_notes import book
from test_postgres_branding import postgres_setup
from test_work_orders import account
from test_workflow import TEMPLATE, add_signature, answers, setup, upload


@pytest.fixture(params=["setup", "postgres_setup"], ids=["sqlite", "postgres"])
def backend(request):
    return request.getfixturevalue(request.param)


def membership(client, admin, contract):
    upload(client, admin, contract["id"], True).raise_for_status()
    return client.get(f"/api/contracts/{contract['id']}/chargers", headers=admin).json()[0]["id"]


def report(target):
    return dict(charger_id=target, checklist_version=TEMPLATE["version"], status="draft", base_revision=0,
                payload={"answers": answers(), "started": "2026-11-12T10:00:00Z"})


@pytest.fixture
def signed(backend):
    client, admin, contract, app = backend
    target = membership(client, admin, contract)
    body = report(target)
    rid = str(uuid4())
    path = "/api/reports/" + rid
    client.put(path, json=body, headers=admin).raise_for_status()
    add_signature(client, admin, rid, body["payload"], revision=1)
    body["base_revision"] = 1
    client.put(path, json=body, headers=admin).raise_for_status()
    body["base_revision"] = 2
    return client, admin, app, path, body


def test_acknowledges_only_its_own_transaction(signed):
    client, admin, app, path, body = signed
    note = "Medição Âncora / second visit"
    other = {}

    def accept_another_edit(session):
        if other:
            return
        other["started"] = True
        received = client.get(path, headers=admin).json()
        assert received["revision"] == 3
        received["payload"]["answers"]["SITE.01"]["notes"] = note
        reply = client.put(path, headers=admin, json={**body, "base_revision": 3, "payload": received["payload"]})
        reply.raise_for_status()
        other["revision"] = reply.json()["revision"]

    event.listen(app.state.factory, "after_commit", accept_another_edit)
    try:
        first = client.put(path, json=body, headers=admin)
        first.raise_for_status()
    finally:
        event.remove(app.state.factory, "after_commit", accept_another_edit)
    assert first.json()["revision"] == 3 and other["revision"] == 4
    assert first.json()["payload"]["answers"]["SITE.01"]["notes"] != note
    assert client.put(path, json={**body, "base_revision": 3}, headers=admin).status_code == 409
    assert client.get(path, headers=admin).json()["payload"]["answers"]["SITE.01"]["notes"] == note


def test_closed_manifest_cannot_gain_an_inflight_photo(signed, monkeypatch):
    client, admin, _, path, body = signed
    content = BytesIO()
    Image.new("RGB", (12, 12), "orange").save(content, "PNG")
    inspect = main.inspect_evidence
    closed = {}

    def close_before_accepting(data):
        reply = client.put(path, headers=admin, json={**body, "status": "closed"})
        reply.raise_for_status()
        closed.update(reply.json())
        return inspect(data)

    monkeypatch.setattr(main, "inspect_evidence", close_before_accepting)
    response = client.put(path + "/photos/" + str(uuid4()), headers=admin,
                          data={"checkpoint": "SITE.01", "base_revision": 2},
                          files={"file": ("Âncora.png", content.getvalue())})
    assert response.status_code == 409
    reread = client.get(path, headers=admin).json()
    assert closed["photos"] == reread["photos"] and len(reread["photos"]) == 1
    assert closed["revision"] == reread["revision"] == 3


def test_ordinary_closure_compares_authoritative_instants(signed):
    client, admin, _, path, body = signed
    body["status"] = "closed"
    # Neither a changed start in the payload nor an offset can disguise an earlier finish.
    body["payload"].update(started="2026-11-12T07:00:00Z", finished="2026-11-12T10:59:59+01:00")
    assert client.put(path, json=body, headers=admin).status_code == 422
    draft = client.get(path, headers=admin).json()
    assert draft["status"] == "draft" and draft["revision"] == 2 and len(draft["photos"]) == 1
    body["payload"].update(started=draft["started"], finished="2026-11-12T11:00:00+01:00")
    client.put(path, json=body, headers=admin).raise_for_status()


def test_accented_payload_size_and_same_id_retry(signed):
    client, admin, _, path, body = signed
    base = client.get(path, headers=admin).json()
    for section in base["template"]["sections"]:
        for item in section["items"]:
            if item["kind"] == "notes":
                body["payload"]["answers"][item["id"]]["notes"] = ("Ação Âncora / field " * 1800)[:28000]
    client.put(path, json=body, headers=admin).raise_for_status()
    body["base_revision"] = 3
    for answer in body["payload"]["answers"].values():
        if len(answer.get("notes", "")) > 10000:
            answer["notes"] = "Âç" * 16000
    assert client.put(path, json=body, headers=admin).status_code == 413
    assert client.get(path, headers=admin).json()["revision"] == 3
    fresh = "/api/reports/" + str(uuid4())
    assert client.put(fresh, json={**body, "base_revision": 0}, headers=admin).status_code == 413
    body["payload"] = base["payload"]
    client.put(path, json={**body, "status": "closed"}, headers=admin).raise_for_status()


def plan(client, admin, users, target):
    day = work_orders.lisbon_now().date().isoformat()
    oid = str(uuid4())
    path = "/api/work-orders/" + oid
    payload = dict(id=oid, start=day, due=day, technicians=users, targets=[target], revision=0)
    client.put(path, headers=admin, json=payload).raise_for_status()
    orders = client.get("/api/work-orders", headers=admin).json()["orders"]
    return path, next(row for row in orders if row["id"] == oid)["items"][0]["id"], payload


@pytest.mark.parametrize("same_crew", [True, False])
def test_stale_offline_and_recovery_visits_do_not_claim_replanned_work(backend, same_crew):
    client, admin, contract, _ = backend
    target = membership(client, admin, contract)
    first, ana = account(client, admin, "ana")
    second, rui = account(client, admin, "rui")
    third, teo = account(client, admin, "teo")
    order, item, _ = plan(client, admin, [first, second], target)
    body = dict(report(target), work_item_id=item)
    body["payload"]["answers"]["FIN.03"] = {"value": "", "notes": "Portão Âmbar / North"}
    path = "/api/reports/" + str(uuid4())
    client.put(path, headers=ana, json=body).raise_for_status()
    client.put(f"{order}/items/{item}/invalid", headers=rui,
               json={"revision": 1, "note": "Acesso impedido / no entry"}).raise_for_status()
    other_owner, other_tech = (second, rui) if same_crew else (third, teo)
    _, new_item, _ = plan(client, admin, [first, other_owner], target)
    assert new_item != item
    current = next(i for o in client.get("/api/work-orders", headers=other_tech).json()["orders"] for i in o["items"] if i["id"] == new_item)
    assert current["id"] == new_item and current["drafts"] == []
    assert client.put(path, headers=ana, json={**body, "base_revision": 1, "status": "closed"}).status_code == 403
    assert client.put("/api/reports/" + str(uuid4()), headers=ana, json=body).status_code == 409
    assert client.get(path, headers=other_tech).status_code == 403
    recovered = client.get(path, headers=ana).json()
    assert recovered["work_item_id"] == item and recovered["payload"]["answers"]["FIN.03"]["notes"] == "Portão Âmbar / North"

    def claim(user):
        return client.put("/api/reports/" + str(uuid4()), headers=user, json={**report(target), "work_item_id": new_item})
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(claim, [ana, other_tech]))
    assert sorted(reply.status_code for reply in result) == [200, 409]
    later = next(reply.json() for reply in result if reply.status_code == 200)
    assert later["id"] != recovered["id"] and later["work_item_id"] == new_item


def test_contract_import_replay_and_connector_validation(backend):
    client, admin, contract, _ = backend
    path = "/api/contracts/" + contract["id"]
    headers = {**admin, "X-Lem-Client-Platform": "windows"}
    data = book("Entrada norte / North")
    def send(route, data, commit, **fields):
        return client.post(route, headers=headers, files={"file": ("Âncora.xlsx", data)},
                           data={"mapping": '{"sheet":"Stations","columns":{}}', "commit": commit, **fields})
    operation = send(path + "/import", data, False).json()["operation_id"]
    first = send(path + "/import", data, True, operation_id=operation)
    first.raise_for_status()
    row, = client.get(path + "/chargers", headers=admin).json()
    notes = path + "/chargers/" + row["id"] + "/notes"
    client.put(notes, headers=admin, json={"notes": "Portão sul / South", "base_revision": 1}).raise_for_status()
    assert send(path + "/import", data, True, operation_id=operation).json() == first.json()
    assert client.get(notes, headers=admin).json()["contract_notes"] == "Portão sul / South"
    another = send(path + "/import", data, False).json()["operation_id"]
    assert another != operation
    send(path + "/import", data, True, operation_id=another).raise_for_status()
    assert client.get(notes, headers=admin).json()["notes_revision"] == 3
    workbook = openpyxl.load_workbook(BytesIO(data))
    workbook.active["N2"] = "0 x AC Type 2"
    bad = BytesIO()
    workbook.save(bad)
    for url in (path + "/import", "/api/stations/import"):
        preview = send(url, bad.getvalue(), False).json()
        assert preview["count"] == 0 and preview["errors"][0]["cell"] == "N2"
        assert send(url, bad.getvalue(), True).status_code == 422
    client.put("/api/reports/" + str(uuid4()), headers=admin, json=report(row["id"])).raise_for_status()

def test_started_order_cannot_be_postponed_to_cancel(backend):
    client, admin, contract, _ = backend
    target = membership(client, admin, contract)
    owner, tech = account(client, admin, "turno")
    path, item, body = plan(client, admin, [owner], target)
    other = next(row["id"] for row in client.get(f"/api/contracts/{contract['id']}/chargers", headers=admin).json() if row["id"] != target)
    body.update(revision=1, targets=[target, other])
    client.put(path, headers=admin, json=body).raise_for_status()
    saved = "/api/reports/" + str(uuid4())
    client.put(saved, headers=tech, json={**report(target), "work_item_id": item}).raise_for_status()
    # An independent full-contract administrator closes the other task.
    rid = str(uuid4())
    other_path = "/api/reports/" + rid
    finished = report(other)
    client.put(other_path, headers=admin, json=finished).raise_for_status()
    add_signature(client, admin, rid, finished["payload"], revision=1)
    client.put(other_path, headers=admin, json={**finished, "base_revision": 1, "status": "closed"}).raise_for_status()
    before, = client.get("/api/work-orders", headers=tech).json()["orders"]
    revision = before["revision"]
    assert before["items"][1]["report_id"] == rid
    tomorrow = (work_orders.lisbon_now().date() + timedelta(days=1)).isoformat()
    assert client.put(path, headers=admin, json={**body, "revision": revision, "start": tomorrow, "due": tomorrow}).status_code == 409
    assert client.put(path + "/cancel", headers=admin, json={"revision": revision}).status_code == 409
    client.put(path, headers=admin, json={**body, "revision": revision, "due": tomorrow}).raise_for_status()
    order, = client.get("/api/work-orders", headers=tech).json()["orders"]
    assert order["started"] and order["start"] == body["start"]
    assert order["items"][0]["drafts"][0]["id"] == saved.split("/")[-1] and order["items"][1]["report_id"] == rid
    client.get(saved, headers=tech).raise_for_status()


@pytest.mark.parametrize("office,phase", [("notes", "draft"), ("import", "first"), ("import", "closed")])
def test_office_and_field_save_in_catalogue_order(backend, monkeypatch, office, phase):
    client, admin, contract, _ = backend
    target = membership(client, admin, contract)
    cid = contract["id"]
    owner, tech = account(client, admin, "campo")
    _, item, _ = plan(client, admin, [owner], target)
    rid = str(uuid4())
    path = "/api/reports/" + rid
    body = {**report(target), "work_item_id": item}
    if phase != "first":
        client.put(path, headers=tech, json=body).raise_for_status()
        body["base_revision"] = 1
    if phase == "closed":
        add_signature(client, tech, rid, body["payload"], revision=1)
        body["status"] = "closed"

    # The office request reaches the first lock before the field save. Let the
    # field transaction take that lock first, then allow both to finish.
    office_entered, field_locked = Event(), Event()
    lock = main.lock_catalogue

    def interleaved(db):
        if db.info["client_platform"] == "windows":
            office_entered.set()
            assert field_locked.wait(10)
            lock(db)
        else:
            assert office_entered.wait(10)
            lock(db)
            field_locked.set()

    monkeypatch.setattr(main, "lock_catalogue", interleaved)
    headers = {**admin, "X-Lem-Client-Platform": "windows"}
    def office_save():
        if office == "notes":
            return client.put(f"/api/contracts/{cid}/chargers/{target}/notes", headers=headers,
                              json={"base_revision": 0, "notes": "Portão Âmbar / Gate"})
        return upload(client, headers, cid, True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        saved = pool.submit(office_save)
        client.put(path, headers={**tech, "X-Lem-Client-Platform": "android"}, json=body).raise_for_status()
        saved.result(timeout=20).raise_for_status()
    assert client.get(path, headers=tech).json()["status"] == body["status"]
    if office == "notes":
        assert client.get(f"/api/contracts/{cid}/chargers/{target}/notes", headers=tech).json()["contract_notes"] == "Portão Âmbar / Gate"


def test_export_listing_includes_closed_detached_history(signed):
    client, admin, _, path, body = signed
    closed = client.put(path, headers=admin, json={**body, "status": "closed"}).json()
    cid = closed["contract_id"]
    root = "/api/contracts/" + cid + "/chargers"
    owner, tech = account(client, admin, "exportador")
    client.put(f"/api/contracts/{cid}/users/{owner}", headers=admin).raise_for_status()
    client.delete(root + "/" + body["charger_id"], headers=admin).raise_for_status()
    assert all(row["id"] != body["charger_id"] for row in client.get(root, headers=admin).json())
    for user in (admin, tech):
        history = client.get(root + "?include_detached=true", headers=user).json()
        assert any(row["id"] == body["charger_id"] and any(r["id"] == closed["id"] for r in row["reports"]) for row in history)

