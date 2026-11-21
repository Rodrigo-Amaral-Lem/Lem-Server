# SPDX-License-Identifier: GPL-3.0-only
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import BytesIO
import json
from uuid import uuid4
from zipfile import ZipFile
from zoneinfo import ZoneInfo

import openpyxl
import pytest
from pypdf import PdfReader
from sqlalchemy import select, func

from app import interventions, work_orders
from app.models import CorrectiveCoverage, Intervention
from test_postgres_branding import postgres_setup
from test_work_orders import account
from test_workflow import setup, upload, xlsx, TEMPLATE, answers, add_signature


def sheet(values):
    book = openpyxl.Workbook()
    for value in values:
        book.active.append([value, "Coluna ignorada"])
    if values and values[0] == 7:
        book.active["A1"].number_format = "000"
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def import_contract(client, admin, ids, *, name="Reparação North", start="2028-01-01", end="2028-12-31", key=None):
    return client.put("/api/corrective-contracts/" + (key or str(uuid4())), headers=admin,
                      data=dict(company="Âncora", name=name, start=start, end=end),
                      files={"file": ("IDs.xlsx", sheet(ids))})


@pytest.fixture
def today(monkeypatch):
    instant = datetime(2028, 2, 29, 12, tzinfo=ZoneInfo("Europe/Lisbon"))
    monkeypatch.setattr(interventions, "lisbon_now", lambda: instant)
    monkeypatch.setattr(work_orders, "lisbon_now", lambda: instant)
    return instant.date()


def test_import_atomic_membership_dates_and_retries(setup, today):
    client, admin, preventive, app = setup
    upload(client, admin, preventive["id"], True, xlsx(("007", "Ação-Â"))).raise_for_status()
    key = str(uuid4())
    for values, error in [(["007", "Outro"], "Linha 2"), (["007", "007"], "repetido"),
                          (["=1+2"], "Linha 1"), ([None], "coluna A")]:
        response = import_contract(client, admin, values, key=key)
        assert response.status_code == 422 and error in response.json()["detail"]
    for start, end in [("2027-12-31", "2028-01-01"), ("2028-10-02", "2028-10-01"),
                       ("2028-02-29", "2030-03-01")]:
        assert import_contract(client, admin, ["007"], key=key, start=start, end=end).status_code == 422
    assert client.get("/api/corrective-contracts", headers=admin).json() == []
    def create():
        return import_contract(client, admin, [7, None, "Ação-Â"], start="2028-02-29", end="2030-02-28", key=key)
    first = create()
    first.raise_for_status()
    assert first.json() == create().json()
    assert first.json()["name"] == "2028_Âncora_Reparação North" and first.json()["count"] == 2
    assert import_contract(client, admin, ["007"], key=key).status_code == 409
    with app.state.factory() as db:
        assert db.scalar(select(func.count()).select_from(CorrectiveCoverage)) == 2


