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
    def jobs(user):
        rows = client.get("/api/contracts", headers={**user, "X-Lem-Work-Orders":"1"})
        rows.raise_for_status()
        return {r["id"]: r["intervention"] for r in rows.json() if "intervention" in r}
    all_jobs = jobs(admin)
    assert [c["id"] for c in client.get("/api/contracts", headers=admin).json()] == [preventive["id"]]
    assert all_jobs[key]["contract"]["id"] == current["id"] and all_jobs[key]["notes"] == notes
    assert all_jobs[planned]["contract"]["id"] == old["id"]
    assert all_jobs[sporadic]["contract"] is None and all_jobs[commissioning]["contract"] is None
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
    order_id = str(uuid4())
    plan = dict(id=order_id, revision=0, start=today.isoformat(), due=today.isoformat(), technicians=[uid], targets=[base, planned])
    client.put("/api/work-orders/" + order_id, headers=admin, json=plan).raise_for_status()
    def order():
        return client.get("/api/work-orders", headers=tech).json()["orders"][0]
    rows = order()["items"]
    assert [r["kind"] for r in rows] == ["preventive", "corrective"]
    assert rows[1]["contract"] == old["name"] and rows[1]["contract_notes"] == "Ação planeada / planned."
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
    assert [r["state"] for r in order()["items"]] == ["pending", "closed"]
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
            assert result.status_code == 409 and "ainda não começou" in result.json()["detail"]
            assert client.get(endpoint, headers=tech).json()["status"] == "draft"
    with app.state.factory() as db:
        assert db.scalar(select(func.count()).select_from(Intervention)) == 4
