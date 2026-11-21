# SPDX-License-Identifier: GPL-3.0-only
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from test_postgres_branding import postgres_setup
from app import work_orders
from app.work_order_retention import cancellation_cutoff, purge_cancelled_orders
from test_workflow import TEMPLATE, add_signature, answers, setup, upload, xlsx


def account(client, admin, name, role="tech"):
    email = name + "@example.invalid"
    response = client.post("/api/users", headers=admin, json={
        "email": email, "name": name, "role": role, "password": "TestAccount-123!"})
    response.raise_for_status()
    auth = client.post("/api/login", json={"email": email, "password": "TestAccount-123!"}).json()
    return response.json()["id"], {"Authorization": "Bearer " + auth["token"], "X-Lem-Work-Orders": "1"}


@pytest.mark.parametrize("backend", ["setup", "postgres_setup"])
def test_lifecycle_and_scoped_access(backend, request, monkeypatch):
    client, admin, first, _ = request.getfixturevalue(backend)
    local = datetime(2026, 7, 1, 6, 59, tzinfo=ZoneInfo("Europe/Lisbon"))
    monkeypatch.setattr(work_orders, "lisbon_now", lambda: local)
    uid, tech = account(client, admin, "joao")
    _, other = account(client, admin, "rui")
    _, observer = account(client, admin, "ana", "admin")
    _, silent = account(client, admin, "ines", "admin")
    second = client.post("/api/contracts", headers=admin, json={
        "company": "Âncora", "year": 2026, "semester": 1, "kind": "MP"}).json()
    for contract in [first, second]:
        upload(client, admin, contract["id"], True, xlsx(("LIS-1", "LIS-2"))).raise_for_status()
    candidates = client.get("/api/work-order-candidates", headers=admin).json()
    def target(contract, label):
        return next(i["target"] for i in candidates if i["contract_id"] == contract["id"] and i["external_id"] == label)
    a, b = target(first, "LIS-1"), target(second, "LIS-1")
    extra = target(first, "LIS-2")
    oid = str(uuid4())
    url = "/api/work-orders/" + oid
    plan = dict(id=oid, revision=0, start="2026-08-01", due="2026-08-02", technicians=[uid], targets=[a, b])
    save = lambda body: client.put(url, headers=admin, json=body)
    save(plan).raise_for_status()
    assert save(plan).json()["revision"] == 1
    def order(user):
        response = client.get("/api/work-orders", headers=user)
        response.raise_for_status()
        return response.json()["orders"][0]
    device = str(uuid4())
    inbox = "/api/work-order-notices/" + device
    def notices(user):
        return client.get(inbox, headers=user).json()
    assert order(tech)["code"] == "OT-202608-0001"
    assert order(admin)["following"] and not order(observer)["following"]
    assert client.get("/api/work-orders", headers=other).json()["orders"] == []
    client.put(url + "/follow", headers=observer, json={"following": True}).raise_for_status()
    assert [n["kind"] for n in notices(tech)] == ["assigned"]
    client.put(inbox + "/" + notices(tech)[0]["id"], headers=tech).raise_for_status()
    assert not notices(tech)
    assert len(client.get("/api/contracts", headers=tech).json()) == 2
    assert client.get("/api/contracts", headers={**tech, "X-Lem-Work-Orders": "0"}).json() == []
    listing = "/api/contracts/" + first["id"] + "/chargers"
    assert [r["id"] for r in client.get(listing, headers=tech).json()] == [a]
    assert client.get(listing + "/" + extra + "/notes", headers=tech).status_code == 403
    rid = str(uuid4())
    report = "/api/reports/" + rid
    body = dict(charger_id=b, checklist_version=TEMPLATE["version"], status="draft", base_revision=0,
                payload={"answers": {}})
    assert client.put(report, headers=tech, json=body).status_code == 403
    updated = {**plan, "start":"2026-07-01", "due":"2026-07-02", "revision":1, "targets":[extra, b, a]}
    save(updated).raise_for_status()
    assert save({**plan, "revision":1}).status_code == 409
    save({**updated, "revision":2, "targets":[a,b]}).raise_for_status()
    assert order(tech)["code"] == "OT-202608-0001" and not notices(tech)
    local = local.replace(hour=7, minute=0)
    reminder, = notices(tech)
    assert reminder["kind"] == "start" and reminder["valid_ms"] == 13 * 3600000
    local = local.replace(hour=20)
    assert not notices(tech)
    local += timedelta(days=3)
    # After the deadline the technician can finish the second item first.
    assert client.put(report, headers=tech, json={**body,"charger_id":extra}).status_code == 403
    body["work_item_id"] = order(tech)["items"][1]["id"]
    client.put(report, headers=tech, json=body).raise_for_status()
    assert client.get(report, headers=other).status_code == 403
    draft, = order(tech)["items"][1]["drafts"]
    assert draft["id"] == rid and draft["owner_id"] == uid and draft["photo_count"] == 0
    assert client.put("/api/reports/" + str(uuid4()), headers=tech, json=body).status_code == 409
    assert uid in [o["id"] for o in client.get(report + "/owners", headers=admin).json()]
    payload = {"answers": answers()}
    add_signature(client, tech, rid, payload)
    draft, = order(observer)["items"][1]["drafts"]
    assert draft["revision"] == 1 and draft["photo_count"] == 1
    response = client.put(report, headers=tech, json={**body, "base_revision":1,"status":"closed","payload":payload})
    response.raise_for_status()
    progress = order(observer)
    assert [r["state"] for r in progress["items"]] == ["pending","closed"]
    assert progress["items"][1]["report_id"] == rid and not progress["completed"]
    assert client.get(report, headers=tech).status_code == 200
    assert client.put("/api/reports/" + str(uuid4()), headers=tech, json=body).status_code == 403
    invalid = url + "/items/" + progress["items"][0]["id"] + "/invalid"
    note = {"revision":progress["revision"], "note":"Ângulo inacessível. Ação adiada — gate locked."}
    assert client.put(invalid, headers=tech, json={**note,"note":"   "}).status_code == 422
    client.put(invalid, headers=tech, json=note).raise_for_status()
    client.put(invalid, headers=tech, json=note).raise_for_status()
    final = order(observer)
    assert final["items"][0]["note"] == note["note"] and final["completed"]
    assert final["items"][0]["report_id"] is None
    assert {n["kind"] for n in notices(admin)} == {"completed","invalid"}
    assert len(notices(observer)) == 2 and notices(silent) == []
    assert a in [c["target"] for c in client.get("/api/work-order-candidates", headers=admin).json()]
    client.put("/api/contracts/" + first["id"] + "/users/" + uid, headers=admin).raise_for_status()
    assert len(client.get(listing, headers=tech).json()) == 2


