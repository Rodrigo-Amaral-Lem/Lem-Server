# SPDX-License-Identifier: GPL-3.0-only
"""Return visits, report-scoped owners and the atomic individual handoff."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from io import BytesIO
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from PIL import Image
from sqlalchemy import select

from app import work_orders
from app.models import OwnershipTransfer
from test_postgres_branding import postgres_setup
from test_report_corrections import body as correction_body
from test_visit_sync_integrity import backend, plan, report
from test_work_orders import account
from test_workflow import add_signature, setup, upload, xlsx


OBSERVATION = "Ligação revista na Âncora / inspected on site."


def ok(response):
    assert response.is_success, (response.status_code, response.text)
    return response.json()


@pytest.fixture
def field(backend, monkeypatch):
    api, admin, contract, app = backend
    instant = datetime(2028, 2, 29, 12, tzinfo=ZoneInfo("Europe/Lisbon"))
    monkeypatch.setattr(work_orders, "lisbon_now", lambda: instant)
    ok(upload(api, admin, contract["id"], True, xlsx(("Ação-Â North",))))
    charger, = ok(api.get(f"/api/contracts/{contract['id']}/chargers", headers=admin))
    return api, admin, charger, app


def intervention(api, admin, charger, kind, technician=None):
    identifier = str(uuid4())
    path = f"/api/interventions/{identifier}"
    ok(api.put(path, headers=admin, json={
        "kind": kind, "station_id": charger["station_id"], "technician": technician,
    }))
    return identifier, path


def job_summary(api, headers, job_id):
    scopes = ok(api.get("/api/contracts", headers={**headers, "X-Lem-Work-Orders": "1"}))
    return next(scope["intervention"] for scope in scopes if scope["id"] == job_id)


def draft_body(target, item=None):
    body = report(target)
    if item:
        body["work_item_id"] = item
    body["payload"]["answers"]["FIN.03"] = {"value": "", "notes": OBSERVATION}
    return body


def add_photo(api, headers, path):
    image = BytesIO()
    Image.new("RGB", (64, 32), "green").save(image, format="PNG")
    original = image.getvalue()
    photo = f"{path}/photos/{uuid4()}"
    ok(api.put(photo, headers=headers, data={"checkpoint": "FIN.03", "base_revision": "1"},
               files={"file": ("site.png", original, "image/png")}))
    return photo, original


def mark_impeded(api, order, item, headers):
    ok(api.put(f"{order}/items/{item}/invalid", headers=headers, json={
        "revision": 1, "note": "Portão fechado / site locked",
    }))


@pytest.mark.parametrize("kind", ["corrective", "commissioning"])
def test_return_visit_preserves_old_draft_and_finishes_new_report(field, kind):
    api, admin, charger, _ = field
    first_id, first = account(api, admin, "outbound")
    second_id, second = account(api, admin, "return-crew")
    _, planner = account(api, admin, "second-planner", "admin")
    target, _ = intervention(api, admin, charger, kind)
    outbound, old_item, _ = plan(api, admin, [first_id], target)
    old_path = f"/api/reports/{uuid4()}"
    old_body = draft_body(target, old_item)
    old = ok(api.put(old_path, headers=first, json=old_body))
    photo, original = add_photo(api, first, old_path)
    mark_impeded(api, outbound, old_item, first)

    assert "report_id" not in job_summary(api, admin, target)
    catalogue, = ok(api.get(f"/api/contracts/{target}/chargers", headers=admin))
    assert catalogue["reports"][0]["work_item_recovery"]
    candidates = ok(api.get("/api/work-order-candidates", headers=planner))
    assert target in {row["target"] for row in candidates}
    _, new_item, current = plan(api, planner, [second_id], target)
    candidates = ok(api.get("/api/work-order-candidates", headers=planner))
    assert target not in {row["target"] for row in candidates}
    new_body = draft_body(target, new_item)
    new_path = f"/api/reports/{uuid4()}"
    fresh = ok(api.put(new_path, headers=second, json=new_body))
    assert fresh["work_item_id"] == new_item
    assert job_summary(api, second, target)["report_id"] == fresh["id"]
    assert api.put(f"/api/reports/{uuid4()}", headers=second, json=new_body).status_code == 409
    assert api.put(old_path, headers=first, json={**old_body, "base_revision": 1}).status_code == 403

    add_signature(api, second, fresh["id"], new_body["payload"])
    saved = ok(api.put(new_path, headers=second, json={**new_body, "status": "closed", "base_revision": 1}))
    assert saved["status"] == "closed"
    candidates = ok(api.get("/api/work-order-candidates", headers=planner))
    assert target not in {row["target"] for row in candidates}
    assert job_summary(api, admin, target)["report_id"] == fresh["id"]
    orders = {row["id"]: row for row in ok(api.get("/api/work-orders", headers=admin))["orders"]}
    assert orders[current["id"]]["items"][0]["report_id"] == fresh["id"]
    original_item = orders[outbound.rsplit("/", 1)[-1]]["items"][0]
    assert original_item["state"] == "invalid" and original_item["drafts"][0]["id"] == old["id"]
    recovered = ok(api.get(old_path, headers=first))
    assert recovered["work_item_id"] == old_item and recovered["work_item_recovery"]
    assert recovered["payload"]["answers"]["FIN.03"]["notes"] == OBSERVATION
    assert api.get(photo, headers=first).content == original


@pytest.mark.parametrize("kind", ["corrective", "commissioning"])
def test_stale_owner_cannot_delete_impeded_intervention_draft(field, kind):
    api, admin, charger, _ = field
    owner_id, owner = account(api, admin, "individual-and-crew")
    partner_id, partner = account(api, admin, "partner")
    target, _ = intervention(api, admin, charger, kind, owner_id)
    order, item, _ = plan(api, admin, [owner_id, partner_id], target)
    path = f"/api/reports/{uuid4()}"
    draft = ok(api.put(path, headers=owner, json=draft_body(target, item)))
    photo, original = add_photo(api, owner, path)

    mark_impeded(api, order, item, partner)
    params = {"base_revision": draft["revision"]}
    rejected = api.delete(path, headers=owner, params=params)
    assert rejected.status_code == 403
    recovered = ok(api.get(path, headers=owner))
    assert recovered["work_item_recovery"] and recovered["revision"] == draft["revision"]
    assert recovered["payload"]["answers"]["FIN.03"]["notes"] == OBSERVATION
    assert api.get(photo, headers=owner).content == original

    from test_report_delete import evidence_digest
    assert ok(api.delete(path, headers=admin, params={**params, "evidence_digest": evidence_digest(recovered)}))["deleted"]
    assert api.get(path, headers=admin).status_code == 410


def test_early_preventive_closure_keeps_mixed_order_scheduled(field, monkeypatch):
    api, admin, charger, _ = field
    owner_id, owner = account(api, admin, "mixed-crew")
    corrective, _ = intervention(api, admin, charger, "corrective")
    commissioning, _ = intervention(api, admin, charger, "commissioning")
    tomorrow = work_orders.lisbon_now() + timedelta(days=1)
    start = tomorrow.date().isoformat()
    order_id = str(uuid4())
    ok(api.put(f"/api/work-orders/{order_id}", headers=admin, json={
        "id": order_id, "revision": 0, "start": start, "due": start,
        "technicians": [owner_id], "targets": [charger["id"], corrective, commissioning],
    }))

    rid = str(uuid4())
    path = f"/api/reports/{rid}"
    preventive = draft_body(charger["id"])
    ok(api.put(path, headers=admin, json=preventive))
    add_signature(api, admin, rid, preventive["payload"])
    ok(api.put(path, headers=admin, json={**preventive, "base_revision": 1, "status": "closed"}))

    order, = ok(api.get("/api/work-orders", headers=owner))["orders"]
    assert order["start"] == start and not order["started"]
    assert order["items"][0]["report_id"] == rid
    drafts = [(f"/api/reports/{uuid4()}", draft_body(item["target"], item["id"])) for item in order["items"][1:]]
    for draft_path, payload in drafts:
        assert api.put(draft_path, headers=owner, json=payload).status_code == 403
    monkeypatch.setattr(work_orders, "lisbon_now", lambda: tomorrow)
    assert ok(api.get("/api/work-orders", headers=owner))["orders"][0]["started"]
    for draft_path, payload in drafts:
        assert ok(api.put(draft_path, headers=owner, json=payload))["work_item_id"] == payload["work_item_id"]


@pytest.mark.parametrize("kind", ["corrective", "commissioning"])
def test_order_crew_can_resolve_corrected_uuid_after_missing_two_corrections(field, kind):
    api, admin, charger, _ = field
    past_id, past_crew = account(api, admin, "past-crew")
    current_id, current_crew = account(api, admin, "current-crew")
    target, _ = intervention(api, admin, charger, kind)
    old_order, old_item, _ = plan(api, admin, [past_id], target)
    mark_impeded(api, old_order, old_item, past_crew)
    _, item, _ = plan(api, admin, [current_id], target)
    original_id = str(uuid4())
    path = f"/api/reports/{original_id}"
    payload = draft_body(target, item)
    ok(api.put(path, headers=current_crew, json=payload))
    add_signature(api, current_crew, original_id, payload["payload"])
    closing = {**payload, "base_revision": 1, "status": "closed"}
    replaced = ok(api.put(path, headers=current_crew, json=closing))
    desktop = {**admin, "X-Lem-Client-Platform": "windows"}
    for _ in range(2):
        working = ok(api.post(f"/api/reports/{replaced['id']}/corrections", headers=desktop, json={
            "id": str(uuid4()), "base_revision": replaced["revision"],
        }))
        replaced = ok(api.post(f"/api/reports/{working['id']}/correction/commit", headers=desktop,
                               json=correction_body(working)))

    assert ok(api.get(f"/api/reports/{replaced['id']}", headers=current_crew))["status"] == "closed"
    stale_reads = [api.get(path + suffix, headers=current_crew) for suffix in ("", "/exists")]
    for response in [*stale_reads, api.put(path, headers=current_crew, json=closing)]:
        assert response.status_code == 410
        assert response.json()["detail"]["replacement_id"] == replaced["id"]
    assert api.get(path + "/exists", headers=past_crew).status_code == 403


def test_transfer_choices_use_the_exact_bound_visit(field):
    api, admin, charger, _ = field
    old_owner, first = account(api, admin, "outbound")
    same_visit, coworker = account(api, admin, "outbound-partner")
    next_visit, _ = account(api, admin, "return-crew")
    target = charger["id"]
    order, item, _ = plan(api, admin, [old_owner, same_visit], target)
    path = f"/api/reports/{uuid4()}"
    body = draft_body(target, item)
    ok(api.put(path, headers=first, json=body))
    mark_impeded(api, order, item, first)
    plan(api, admin, [next_visit], target)
    owners = {owner["id"] for owner in ok(api.get(path + "/owners", headers=admin))}
    assert old_owner in owners and same_visit in owners and next_visit not in owners
    blocked = api.post(path + "/owner", headers=admin, json={"owner_id": next_visit, "base_revision": 1})
    assert blocked.status_code == 403
    stored = ok(api.get(path, headers=first))
    assert stored["owner_id"] == old_owner and stored["revision"] == 1
    ok(api.post(path + "/owner", headers=admin, json={"owner_id": same_visit, "base_revision": 1}))
    recovered = ok(api.get(path, headers=coworker))
    assert recovered["owner_id"] == same_visit and recovered["work_item_id"] == item
    assert recovered["payload"]["answers"]["FIN.03"]["notes"] == OBSERVATION
    assert api.put(path, headers=coworker, json={**body, "base_revision": 2}).status_code == 403


def test_transfer_between_current_crew_members(field):
    api, admin, charger, _ = field
    first_id, first = account(api, admin, "first")
    second_id, second = account(api, admin, "second")
    _, item, _ = plan(api, admin, [first_id, second_id], charger["id"])
    body = draft_body(charger["id"], item)
    path = f"/api/reports/{uuid4()}"
    ok(api.put(path, headers=first, json=body))
    assert second_id in {owner["id"] for owner in ok(api.get(path + "/owners", headers=admin))}
    transferred = ok(api.post(path + "/owner", headers=admin, json={"owner_id": second_id, "base_revision": 1}))
    assert transferred["owner_id"] == second_id and transferred["revision"] == 2
    assert api.put(path, headers=first, json={**body, "base_revision": 2}).status_code == 403
    assert ok(api.put(path, headers=second, json={**body, "base_revision": 2}))["revision"] == 3


def test_individual_handoff_preserves_draft_and_serializes_competing_office_users(field):
    api, admin, charger, app = field
    old_owner, outgoing = account(api, admin, "outgoing")
    first_id, first = account(api, admin, "first-recipient")
    second_id, second = account(api, admin, "second-recipient")
    _, planner = account(api, admin, "planner", "admin")
    target, path = intervention(api, admin, charger, "commissioning", old_owner)
    rid = str(uuid4())
    report_path = f"/api/reports/{rid}"
    draft = draft_body(target)
    ok(api.put(report_path, headers=outgoing, json=draft))
    photo, original = add_photo(api, outgoing, report_path)
    stale_metadata = job_summary(api, admin, target)["handoff"]
    assert stale_metadata == {"report_id": rid, "base_revision": 1}
    assert "handoff" not in job_summary(api, outgoing, target)

    share = path + "/share"
    change = {"technician": first_id, "previous": old_owner}
    assert api.put(share, headers=admin, json=change).status_code == 409
    ok(api.put(report_path, headers=outgoing, json={**draft, "base_revision": 1}))
    assert api.put(share, headers=admin, json={**change, "handoff": stale_metadata}).status_code == 409
    assert job_summary(api, admin, target)["technician"] == old_owner
    metadata = job_summary(api, admin, target)["handoff"]
    assert metadata["base_revision"] == 2
    assert api.put(share, headers=admin, json={**change, "handoff": {
        **metadata, "report_id": str(uuid4()),
    }}).status_code == 409
    recipients = [(first_id, first, admin), (second_id, second, planner)]

    def try_handoff(recipient):
        technician, _, office = recipient
        return api.put(share, headers=office, json={
            "technician": technician, "previous": old_owner, "handoff": metadata,
        })

    with ThreadPoolExecutor(max_workers=2) as executor:
        replies = list(executor.map(try_handoff, recipients))
    assert sorted(reply.status_code for reply in replies) == [200, 409]
    index = next(index for index, reply in enumerate(replies) if reply.is_success)
    winner, incoming, _ = recipients[index]
    assert ok(try_handoff(recipients[index]))["technician"] == winner
    transferred = ok(api.get(report_path, headers=incoming))
    assert transferred["owner_id"] == winner and transferred["revision"] == 3
    assert transferred["payload"]["answers"]["FIN.03"]["notes"] == OBSERVATION
    assert job_summary(api, admin, target)["technician"] == winner
    assert api.get(photo, headers=incoming).content == original
    assert api.get(report_path, headers=outgoing).status_code == 403
    with app.state.factory() as db:
        history, = db.scalars(select(OwnershipTransfer).where(OwnershipTransfer.report_id == rid)).all()
        assert history.old_owner_id == old_owner and history.new_owner_id == winner
