# SPDX-License-Identifier: GPL-3.0-only
"""Form v8 correctness across correction/export and the two independent undo periods."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from uuid import uuid4
from zipfile import ZipFile

import pytest
from pypdf import PdfReader
from app import contract_trash
from app.main import TEMPLATE, FROZEN_TEMPLATES
from app.measurements import format_value
from app.models import ContractTrash, RemovedCharger, RemovedCoverage, Report, Photo
from test_field_handoffs import add_photo, intervention, ok
from test_interventions import import_contract, today
from test_report_corrections import start, body
from test_workflow import setup, upload, xlsx, add_signature
from test_postgres_branding import postgres_setup
from test_work_orders import account


def station(api, headers, contract):
    ok(upload(api, headers, contract["id"], True, xlsx(("Âncora-01",))))
    return ok(api.get(f"/api/contracts/{contract['id']}/chargers", headers=headers))[0]


def create_report(api, headers, target, version=8, closed=False):
    schema = TEMPLATE if version == 8 else FROZEN_TEMPLATES[version]
    answers = {item["id"]: {"value": "NA", "notes": "Veículo em carga / busy"}
               for section in schema["sections"] for item in section["items"]}
    payload = {"answers": answers, "started": "2026-11-10T09:00:00Z", "finished": "2026-11-10T11:00:00Z"}
    path = "/api/reports/" + str(uuid4())
    data = dict(checklist_version=version, charger_id=target, base_revision=0, payload=payload,
                status="draft", frozen_history=closed and version < 8)
    ok(api.put(path, headers=headers, json=data))
    photo, image = add_photo(api, headers, path)
    if closed:
        add_signature(api, headers, path.rsplit("/", 1)[-1], payload)
        ok(api.put(path, headers=headers, json={**data, "status": "closed", "base_revision": 1}))
    return path, photo, image


@pytest.mark.parametrize("kind", ["preventive", "corrective", "commissioning"])
def test_closed_v7_requires_both_times_and_exports_the_corrected_originals(setup, kind, tmp_path):
    api, headers, contract, _ = setup
    headers = {**headers, "X-Lem-Client-Platform": "windows"}
    charger = station(api, headers, contract)
    target = charger["id"] if kind == "preventive" else intervention(api, headers, charger, kind)[0]
    path, _, image = create_report(api, headers, target, 7, closed=True)
    original = ok(api.get(path, headers=headers))
    assert original["template"]["version"] == 7
    working = start((api, headers, contract, setup[3], original))
    data = body(working)
    commit = "/api/reports/" + working["id"] + "/correction/commit"
    def submit():
        return api.post(commit, headers=headers, json=data)
    rejected = submit()
    assert rejected.status_code == 422
    assert all(key + ": falta resposta" in rejected.text for key in ("MAN.12", "MAN.14"))
    assert ok(api.get(path, headers=headers)) == original
    answers = data["payload"]["answers"]
    answers["MAN.12"] = {"value": "999999"}
    answers["MAN.14"] = {"value": "125"}
    assert "MAN.14: escolha" in submit().text
    answers["MAN.14"].update(assessment="NOK", notes="")
    assert "NOK precisa de observação" in submit().text
    answers["MAN.14"].update(value="NA", notes="Proteção diferencial não dispara")
    assert "MAN.14: falta fotografia" in submit().text
    from pathlib import Path
    video = (Path(__file__).with_name("fixtures") / "evidence-clip.mp4").read_bytes()
    media = "/api/reports/" + working["id"] + "/photos/" + str(uuid4())
    ok(api.put(media, headers=headers, data={"checkpoint": "MAN.14", "base_revision": working["revision"]},
               files={"file": ("ensaio.mp4", video, "video/mp4")}))
    assert "MAN.14: falta fotografia" in submit().text
    answers["MAN.14"]["value"] = "125 ms"
    new_photo = "/api/reports/" + working["id"] + "/photos/" + str(uuid4())
    ok(api.put(new_photo, headers=headers, data={"checkpoint":"MAN.14", "base_revision":working["revision"]},
               files={"file": ("Âncora.png", image, "image/png")}))
    saved = ok(submit())
    assert saved == ok(submit())
    assert saved["payload"]["answers"]["MAN.12"]["value"] == "999999 ms"
    result = api.get("/api/reports/" + working["id"] + "/bundle", headers=headers)
    result.raise_for_status()
    (tmp_path / "correcao.zip").write_bytes(result.content)
    with ZipFile(BytesIO(result.content)) as z:
        document = json.loads(z.read(next(name for name in z.namelist() if name.endswith(".json"))))
        assert document["payload"]["answers"] == saved["payload"]["answers"]
        assert [z.read(name) for name in z.namelist() if name.endswith(".png")].count(image) == 2
        pdf = z.read(next(name for name in z.namelist() if name.endswith(".pdf")))
    (tmp_path / "correcao.pdf").write_bytes(pdf)
    text = " ".join(" ".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages).split())
    standalone = api.get("/api/reports/" + working["id"] + "/pdf", headers=headers)
    standalone.raise_for_status()
    assert text == " ".join(" ".join(page.extract_text() for page in PdfReader(BytesIO(standalone.content)).pages).split())
    for label in ("MAN.12", "MAN.14", "tempo de disparo configurado", "tempo de disparo com testador",
                  "999999 ms", "125 ms", "Proteção diferencial não dispara"):
        assert label in text


@pytest.mark.parametrize("raw,expected", [("0", "0 ms"), ("999999", "999999 ms"), ("NA", "Não Aplicável"),
                                        ("1000000", None), ("0,2", None), ("72 mA", None)])
def test_milliseconds(raw, expected):
    if expected is None:
        with pytest.raises(ValueError):
            format_value("ms", raw)
    else:
        assert format_value("ms", raw) == expected


@pytest.mark.parametrize("kind", ["corrective", "commissioning"])
@pytest.mark.parametrize("backend", ["setup", "postgres_setup"])
def test_job_48_hours_exact_recovery_is_admin_only_and_preserves_draft(request, backend, kind):
    api, h, c, app = request.getfixturevalue(backend)
    charger = station(api, h, c)
    key, job = intervention(api, h, charger, kind)
    _, tech = account(api, h, "retention-tech")
    path, photo, image = create_report(api, h, key)
    before = ok(api.get(path, headers=h))
    trash = job + "/trash"
    confirm = {"confirmation":"APAGAR"}
    assert api.post(trash, headers=tech, json=confirm).status_code == 403
    first = ok(api.post(trash, headers=h, json=confirm))
    assert ok(api.post(trash, headers=h, json=confirm)) == first
    assert api.get(path, headers=h).status_code == 423
    with app.state.factory() as db:
        entry = db.get(ContractTrash, key)
        assert datetime.fromisoformat(entry.purge_after) - datetime.fromisoformat(entry.deleted_at) == timedelta(hours=48)
    restore = f"/api/contracts/{key}/restore"
    assert api.post(restore, headers=tech).status_code == 403
    ok(api.post(restore, headers=h))
    assert ok(api.get(path, headers=h)) == before
    assert api.get(photo, headers=h).content == image
    ok(api.post(trash, headers=h, json=confirm))
    with app.state.factory.begin() as db:
        db.get(ContractTrash, key).purge_after = datetime.now(timezone.utc).isoformat()
    assert api.post(restore, headers=h).status_code == 410
    assert app.state.purge_expired_contracts() == 1
    assert api.get(path, headers=h).status_code == 404
    assert api.put(job, headers=h, json={"kind":kind, "station_id":charger["station_id"]}).status_code == 409
    next_key, next_path = intervention(api, h, charger, kind)
    assert next_key != key
    create_report(api, h, next_key, closed=True)
    assert api.post(next_path + "/trash", headers=h, json=confirm).status_code == 409


@pytest.mark.parametrize("kind", ["preventive", "corrective"])
@pytest.mark.parametrize("backend", ["setup", "postgres_setup"])
def test_membership_14_days_restores_only_its_own_reports(request, backend, today, kind):
    api, h, c, app = request.getfixturevalue(backend)
    charger = station(api, h, c)
    preventive = f"/api/contracts/{c['id']}"
    unrelated, _, _ = create_report(api, h, intervention(api, h, charger, "commissioning")[0])
    if kind == "corrective":
        covered = ok(import_contract(api, h, [charger["external_id"]]))
        key, _ = intervention(api, h, charger, "corrective")
        endpoint = f"/api/corrective-contracts/{covered['id']}/members/{charger['station_id']}"
        listing = endpoint.rsplit("/", 1)[0]
    else:
        key = charger["id"]
        endpoint = f"{preventive}/chargers/{key}"
        listing = preventive + "/removed-chargers"
    path, photo, image = create_report(api, h, key, closed=True)
    before = ok(api.get(path, headers=h))
    _, tech = account(api, h, "coverage-tech")
    assert api.delete(endpoint, headers=tech).status_code == 403
    instant = datetime.now(timezone.utc)
    ok(api.delete(endpoint, headers=h))
    first = ok(api.get(listing, headers=h))[0]
    remaining = datetime.fromisoformat(first["purge_after"]) - instant
    assert timedelta(days=14) <= remaining < timedelta(days=14, seconds=5)
    ok(api.delete(endpoint, headers=h))
    assert first == ok(api.get(listing, headers=h))[0]
    assert api.get(path, headers=h).status_code in (409, 423)
    assert api.get(unrelated, headers=h).is_success
    if kind == "corrective":
        assert api.post(f"/api/contracts/{key}/restore", headers=h).status_code == 409
    assert api.post(endpoint+"/restore", headers=tech).status_code == 403
    ok(api.post(endpoint+"/restore", headers=h))
    ok(api.post(endpoint+"/restore", headers=h))
    assert ok(api.get(path, headers=h)) == before
    assert api.get(photo, headers=h).content == image
    ok(api.delete(endpoint, headers=h))
    with app.state.factory.begin() as db:
        cutoff = datetime.now(timezone.utc).isoformat()
        if kind == "preventive":
            db.get(RemovedCharger, key).purge_after = cutoff
        else:
            db.get(RemovedCoverage, (covered["id"], charger["station_id"])).purge_after = cutoff
            db.get(ContractTrash, key).purge_after = cutoff
    assert api.post(endpoint+"/restore", headers=h).status_code == 410
    contract_trash.purge_memberships(app.state.factory)
    app.state.purge_expired_contracts()
    with app.state.factory() as db:
        assert db.get(Report, before["id"]) is None
        assert db.get(Photo, photo.rsplit("/", 1)[-1]) is None
    assert api.get(unrelated, headers=h).is_success


def test_reimport_respects_last_recoverable_instant_and_does_not_revive_expired_reports(setup, monkeypatch):
    api, h, c, _ = setup
    charger = station(api, h, c)
    path, photo, image = create_report(api, h, charger["id"], closed=True)
    before = ok(api.get(path, headers=h))
    member = f"/api/contracts/{c['id']}/chargers/{charger['id']}"
    removed = f"/api/contracts/{c['id']}/removed-chargers"
    for last_instant in (True, False):
        ok(api.delete(member, headers=h))
        deadline = datetime.fromisoformat(ok(api.get(removed, headers=h))[0]["purge_after"])
        instant = deadline - timedelta(microseconds=1) if last_instant else deadline
        monkeypatch.setattr(contract_trash, "utc_now", lambda: instant)
        ok(upload(api, h, c["id"], True, xlsx((charger["external_id"],))))
        rows = ok(api.get(member.rsplit("/", 1)[0], headers=h))
        assert rows[0]["id"] == charger["id"]
        if last_instant:
            assert ok(api.get(path, headers=h)) == before
            assert api.get(photo, headers=h).content == image
        else:
            assert rows[0]["reports"] == []
            assert api.get(path, headers=h).status_code == 410
            assert not api.get(photo, headers=h).is_success


def test_job_and_membership_recovery_keep_independent_deadlines(setup, today, monkeypatch):
    from app import main
    api, h, c, _ = setup
    charger = station(api, h, c)
    coverage = ok(import_contract(api, h, [charger["external_id"]]))
    key, job = intervention(api, h, charger, "corrective")
    path, _, _ = create_report(api, h, key)
    member = f"/api/corrective-contracts/{coverage['id']}/members/{charger['station_id']}"
    deleted = ok(api.post(job + "/trash", headers=h, json={"confirmation": "APAGAR"}))
    ok(api.delete(member, headers=h))
    ok(api.post(member + "/restore", headers=h))
    assert api.get(path, headers=h).status_code == 423
    restore = f"/api/contracts/{key}/restore"
    deadline = datetime.fromisoformat(deleted["purge_after"])
    with monkeypatch.context() as clock:
        clock.setattr(main, "now", lambda: (deadline - timedelta(microseconds=1)).isoformat())
        ok(api.post(restore, headers=h))
        assert ok(api.get(path, headers=h))["status"] == "draft"
    entry = ok(api.post(job + "/trash", headers=h, json={"confirmation": "APAGAR"}))
    monkeypatch.setattr(main, "now", lambda: entry["purge_after"])
    assert api.post(restore, headers=h).status_code == 410
    assert not ok(api.get("/api/admin/deleted-contracts", headers=h))


@pytest.mark.parametrize("backend", ["setup", "postgres_setup"])
def test_closure_racing_with_request_deletion_keeps_one_consistent_result(request, backend):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    api, h, c, _ = request.getfixturevalue(backend)
    charger = station(api, h, c)
    key, job = intervention(api, h, charger, "commissioning")
    path, photo, image = create_report(api, h, key)
    draft = ok(api.get(path, headers=h))
    add_signature(api, h, draft["id"], draft["payload"], revision=draft["revision"])
    closure = dict(checklist_version=8, charger_id=key, base_revision=draft["revision"],
                   payload=draft["payload"], status="closed")
    ready = Barrier(2)
    def close():
        ready.wait(timeout=10)
        return api.put(path, headers=h, json=closure)
    def remove():
        ready.wait(timeout=10)
        return api.post(job + "/trash", headers=h, json={"confirmation": "APAGAR"})

    with ThreadPoolExecutor(2) as pool:
        closing, deleting = pool.submit(close), pool.submit(remove)
        results = closing.result().status_code, deleting.result().status_code
    assert results in [(200, 409), (423, 200)]
    if results[1] == 200:
        ok(api.post(f"/api/contracts/{key}/restore", headers=h))
        assert ok(api.get(path, headers=h))["status"] == "draft"
        ok(api.put(path, headers=h, json=closure))
    assert ok(api.get(path, headers=h))["status"] == "closed"
    assert api.get(photo, headers=h).content == image