@pytest.mark.parametrize("backend", ["setup", "postgres_setup"])
def test_competing_plans_serialize_membership_and_monthly_number(backend, request):
    client, admin, contract, _ = request.getfixturevalue(backend)
    uid, _ = account(client, admin, "tech")
    upload(client, admin, contract["id"], True).raise_for_status()
    a, b = client.get("/api/work-order-candidates", headers=admin).json()
    today = work_orders.lisbon_now().date().isoformat()
    def create(target):
        oid = str(uuid4())
        return client.put("/api/work-orders/" + oid, headers=admin, json=dict(
            id=oid, revision=0, start=today, due=today, technicians=[uid], targets=[target]))
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(r.status_code for r in pool.map(create, [a["target"],a["target"]])) == [200,409]
    create(b["target"]).raise_for_status()
    orders = client.get("/api/work-orders", headers=admin).json()["orders"]
    assert sorted(o["code"][-4:] for o in orders) == ["0001","0002"]


def test_operator_is_independent_from_contract_customer(setup):
    from io import BytesIO
    import openpyxl
    from test_contract_notes import book, send_book
    client, admin, contract, _ = setup
    workbook = openpyxl.load_workbook(BytesIO(book()))
    workbook.active["O1"], workbook.active["O2"] = "Operador atual", "Operação Âncora / North"
    data = BytesIO()
    workbook.save(data)
    send_book(client, admin, "/api/contracts/" + contract["id"] + "/import", data.getvalue())
    station, = client.get("/api/stations", headers=admin).json()
    assert station["details"]["operator"] == "Operação Âncora / North"
    uid, tech = account(client, admin, "visita")
    member, = client.get("/api/work-order-candidates", headers=admin).json()
    oid = str(uuid4())
    start = work_orders.lisbon_now().date().isoformat()
    client.put("/api/work-orders/" + oid, headers=admin, json=dict(id=oid, revision=0,
               start=start, due=start, technicians=[uid], targets=[member["target"]])).raise_for_status()
    row, = client.get("/api/work-orders", headers=tech).json()["orders"][0]["items"]
    customer = client.get("/api/contracts", headers=admin).json()[0]["company"]
    assert (row["client"], row["station_id"], row["details"]["operator"]) == (
        customer, station["id"], "Operação Âncora / North")


