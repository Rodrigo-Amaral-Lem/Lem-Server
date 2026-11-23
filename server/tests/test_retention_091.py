"""Verify retention across work-order and intervention transitions."""

from uuid import uuid4

import pytest

from app import work_orders
from test_field_handoffs import OBSERVATION, add_photo, draft_body, field, intervention, ok
from test_postgres_branding import postgres_setup
from test_report_delete import evidence_digest
from test_visit_sync_integrity import backend
from test_work_orders import account
from test_workflow import add_signature, setup


REPORT_CATALOGUE = {"X-Lem-Work-Orders": "1"}
DESKTOP = {"X-Lem-Client-Platform": "windows"}
FIELD_MODEL = "Modelo Âncora / field"
OFFICE_MODEL = "Modelo Âncora / office"


def make_order(api, admin, technicians, targets):
    order_id = str(uuid4())
    date = work_orders.lisbon_now().date().isoformat()
    path = f"/api/work-orders/{order_id}"
    plan = {
        "id": order_id, "revision": 0, "start": date, "due": date,
        "technicians": technicians, "targets": targets,
    }
    ok(api.put(path, headers=admin, json=plan))
    order = next(row for row in ok(api.get("/api/work-orders", headers=admin))["orders"]
                 if row["id"] == order_id)
    return path, plan, order


@pytest.mark.parametrize("kind", ["corrective", "commissioning"])
def test_reassignment_preserves_the_server_draft_and_enforces_scope(field, kind):
    api, admin, charger, _ = field
    outgoing_id, outgoing = account(api, admin, "outgoing")
    incoming_id, incoming = account(api, admin, "incoming")
    job, _ = intervention(api, admin, charger, kind)
    order_path, plan, order = make_order(api, admin, [outgoing_id], [job])
    rid = str(uuid4())
    report_path = f"/api/reports/{rid}"
    body = draft_body(job, order["items"][0]["id"])
    ok(api.put(report_path, headers=outgoing, json=body))
    photo_path, photo_bytes = add_photo(api, outgoing, report_path)
    assert ok(api.get("/api/work-orders", headers=outgoing))["orders"][0]["items"][0]["drafts"][0]["id"] == rid

    ok(api.put(order_path, headers=admin, json={**plan, "revision": order["revision"],
                                               "technicians": [incoming_id]}))
    assert ok(api.get("/api/work-orders", headers=outgoing))["orders"] == []
    assert ok(api.get("/api/contracts", headers={**outgoing, **REPORT_CATALOGUE})) == []
    assert api.get(report_path, headers=outgoing).status_code == 403
    incoming_item = ok(api.get("/api/work-orders", headers=incoming))["orders"][0]["items"][0]
    assert incoming_item["drafts"][0]["owner_id"] == outgoing_id
    assert ok(api.get(report_path, headers=admin))["payload"]["answers"]["FIN.03"]["notes"] == OBSERVATION
    assert api.get(photo_path, headers=admin).content == photo_bytes


def test_detaching_planned_pcve_keeps_existing_report_in_order(field):
    api, admin, charger, _ = field
    owner_id, owner = account(api, admin, "planned")
    commissioning, _ = intervention(api, admin, charger, "commissioning")
    _, _, order = make_order(api, admin, [owner_id], [charger["id"], commissioning])
    item = order["items"][0]
    rid = str(uuid4())
    path = f"/api/reports/{rid}"
    body = draft_body(charger["id"], item["id"])
    ok(api.put(path, headers=owner, json=body))
    photo_path, photo_bytes = add_photo(api, owner, path)
    before = ok(api.get("/api/work-orders", headers=owner))["orders"][0]["items"][0]
    assert before["available"] and before["drafts"][0]["id"] == rid

    ok(api.delete(f"/api/contracts/{charger['contract_id']}/chargers/{charger['id']}", headers=admin))
    after = ok(api.get("/api/work-orders", headers=owner))["orders"][0]["items"][0]
    assert after["state"] == "pending" and not after["available"] and [draft["id"] for draft in after["drafts"]] == [rid]
    assert ok(api.get(f"/api/contracts/{charger['contract_id']}/chargers",
                      headers={**owner, **REPORT_CATALOGUE})) == []
    assert ok(api.get(path, headers=owner))["payload"]["answers"]["FIN.03"]["notes"] == OBSERVATION
    assert api.get(photo_path, headers=owner).content == photo_bytes
    new_report = f"/api/reports/{uuid4()}"
    assert api.put(new_report, headers=owner, json=body).status_code == 409
    updated = ok(api.put(path, headers=owner, json={**body, "base_revision": 1}))
    assert updated["id"] == rid and updated["revision"] == 2


