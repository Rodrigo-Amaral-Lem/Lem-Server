# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Overlapping real exports: user/report isolation, overload and recovery."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from io import BytesIO
from threading import Barrier, Event
from zipfile import ZipFile
import importlib
import json
import uuid

import pytest
from PIL import Image
from pypdf import PdfReader
from fastapi.testclient import TestClient

from app.main import TEMPLATE
from test_workflow import setup, upload, answers, add_signature


def test_bundle_reads_each_original_once(exports):
    from sqlalchemy import event
    client, admin, _, app, _, reports = exports
    engine = app.state.factory.kw['bind']
    originals = []
    def observe(connection, cursor, statement, params, context, many):
        if statement.lstrip().lower().startswith('select photos.data '):
            originals.append(params)
    event.listen(engine,'before_cursor_execute',observe)
    try:
        response=client.get(f'/api/reports/{reports[0]["id"]}/bundle',headers=admin)
        check_export(response,'bundle',reports[0],reports)
        assert len(originals)==len(reports[0]['document']['photos'])
    finally:
        event.remove(engine,'before_cursor_execute',observe)


@pytest.fixture
def exports(tmp_path, monkeypatch):
    monkeypatch.setenv("LEM_EXPORT_CONCURRENCY", "4")
    client, admin, contract, app = setup.__wrapped__(tmp_path, monkeypatch)
    cid = contract["id"]
    assert upload(client, admin, cid, True).status_code == 200
    chargers = client.get(f"/api/contracts/{cid}/chargers", headers=admin).json()
    users = []
    for n in range(3):
        email = f"export-{n}@example.test"
        created = client.post("/api/users", headers=admin, json={
            "email": email, "name": f"Export Technician {n}", "password": "ExportPassword123!"})
        assert created.status_code == 200, created.text
        uid = created.json()["id"]
        if n < 2:
            assert client.put(f"/api/contracts/{cid}/users/{uid}", headers=admin).status_code == 200
        token = client.post("/api/login", json={"email": email, "password": "ExportPassword123!"}).json()["token"]
        users.append({"Authorization": "Bearer " + token})
    reports = []
    for n, headers in enumerate(users[:2]):
        rid, pid = str(uuid.uuid4()), str(uuid.uuid4())
        marker = f"EXPORT-PRIVATE-{n}-END"
        payload = {"answers": answers()}
        payload["answers"]["FIN.03"] = {"value": "", "notes": marker}
        body = {"checklist_version": TEMPLATE["version"], "charger_id": chargers[n]["id"],
                "base_revision": 0, "status": "draft", "payload": payload}
        result = client.put(f"/api/reports/{rid}", headers=headers, json=body)
        assert result.status_code == 200, result.text
        image = BytesIO()
        Image.new("RGB", (60, 40), (n * 100, 40, 220)).save(image, "PNG")
        original = image.getvalue()
        result = client.put(f"/api/reports/{rid}/photos/{pid}", headers=headers,
            data={"checkpoint": "EST.01", "base_revision": "1"},
            files={"file": (f"original-{n}.png", original, "image/png")})
        assert result.status_code == 200, result.text
        add_signature(client, headers, rid, payload)
        result = client.put(f"/api/reports/{rid}", headers=headers,
                            json={**body, "base_revision": 1, "status": "closed"})
        assert result.status_code == 200, result.text
        reports.append({"id": rid, "photo": pid, "original": original, "marker": marker,
                        "document": result.json(), "owner": headers})
    return client, admin, contract, app, users, reports


def check_export(response, kind, report, others):
    assert response.status_code == 200, response.text[:200]
    if kind == "bundle":
        assert response.headers["content-type"] == "application/zip"
        assert int(response.headers["content-length"]) == len(response.content)
        with ZipFile(BytesIO(response.content)) as archive:
            assert archive.testzip() is None
            document = json.loads(archive.read(next(name for name in archive.namelist() if name.endswith(".json"))))
            assert document["id"] == report["id"]
            assert document["owner_id"] == report["document"]["owner_id"]
            assert document["revision"] == report["document"]["revision"]
            identifier = document["identifier"]
            original = archive.read(f'fotografias/EST.01/{identifier}_{report["photo"]}.png')
            assert original == report["original"]
            for photo in document["photos"]:
                data = archive.read(f'fotografias/{photo["checkpoint"]}/{identifier}_{photo["id"]}.png')
                assert sha256(data).hexdigest() == photo["digest"]
            pdf = archive.read(document["identifier"] + ".pdf")
    else:
        assert response.headers["content-type"] == "application/pdf"
        pdf = response.content
    text = " ".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
    assert report["marker"] in text
    assert all(other["marker"] not in text for other in others if other["id"] != report["id"])


@pytest.mark.parametrize("same_report", [True, False])
def test_simultaneous_pdf_zip_keep_report_and_user_data_separate(exports, monkeypatch, same_report):
    client, admin, _, app, users, reports = exports
    renderer = importlib.import_module("app.pdf_report")
    build = renderer.build_pdf
    gate = Barrier(4)

    def concurrent_build(*args):
        gate.wait(timeout=15)
        return build(*args)

    monkeypatch.setattr(renderer, "build_pdf", concurrent_build)
    jobs = [(users[0], "pdf", reports[0]), (users[1], "bundle", reports[0]),
            (admin, "pdf", reports[0 if same_report else 1]),
            (users[0], "bundle", reports[0 if same_report else 1])]
    with client, ThreadPoolExecutor(4) as pool:
        pending = [(pool.submit(client.get, f'/api/reports/{report["id"]}/{kind}', headers=headers), kind, report)
                   for headers, kind, report in jobs]
        for future, kind, report in pending:
            check_export(future.result(timeout=30), kind, report, reports)
    assert app.state.factory.kw["bind"].pool.checkedout() == 0