@pytest.mark.parametrize("backend", ["setup", "postgres_setup"])
def test_cancel_releases_scoped_memberships_and_competing_draft_claims(backend, request):
    client, admin, contract, _ = request.getfixturevalue(backend)
    first, ana = account(client, admin, "ana-turno")
    second, rui = account(client, admin, "rui-turno")
    upload(client, admin, contract["id"], True).raise_for_status()
    target, other = [row["target"] for row in client.get("/api/work-order-candidates", headers=admin).json()]
    today = work_orders.lisbon_now().date()
    tomorrow = (today + timedelta(days=1)).isoformat()
    oid = str(uuid4())
    url = "/api/work-orders/" + oid
    plan = dict(id=oid, revision=0, start=tomorrow, due=tomorrow, technicians=[first, second], targets=[target, other])
    client.put(url, headers=admin, json=plan).raise_for_status()
    assert set(client.get("/api/contracts", headers=ana).json()[0]["order_targets"]) == {target, other}
    assert client.put(url + "/cancel", headers=ana, json={"revision":1}).status_code == 403
    assert client.put(url + "/cancel", headers=admin, json={"revision":2}).status_code == 409
    cancelled = client.put(url + "/cancel", headers=admin, json={"revision":1})
    cancelled.raise_for_status()
    assert client.put(url + "/cancel", headers=admin, json={"revision":1}).json() == cancelled.json()
    assert not client.get("/api/contracts", headers=ana).json()
    assert not client.get("/api/work-orders", headers=ana).json()["orders"]
    archived, = client.get("/api/work-orders?include_cancelled=true", headers=ana).json()["orders"]
    assert archived["cancelled_at"] and not archived["completed"]
    assert all(r["state"] == "cancelled" and not r["available"] for r in archived["items"])
    assert client.put(url, headers=admin, json=plan).status_code == 409
    assert client.put(url + "/follow", headers=admin, json={"following":True}).status_code == 409

    next_id = str(uuid4())
    next_url = "/api/work-orders/" + next_id
    client.put(next_url, headers=admin, json={**plan, "id":next_id, "start":today.isoformat()}).raise_for_status()
    assert client.put(next_url + "/cancel", headers=admin, json={"revision":1}).status_code == 409
    body = dict(charger_id=target, checklist_version=TEMPLATE["version"], base_revision=0,
                status="draft", payload={"answers":{}})
    current, = client.get("/api/work-orders", headers=ana).json()["orders"]
    body["work_item_id"] = current["items"][0]["id"]
    def claim(user):
        return client.put("/api/reports/" + str(uuid4()), headers=user, json=body).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, (ana, rui))) == [200, 409]
    current, = client.get("/api/work-orders", headers=ana).json()["orders"]
    draft, = current["items"][0]["drafts"]
    assert draft["owner_id"] in (first, second) and draft["owner_name"]
    assert client.get("/api/reports/" + draft["id"], headers=rui).status_code == 200