def test_stale_delete_retains_a_photo_accepted_after_the_admin_read(field):
    api, admin, charger, _ = field
    owner_id, owner = account(api, admin, "offline")
    job, _ = intervention(api, admin, charger, "commissioning")
    _, _, order = make_order(api, admin, [owner_id], [job])
    path = f"/api/reports/{uuid4()}"
    body = draft_body(job, order["items"][0]["id"])
    ok(api.put(path, headers=owner, json=body))
    admin_view = ok(api.get(path, headers=admin))
    assert admin_view["revision"] == 1 and admin_view["photos"] == []
    photo_path, photo_bytes = add_photo(api, owner, path)
    after_upload = ok(api.get(path, headers=owner))
    assert after_upload["revision"] == admin_view["revision"] and len(after_upload["photos"]) == 1
    assert api.get(photo_path, headers=owner).content == photo_bytes

    params = {"base_revision": admin_view["revision"], "evidence_digest": evidence_digest(admin_view)}
    assert api.delete(path, headers=admin, params=params).status_code == 409
    assert api.get(photo_path, headers=owner).content == photo_bytes
    params["evidence_digest"] = evidence_digest(after_upload)
    assert ok(api.delete(path, headers=admin, params=params))["deleted"]
    removed = api.get(path, headers=owner)
    assert removed.status_code == 410 and isinstance(removed.json()["detail"], str)
    assert api.get(photo_path, headers=owner).status_code == 410


@pytest.mark.parametrize("kind", ["corrective", "commissioning"])
def test_ordinary_closure_recovery_get_uses_current_station_record(field, kind):
    api, admin, charger, _ = field
    station_path = f"/api/stations/{charger['station_id']}"
    owner_id, owner = account(api, admin, "closer")
    job, _ = intervention(api, admin, charger, kind)
    _, _, order = make_order(api, admin, [owner_id], [job])
    rid = str(uuid4())
    path = f"/api/reports/{rid}"
    body = draft_body(job, order["items"][0]["id"])
    created = ok(api.put(path, headers=owner, json=body))
    assert created["snapshot"]["charger"]["model"] == charger["details"]["model"]
    add_signature(api, owner, rid, body["payload"])
    body["payload"]["answers"]["EST.03"]["value"] = FIELD_MODEL
    body["payload"]["finished"] = "2026-11-12T11:00:00Z"
    sent = ok(api.put(path, headers=owner, json={**body, "base_revision": 1, "status": "closed"}))
    assert sent["station_update"]["details"]["model"] == FIELD_MODEL
    live = ok(api.patch(station_path, headers={**admin, **DESKTOP}, json={
        "base_updated": sent["station_update"]["updated"], "fields": {"model": OFFICE_MODEL},
    }))
    recovered = ok(api.get(path, headers=owner))
    assert recovered["status"] == "closed" and recovered["payload"] == sent["payload"]
    assert live["details"]["model"] == OFFICE_MODEL
    assert recovered["snapshot"]["charger"]["model"] == FIELD_MODEL
    assert recovered["station_update"]["details"] == live["details"]
    assert recovered["station_update"]["updated"] == live["updated"]