@pytest.mark.parametrize("backend", ["setup", "postgres_setup"])
def test_mixed_order_individual_grants_and_distinct_visits(backend, request, today):
    client, admin, preventive, app = request.getfixturevalue(backend)
    label = "Ação-Â-North"
    upload(client, admin, preventive["id"], True, xlsx((label, "SUL-9"))).raise_for_status()
    station = next(s for s in client.get("/api/stations", headers=admin).json() if s["external_id"] == label)
    old = import_contract(client, admin, [label]).json()
    current = import_contract(client, admin, [label], name="Assistência", start=today.isoformat(), end=today.isoformat()).json()
    future = import_contract(client, admin, [label], name="Amanhã", start="2028-03-01").json()
    past = import_contract(client, admin, [label], name="Ontem", end="2028-02-28").json()
    other_site = import_contract(client, admin, ["SUL-9"], name="Sul").json()
    applicable = client.get("/api/corrective-contracts", headers=admin, params={"station_id": station["id"]}).json()
    assert [c["id"] for c in applicable] == [current["id"], old["id"]]
    uid, tech = account(client, admin, "angela")
    _, outsider = account(client, admin, "outro")
    def create(key=None, user=admin, **fields):
        return client.put("/api/interventions/" + (key or str(uuid4())), headers=user,
                          json={"kind": "corrective", "station_id": station["id"], **fields})
    for bad in [future, past, other_site]:
        assert create(contract_id=bad["id"]).status_code == 422
    assert create(user=tech).status_code == 403
    assert create(user=outsider, kind="commissioning").status_code == 404
    assert create(kind="commissioning", contract_id=current["id"]).status_code == 422
    key = str(uuid4())
    notes = "Ângela, verificar ligação e tensão. Check inlet."
    direct = create(key, notes=notes, technician=uid)
    direct.raise_for_status()
    assert create(key, notes=notes, technician=uid).json() == direct.json()
    assert create(key, contract_id=None).status_code == 409
    planned = create(contract_id=old["id"]).json()["id"]
    sporadic = create(contract_id=None).json()["id"]
    commissioning = create(user=tech, kind="commissioning").json()["id"]
    ordered_commissioning = create(kind="commissioning").json()
    commissioning_target = ordered_commissioning["id"]
    def jobs(user):
        rows = client.get("/api/contracts", headers={**user, "X-Lem-Work-Orders":"1"})
        rows.raise_for_status()
        return {r["id"]: r["intervention"] for r in rows.json() if "intervention" in r}
    all_jobs = jobs(admin)
    assert [c["id"] for c in client.get("/api/contracts", headers=admin).json()] == [preventive["id"]]
    assert all_jobs[key]["contract"]["id"] == current["id"] and all_jobs[key]["notes"] == notes
    assert all_jobs[planned]["contract"]["id"] == old["id"]
    assert all_jobs[sporadic]["contract"] is None and all_jobs[commissioning]["contract"] is None
    assert all_jobs[commissioning_target]["technician"] is None
    assert set(jobs(tech)) == {key, commissioning} and jobs(outsider) == {}
    assert client.put(f"/api/contracts/{key}/users/{uid}", headers=admin).status_code == 409
    assert client.put(f"/api/interventions/{sporadic}/share", headers=admin, json={"previous":None, "technician":uid}).is_success
    assert client.put(f"/api/interventions/{sporadic}/share", headers=admin, json={"previous":None, "technician":None}).status_code == 409
    notes_path = f"/api/contracts/{planned}/chargers/{planned}/notes"
    assert client.get(notes_path, headers=tech).status_code == 403
    client.put(notes_path, headers=admin, json={"base_revision":0,"notes":"Ação planeada / planned."}).raise_for_status()
    choices = client.get("/api/work-order-candidates", headers=admin).json()
    base = next(c["target"] for c in choices if c["external_id"] == label and c["kind"] == "preventive")
    assert {c["target"] for c in choices if c["kind"] == "corrective"} == {key, planned, sporadic}
    candidate = next((c for c in choices if c["target"] == commissioning_target), None)
    assert candidate == {
        "target": commissioning_target, "contract_id": commissioning_target, "contract": None,
        "external_id": label, "kind": "commissioning", "reference": ordered_commissioning["reference"],
    }
    order_id = str(uuid4())
    plan = dict(id=order_id, revision=0, start=today.isoformat(), due=today.isoformat(), technicians=[uid], targets=[base, planned])
    order_path = "/api/work-orders/" + order_id
    client.put(order_path, headers=admin, json=plan).raise_for_status()

    def order():
        return client.get("/api/work-orders", headers=tech).json()["orders"][0]

    original_rows = order()["items"]
    edited = {**plan, "revision": order()["revision"], "targets": [base, planned, commissioning_target]}
    client.put(order_path, headers=admin, json=edited).raise_for_status()
    duplicate_order = str(uuid4())
    duplicate = {**plan, "id": duplicate_order, "targets": [commissioning_target]}
    assert client.put("/api/work-orders/" + duplicate_order, headers=admin, json=duplicate).status_code == 409
    assert commissioning_target not in {c["target"] for c in client.get("/api/work-order-candidates", headers=admin).json()}

    rows = order()["items"]
    assert [r["kind"] for r in rows] == ["preventive", "corrective", "commissioning"]
    assert [r["id"] for r in rows[:2]] == [r["id"] for r in original_rows]
    assert rows[1]["contract"] == old["name"] and rows[1]["contract_notes"] == "Ação planeada / planned."
    assert rows[2]["contract"] is None and rows[2]["reference"] == ordered_commissioning["reference"]
    assert rows[2]["external_id"] == label and rows[2]["station_id"] == station["id"]
    assert jobs(tech)[commissioning_target]["kind"] == "commissioning"
    assert jobs(outsider) == {}
    payload = {"charger_id":planned, "work_item_id":rows[1]["id"], "checklist_version":TEMPLATE["version"],
               "base_revision":0, "payload":{"answers":{}}}
    rid = str(uuid4())
    report = "/api/reports/" + rid
    assert client.put(report, headers=outsider, json=payload).status_code == 403
    saved = client.put(report, headers=tech, json=payload)
    saved.raise_for_status()
    snapshot = saved.json()["snapshot"]
    assert snapshot["intervention"]["kind"] == "corrective" and snapshot["contract"]["name"] == old["name"]
    assert saved.json()["identifier"] == label + "_" + all_jobs[planned]["reference"]
    closed = {"answers":answers()}
    add_signature(client, tech, rid, closed)
    client.put(report, headers=tech, json={**payload,"base_revision":1,"status":"closed","payload":closed}).raise_for_status()
    assert [r["state"] for r in order()["items"]] == ["pending", "closed", "pending"]
    assert jobs(tech)[planned]["report_id"] == rid
    pdf = client.get(report + "/pdf", headers=tech)
    pdf.raise_for_status()
    text = PdfReader(BytesIO(pdf.content)).pages[0].extract_text()
    assert "CORRETIVA" in text and "Âncora" in text
    bundle = client.get(report + "/bundle", headers=tech)
    bundle.raise_for_status()
    with ZipFile(BytesIO(bundle.content)) as archive:
        metadata = json.loads(archive.read(saved.json()["identifier"] + ".json"))
        assert metadata["snapshot"]["intervention"] == snapshot["intervention"]
        assert any(n.startswith("fotografias/") for n in archive.namelist())
    commissioned_report = "/api/reports/" + str(uuid4())
    commissioning_payload = {**payload, "charger_id": commissioning_target, "work_item_id": rows[2]["id"]}
    assert client.put(commissioned_report, headers=outsider, json=commissioning_payload).status_code == 403
    assert client.put(commissioned_report, headers=tech, json={**commissioning_payload, "work_item_id":None}).status_code == 403
    assert client.put(commissioned_report, headers=tech, json={**commissioning_payload, "work_item_id":rows[0]["id"]}).status_code == 409
    draft = client.put(commissioned_report, headers=tech, json=commissioning_payload)
    draft.raise_for_status()
    assert draft.json()["snapshot"]["contract"] is None
    assert draft.json()["snapshot"]["intervention"]["kind"] == "commissioning"
    assert draft.json()["identifier"] == f'{label}_{ordered_commissioning["reference"]}'
    assert draft.json()["work_item_id"] == rows[2]["id"]
    completion = {"answers": answers()}
    add_signature(client, tech, draft.json()["id"], completion)
    client.put(commissioned_report, headers=tech, json={**commissioning_payload, "base_revision":1,
               "status":"closed", "payload":completion}).raise_for_status()
    final_rows = order()["items"]
    assert [r["state"] for r in final_rows] == ["pending", "closed", "closed"]
    assert final_rows[2]["report_id"] == draft.json()["id"]
    assert commissioning_target not in {c["target"] for c in client.get("/api/work-order-candidates", headers=admin).json()}
    for target, kind in [(commissioning, "commissioning"), (sporadic, "corrective")]:
        def claim(_):
            return client.put("/api/reports/" + str(uuid4()), headers=tech,
                              json={**payload, "charger_id":target, "work_item_id":None})
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(claim, range(2)))
        assert sorted(r.status_code for r in replies) == [200,409]
        done = next(r.json() for r in replies if r.is_success)
        assert done["snapshot"]["contract"] is None and done["snapshot"]["intervention"]["kind"] == kind
        assert done["template"]["version"] == TEMPLATE["version"]
        if kind == "commissioning":
            completion = {"answers": answers()}
            add_signature(client, tech, done["id"], completion)
            endpoint = "/api/reports/" + done["id"]
            client.put(endpoint, headers=tech, json={**payload, "charger_id":target, "work_item_id":None,
                       "base_revision":1, "status":"closed", "payload":completion}).raise_for_status()
            exported = client.get(endpoint + "/pdf", headers=tech)
            exported.raise_for_status()
            page = PdfReader(BytesIO(exported.content)).pages[0].extract_text()
            assert "COMISSIONAMENTO" in page and all_jobs[target]["reference"] in page and "Contrato" not in page
        else:
            # An individually shared draft can later be placed in a future order.
            future_order = str(uuid4())
            future_plan = {**plan, "id": future_order, "targets": [target], "start": "2028-03-01", "due": "2028-03-02"}
            client.put("/api/work-orders/" + future_order, headers=admin, json=future_plan).raise_for_status()
            endpoint = "/api/reports/" + done["id"]
            completion = {"answers": answers()}
            add_signature(client, tech, done["id"], completion)
            result = client.put(endpoint, headers=tech, json={**payload, "charger_id":target, "work_item_id":None,
                                "base_revision":1, "status":"closed", "payload":completion})
            assert result.status_code == 409 and result.json()["reason"] == "work_order_not_started"
            assert client.get(endpoint, headers=tech).json()["status"] == "draft"
    with app.state.factory() as db:
        assert db.scalar(select(func.count()).select_from(Intervention)) == 5