@pytest.mark.parametrize("backend", ["setup", "postgres_setup"])
def test_cancelled_order_expires_after_24_elapsed_hours(backend, request, monkeypatch):
    from sqlalchemy import select
    from app.models import (DeletedWorkOrder, ExpiredVisitDraft, NoticeReceipt, WorkAssignee, WorkItem,
                            WorkItemDraft, WorkNotice, WorkOrder, WorkOrderCancellation)

    client, admin, contract, app = request.getfixturevalue(backend)
    # This interval crosses the spring change of Lisbon's UTC offset.
    cancelled_at = datetime(2027, 3, 27, 12, tzinfo=timezone.utc)
    clock = cancelled_at
    monkeypatch.setattr(work_orders, "now", lambda: clock.isoformat())
    monkeypatch.setattr(work_orders, "lisbon_now", lambda: clock.astimezone(ZoneInfo("Europe/Lisbon")))
    monkeypatch.setattr(work_orders, "cancellation_cutoff", lambda: cancellation_cutoff(clock))
    uid, tech = account(client, admin, "visita")
    client.put(f"/api/contracts/{contract['id']}/users/{uid}", headers=admin).raise_for_status()
    upload(client, admin, contract["id"], True).raise_for_status()
    a, b = [r["target"] for r in client.get("/api/work-order-candidates", headers=admin).json()]
    def plan(oid, target, day):
        return client.put("/api/work-orders/" + oid, headers=admin, json=dict(id=oid, revision=0,
                          start=day, due=day, technicians=[uid], targets=[target]))
    def history():
        result = client.get("/api/work-orders?include_cancelled=true", headers=tech)
        result.raise_for_status()
        return {r["id"]: r for r in result.json()["orders"]}

    first, kept = str(uuid4()), str(uuid4())
    plan(first, a, "2027-03-30").raise_for_status()
    plan(kept, b, "2027-03-27").raise_for_status()
    origin = history()[first]["items"][0]["id"]
    report = str(uuid4())
    endpoint = "/api/reports/" + report
    payload = dict(charger_id=a, work_item_id=origin, checklist_version=TEMPLATE["version"], base_revision=0,
                   status="draft", payload={"answers":{}})
    client.put(endpoint, headers=tech, json=payload).raise_for_status()
    signature = add_signature(client, tech, report, {"answers": answers()})
    path = "/api/work-orders/" + first + "/cancel"
    client.put(path, headers=admin, json={"revision":1}).raise_for_status()
    code = history()[first]["code"]
    # Old installations may have remaining events and acknowledgements for a cancelled order.
    event_id, device = str(uuid4()), str(uuid4())
    with app.state.factory.begin() as db:
        db.add(WorkNotice(id=event_id, order_id=first, user_id=uid, kind="invalid", message="Teste"))
        db.flush()
        db.add(NoticeReceipt(notice_id=event_id, device=device))
    clock += timedelta(hours=24) - timedelta(microseconds=1)
    assert first in history() and purge_cancelled_orders(app.state.factory, clock) == 0
    assert client.put(path, headers=admin, json={"revision":1}).json()["cancelled_at"] == cancelled_at.isoformat()
    clock += timedelta(microseconds=1)
    assert set(history()) == {kept}
    assert client.put(path, headers=admin, json={"revision":1}).status_code == 404
    assert plan(first, a, "2027-03-30").status_code == 410
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _: purge_cancelled_orders(app.state.factory, clock), range(2))) == 1
    with app.state.factory() as db:
        assert db.get(WorkOrder, first) is None and db.get(WorkOrderCancellation, first) is None
        assert db.get(WorkItem, origin) is None and db.get(WorkItemDraft, (origin, report)) is None
        assert db.get(WorkAssignee, (first, uid)) is None and db.get(NoticeReceipt, (event_id, device)) is None
        assert db.scalar(select(WorkNotice.id).where(WorkNotice.order_id == first)) is None
        assert db.get(DeletedWorkOrder, first).code == code
        assert db.get(ExpiredVisitDraft, report).item_id == origin
    assert plan(first, a, "2027-03-30").status_code == 410
    replacement = str(uuid4())
    plan(replacement, a, "2027-03-28").raise_for_status()
    current = history()[replacement]
    assert current["code"] == "OT-202703-0003" and current["items"][0]["drafts"] == []
    recovered = client.get(endpoint, headers=tech).json()
    assert recovered["status"] == "draft" and recovered["work_item_id"] == origin and recovered["work_item_expired"]
    photo = client.get(endpoint + "/photos/" + signature, headers=tech)
    assert photo.is_success and photo.content.startswith(b"\x89PNG")
    assert client.put(endpoint, headers=tech, json={**payload, "base_revision":1, "work_item_id":None}).status_code == 403
    assert client.put(endpoint, headers=tech, json={**payload, "base_revision":1,
                      "work_item_id":current["items"][0]["id"]}).status_code == 403
    assert purge_cancelled_orders(app.state.factory, clock) == 0