def test_busy_exports_do_not_block_catalogue_or_sync_and_recover(exports, monkeypatch):
    client, admin, contract, app, users, reports = exports
    renderer = importlib.import_module("app.pdf_report")
    build = renderer.build_pdf
    gate, release = Barrier(5), Event()

    def blocked(*args):
        gate.wait(timeout=15)
        assert release.wait(15)
        return build(*args)

    monkeypatch.setattr(renderer, "build_pdf", blocked)
    report = reports[0]
    with client, ThreadPoolExecutor(4) as pool:
        pending = [pool.submit(client.get, f'/api/reports/{report["id"]}/{kind}', headers=users[n % 2])
                   for n, kind in enumerate(("pdf", "bundle", "pdf", "bundle"))]
        try:
            gate.wait(timeout=15)
            for kind in ("pdf", "bundle"):
                busy = client.get(f'/api/reports/{report["id"]}/{kind}', headers=admin)
                assert busy.status_code == 503 and busy.headers["retry-after"] == "15"
            catalogue = client.get(f'/api/contracts/{contract["id"]}/chargers', headers=admin)
            assert catalogue.status_code == 200
            draft = client.put(f'/api/reports/{uuid.uuid4()}', headers=users[0], json={
                "checklist_version": TEMPLATE["version"], "charger_id": catalogue.json()[0]["id"],
                "base_revision": 0, "status": "draft", "payload": {"answers": {}}})
            assert draft.status_code == 200, draft.text
        finally:
            release.set()
        assert all(future.result(timeout=30).status_code == 200 for future in pending)
    monkeypatch.setattr(renderer, "build_pdf", build)
    check_export(client.get(f'/api/reports/{report["id"]}/bundle', headers=admin), "bundle", report, reports)
    assert app.state.factory.kw["bind"].pool.checkedout() == 0


@pytest.mark.parametrize("kind", ["pdf", "bundle"])
def test_export_errors_release_resources_and_allow_retry(exports, monkeypatch, kind):
    client, admin, _, app, _, reports = exports
    main = importlib.import_module("app.main")
    renderer = importlib.import_module("app.pdf_report")
    build, spool_factory = renderer.build_pdf, main.SpooledTemporaryFile
    spools = []

    def capture(*args, **kwargs):
        spool = spool_factory(*args, **kwargs)
        spools.append(spool)
        return spool

    def fail(*args):
        raise OSError("simulated export failure")

    monkeypatch.setattr(main, "SpooledTemporaryFile", capture)
    monkeypatch.setattr(renderer, "build_pdf", fail)
    report = reports[0]
    url = f'/api/reports/{report["id"]}/{kind}'
    # More failures than admission slots catches slots leaked on error paths.
    with TestClient(app, headers={"X-Lem-Client-Version": "0.5.4"}, raise_server_exceptions=False) as failing_client:
        for _ in range(5):
            assert failing_client.get(url, headers=admin).status_code == 500
            assert all(spool.closed for spool in spools)
            assert app.state.factory.kw["bind"].pool.checkedout() == 0
    monkeypatch.setattr(renderer, "build_pdf", build)
    check_export(client.get(url, headers=admin), kind, report, reports)


@pytest.mark.parametrize("kind", ["pdf", "bundle"])
def test_export_access_and_lifecycle_states(exports, kind):
    client, admin, contract, app, users, reports = exports
    report = reports[0]
    url = f'/api/reports/{report["id"]}/{kind}'
    assert client.get(url).status_code in (401, 403)
    assert client.get(url, headers={"Authorization": "Bearer expired"}).status_code == 401
    assert client.get(url, headers=users[2]).status_code == 403
    check_export(client.get(url, headers=users[1]), kind, report, reports)
    assert client.post(f'/api/contracts/{contract["id"]}/trash', headers=admin,
                       json={"confirmation": "APAGAR"}).status_code == 200
    assert client.get(url, headers=admin).status_code == 423
    assert client.post(f'/api/contracts/{contract["id"]}/restore', headers=admin).status_code == 200
    check_export(client.get(url, headers=users[1]), kind, report, reports)
    assert client.delete(f'/api/reports/{report["id"]}?base_revision=2', headers=admin).status_code == 200
    assert client.get(url, headers=users[1]).status_code == 410
    assert client.get(f'/api/reports/{uuid.uuid4()}/{kind}', headers=admin).status_code == 404
    assert app.state.factory.kw["bind"].pool.checkedout() == 0


def test_interrupted_zip_stream_closes_spool_and_allows_retry(exports, monkeypatch):
    client, admin, _, app, _, reports = exports
    main = importlib.import_module("app.main")
    factory, stream = main.SpooledTemporaryFile, main.StreamingResponse.stream_response
    spools = []

    def capture(*args, **kwargs):
        spool = factory(*args, **kwargs)
        spools.append(spool)
        return spool

    async def disconnect(response, send):
        await send({"type": "http.response.start", "status": response.status_code,
                    "headers": response.raw_headers})
        await send({"type": "http.response.body", "body": b"PKpartial", "more_body": True})
        assert app.state.factory.kw["bind"].pool.checkedout() == 0
        raise OSError("disconnected during ZIP download")

    monkeypatch.setattr(main, "SpooledTemporaryFile", capture)
    monkeypatch.setattr(main.StreamingResponse, "stream_response", disconnect)
    report = reports[0]
    url = f'/api/reports/{report["id"]}/bundle'
    with pytest.raises(Exception):
        client.get(url, headers=admin)
    assert spools and all(spool.closed for spool in spools)
    monkeypatch.setattr(main.StreamingResponse, "stream_response", stream)
    check_export(client.get(url, headers=admin), "bundle", report, reports)
    assert all(spool.closed for spool in spools)