@pytest.mark.parametrize("backend", ["setup", "postgres_setup"])
def test_ordered_commissioning_respects_start_and_can_be_rescheduled(backend, request, today):
    client, admin, preventive, _ = request.getfixturevalue(backend)
    upload(client, admin, preventive["id"], True, xlsx(("Carga-Â Station",))).raise_for_status()
    station = client.get("/api/stations", headers=admin).json()[0]
    technician, tech = account(client, admin, "campo")
    target = str(uuid4())
    client.put(f"/api/interventions/{target}", headers=admin, json={
        "kind": "commissioning", "station_id": station["id"], "technician": technician,
    }).raise_for_status()
    report_id = str(uuid4())
    endpoint = f"/api/reports/{report_id}"
    draft = {"charger_id": target, "checklist_version": TEMPLATE["version"],
             "base_revision": 0, "payload": {"answers": {}}}
    client.put(endpoint, headers=tech, json=draft).raise_for_status()

    future = str(uuid4())
    plan = {"id": future, "revision": 0, "start": "2028-03-01", "due": "2028-03-02",
            "technicians": [technician], "targets": [target]}
    client.put(f"/api/work-orders/{future}", headers=admin, json=plan).raise_for_status()
    completion = {"answers": answers()}
    add_signature(client, tech, report_id, completion)
    close = {**draft, "base_revision": 1, "status": "closed", "payload": completion}
    too_early = client.put(endpoint, headers={**tech, "X-Lem-Client-Version": "0.6.1"}, json=close)
    assert too_early.status_code == 409
    assert too_early.json() == {
        "reason": "work_order_not_started",
        "detail": "A ordem desta intervenção ainda não começou.",
    }
    assert client.get(endpoint, headers=tech).json()["status"] == "draft"

    client.put(f"/api/work-orders/{future}/cancel", headers=admin, json={"revision": 1}).raise_for_status()
    assert target in {c["target"] for c in client.get("/api/work-order-candidates", headers=admin).json()}
    active = str(uuid4())
    client.put(f"/api/work-orders/{active}", headers=admin, json={**plan, "id": active,
               "start": today.isoformat()}).raise_for_status()
    order = client.get("/api/work-orders", headers=tech).json()["orders"][0]
    assert order["id"] == active and order["items"][0]["drafts"][0]["id"] == report_id
    client.put(endpoint, headers=tech, json={**close, "work_item_id": order["items"][0]["id"]}).raise_for_status()
    order = client.get("/api/work-orders", headers=tech).json()["orders"][0]
    assert order["completed"] and order["items"][0]["report_id"] == report_id

    exported = client.get(endpoint + "/pdf", headers=tech)
    exported.raise_for_status()
    page = PdfReader(BytesIO(exported.content)).pages[0].extract_text()
    assert "COMISSIONAMENTO" in page and "Carga-Â Station" in page and "Contrato" not in page