@pytest.mark.parametrize("backend", ["setup", "postgres_setup"])
def test_historical_draft_recovery_uses_server_save_time(backend, request):
    from sqlalchemy import delete
    from app.models import DataMigration, WorkItemDraft, database
    from test_admin_cli import command

    client, admin, contract, app = request.getfixturevalue(backend)
    owner, tech = account(client, admin, "andrea-campo")
    cid = contract["id"]
    upload(client, admin, cid, True).raise_for_status()
    target = client.get("/api/work-order-candidates", headers=admin).json()[0]["target"]
    client.put(f"/api/contracts/{cid}/users/{owner}", headers=admin).raise_for_status()
    today = work_orders.lisbon_now()
    def save_draft(start):
        rid = str(uuid4())
        client.put("/api/reports/" + rid, headers=tech, json=dict(
            charger_id=target, base_revision=0, checklist_version=TEMPLATE["version"], status="draft",
            payload={"answers":{}, "started":start.isoformat()})).raise_for_status()
        return rid
    earlier = save_draft(today + timedelta(days=1))
    oid = str(uuid4())
    url = "/api/work-orders/" + oid
    day = today.date().isoformat()
    client.put(url, headers=admin, json=dict(id=oid, revision=0, start=day, due=day,
               technicians=[owner], targets=[target])).raise_for_status()
    item, = client.get("/api/work-orders", headers=tech).json()["orders"][0]["items"]
    client.put(f"{url}/items/{item['id']}/invalid", headers=tech,
               json={"revision":1, "note":"Acesso suspenso / no access"}).raise_for_status()
    later = save_draft(today - timedelta(days=1))
    command(app, ["users", "unassign", owner, cid])

    # Simulate the earlier schema's missing associations, then perform an ordinary startup.
    with app.state.factory() as db:
        db.execute(delete(WorkItemDraft))
        db.execute(delete(DataMigration).where(DataMigration.name == "work-order-recovery-drafts-v1"))
        db.commit()
        database_url = db.get_bind().url.render_as_string(hide_password=False)
    engine, _ = database(database_url)
    engine.dispose()
    assert client.get("/api/reports/" + earlier, headers=tech).status_code == 200
    assert client.get("/api/reports/" + later, headers=tech).status_code == 403
    recovered, = client.get("/api/work-orders", headers=tech).json()["orders"][0]["items"][0]["drafts"]
    assert recovered["id"] == earlier
