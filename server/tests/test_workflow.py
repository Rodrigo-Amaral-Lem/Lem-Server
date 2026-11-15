# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

import json, uuid, sys, os
from pathlib import Path
from io import BytesIO
from zipfile import ZipFile
from types import SimpleNamespace
import pytest, openpyxl
from PIL import Image
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import create_app, TEMPLATE
from app.models import Photo, Report
from app.pdf_report import build_pdf


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "StrongPassword123!")
    app = create_app("sqlite:///" + str(tmp_path / "test.db"))
    c = TestClient(app, headers={"X-Lem-Client-Version": "0.2.0"})
    token = c.post(
        "/api/login", json={"email": "admin@lem.pt", "password": "StrongPassword123!"}
    ).json()["token"]
    headers = {"Authorization": "Bearer " + token}
    contract = c.post(
        "/api/contracts",
        headers=headers,
        json={"company": "Galp", "year": 2026, "semester": 1, "kind": "TRE"},
    ).json()
    return c, headers, contract, app


def xlsx(ids=("GALP-001", "GALP-002")):
    w = openpyxl.Workbook()
    s = w.active
    s.title = "Parque"
    s.append(["Identificador", "Modelo", "Local", "Série", "CPE", "Custom"])
    for i in ids:
        s.append([i, "DC60", "Aveiro", "000012", "PT0002000000000000AA", "extra"])
    b = BytesIO()
    w.save(b)
    return b.getvalue()


MAPPING = {
    "sheet": "Parque",
    "header_row": 1,
    "columns": {
        "external_id": "Identificador",
        "model": "Modelo",
        "location": "Local",
        "serial": "Série",
        "cpe": "CPE",
    },
}


def upload(c, h, cid, commit=False, data=None):
    return c.post(
        "/api/contracts/" + cid + "/import",
        headers=h,
        files={"file": ("chargers.xlsx", data or xlsx())},
        data={"mapping": json.dumps(MAPPING), "commit": str(commit).lower()},
    )


def answers():
    return {
        i["id"]: {"value": "NA", "notes": "Sem conector/veículo disponível" if i.get("na_notes_required") else ""}
        for s in TEMPLATE["sections"]
        for i in s["items"]
    }


def test_import_preview_atomic_upsert_and_profiles(setup):
    c, h, contract, app = setup
    cid = contract["id"]
    result = upload(c, h, cid).json()
    assert result["count"] == 2 and not result["errors"]
    assert c.get("/api/contracts/" + cid + "/chargers", headers=h).json() == []
    bad = upload(c, h, cid, True, xlsx(("A", "A")))
    assert bad.status_code == 422
    assert c.get("/api/contracts/" + cid + "/chargers", headers=h).json() == []
    assert upload(c, h, cid, True).json() == {"created": 2, "updated": 0}
    assert upload(c, h, cid, True).json() == {"created": 0, "updated": 2}
    catalogue = c.get("/api/contracts/" + cid + "/chargers", headers=h).json()
    assert len(catalogue) == 2
    assert (
        catalogue[0]["details"]["serial"] == "000012"
        and catalogue[0]["details"]["source"]["Custom"] == "extra"
    )
    assert c.get("/api/companies/Galp/import-profile", headers=h).json() == MAPPING


def test_reports_photos_pdf_zip_permissions_conflict(setup):
    c, h, contract, app = setup
    cid = contract["id"]
    upload(c, h, cid, True)
    charger = c.get("/api/contracts/" + cid + "/chargers", headers=h).json()[0]
    tech = c.post(
        "/api/users",
        headers=h,
        json={
            "email": "tech@example.test",
            "password": "TechPassword123!",
            "name": "Técnico João",
            "role": "tech",
        },
    ).json()["id"]
    token = c.post(
        "/api/login", json={"email": "tech@example.test", "password": "TechPassword123!"}
    ).json()["token"]
    t = {"Authorization": "Bearer " + token}
    assert c.get("/api/contracts", headers=t).json() == []
    assert c.get("/api/contracts/" + cid + "/chargers", headers=t).status_code == 403
    assert c.put("/api/contracts/" + cid + "/users/" + tech, headers=h).status_code == 200
    rid = str(uuid.uuid4())
    payload = {"answers": {}}
    body = {"checklist_version": TEMPLATE["version"], "charger_id": charger["id"], "base_revision": 0, "status": "draft", "payload": payload}
    r = c.put("/api/reports/" + rid, headers=t, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 1
    assert c.put("/api/reports/" + rid, headers=t, json=body).status_code == 409
    assert (
        c.put(
            "/api/reports/" + rid, headers=t, json={**body, "base_revision": 1, "status": "closed"}
        ).status_code
        == 422
    )
    a = answers()
    a["EST.01"]["value"] = "OK"
    a["SITE.02"] = {"value": "NOK", "notes": "Pintura com desgaste"}
    payload = {"answers": a}
    assert (
        c.put(
            "/api/reports/" + rid,
            headers=t,
            json={**body, "base_revision": 1, "payload": payload, "status": "closed"},
        ).status_code
        == 422
    )
    photo = BytesIO()
    Image.new("RGB", (640, 480), "#12665f").save(photo, format="JPEG")
    data = photo.getvalue()
    pid = str(uuid.uuid4())
    url = f"/api/reports/{rid}/photos/{pid}"
    assert (
        c.put(
            url,
            headers=t,
            files={"file": ("evidencia.jpg", data)},
            data={"checkpoint": "EST.01", "base_revision": "1"},
        ).status_code
        == 200
    )
    assert (
        c.put(
            url,
            headers=t,
            files={"file": ("evidencia.jpg", data)},
            data={"checkpoint": "EST.01", "base_revision": "1"},
        ).status_code
        == 200
    )
    assert c.get(url, headers=t).content == data
    assert c.put(f"/api/reports/{rid}/photos/{uuid.uuid4()}", headers=t,
                 files={"file": ("site.jpg", data)},
                 data={"checkpoint": "SITE.02", "base_revision": "1"}).status_code == 200
    closed = c.put(
        "/api/reports/" + rid,
        headers=t,
        json={**body, "base_revision": 1, "payload": payload, "status": "closed"},
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["revision"] == 2 and len(closed.json()["photos"]) == 2
    assert (
        c.put(
            "/api/reports/" + rid, headers=t, json={**body, "base_revision": 2, "payload": payload}
        ).status_code
        == 409
    )
    p = c.get("/api/reports/" + rid + "/pdf", headers=t)
    assert p.content.startswith(b"%PDF")
    zipbytes = c.get("/api/reports/" + rid + "/bundle", headers=t).content
    with ZipFile(BytesIO(zipbytes)) as z:
        assert z.read("report.pdf").startswith(b"%PDF")
        assert z.read(f"fotografias/EST.01/{pid}.jpg") == data
    with app.state.factory() as db:
        assert db.get(Photo, pid).data == data
    # Keep a demonstration PDF from the same tested renderer.
    root = Path(__file__).resolve().parents[2] / "examples"
    root.mkdir(exist_ok=True)
    (root / "report-demonstracao.pdf").write_bytes(p.content)
    assert (
        c.post(
            "/api/contracts",
            headers=t,
            json={"company": "Prio", "year": 2026, "semester": 2, "kind": "O&M"},
        ).status_code
        == 403
    )
    assert c.get(url).status_code in (401, 403)
    c.post("/api/logout", headers=t)
    assert c.get("/api/contracts", headers=t).status_code == 401


def test_nok_requires_notes_and_snapshot_survives_reimport(setup):
    c, h, contract, app = setup
    cid = contract["id"]
    upload(c, h, cid, True)
    charger = c.get("/api/contracts/" + cid + "/chargers", headers=h).json()[0]
    rid = str(uuid.uuid4())
    a = answers()
    a["SITE.02"]["value"] = "NOK"
    body = {
        "checklist_version": TEMPLATE["version"], "charger_id": charger["id"],
        "base_revision": 0,
        "payload": {"answers": a},
        "status": "closed",
    }
    assert c.put("/api/reports/" + rid, headers=h, json=body).status_code == 422
    body["status"] = "draft"
    assert c.put("/api/reports/" + rid, headers=h, json=body).status_code == 200
    w = openpyxl.load_workbook(BytesIO(xlsx()))
    w.active["B2"] = "New model"
    b = BytesIO()
    w.save(b)
    upload(c, h, cid, True, b.getvalue())
    assert c.get("/api/reports/" + rid, headers=h).json()["snapshot"]["charger"]["model"] == "DC60"
    assert (
        c.get("/api/contracts/" + cid + "/chargers", headers=h).json()[0]["details"]["model"]
        == "New model"
    )


def test_numeric_identifier_masks(setup):
    c, h, contract, app = setup
    w = openpyxl.Workbook()
    s = w.active
    s.title = "Parque"
    s.append(["Identificador", "Modelo", "Local", "Série", "CPE", "Custom"])
    s.append([12, "AC22", "Teste", 7, "PTTEST", "Extra"])
    s["A2"].number_format = "000000"
    s["D2"].number_format = "0000"
    b = BytesIO()
    w.save(b)
    result = upload(c, h, contract["id"], True, b.getvalue())
    assert result.status_code == 200, result.text
    charger = c.get("/api/contracts/" + contract["id"] + "/chargers", headers=h).json()[0]
    assert charger["external_id"] == "000012" and charger["details"]["serial"] == "0007"


def test_remove_draft_photo_permissions_revision_retry_and_required_evidence(setup):
    c, h, contract, app = setup
    cid = contract["id"]
    upload(c, h, cid, True)
    charger = c.get("/api/contracts/" + cid + "/chargers", headers=h).json()[0]
    rid = str(uuid.uuid4())
    other = str(uuid.uuid4())
    a = answers()
    a["EST.01"]["value"] = "OK"
    body = {
        "checklist_version": TEMPLATE["version"], "charger_id": charger["id"],
        "base_revision": 0,
        "payload": {"answers": a},
        "status": "draft",
    }
    assert c.put("/api/reports/" + rid, headers=h, json=body).status_code == 200
    assert c.put("/api/reports/" + other, headers=h, json=body).status_code == 200
    image = BytesIO()
    Image.new("RGB", (32, 24), "red").save(image, format="PNG")
    pid = str(uuid.uuid4())
    url = f"/api/reports/{rid}/photos/{pid}"
    assert (
        c.put(
            url,
            headers=h,
            files={"file": ("photo.png", image.getvalue())},
            data={"checkpoint": "EST.01", "base_revision": "1"},
        ).status_code
        == 200
    )
    assert c.delete(url + "?base_revision=0", headers=h).status_code == 409
    assert c.get(url, headers=h).status_code == 200
    assert (
        c.delete(f"/api/reports/{other}/photos/{pid}?base_revision=1", headers=h).status_code == 404
    )
    assert c.delete(url + "?base_revision=1").status_code in (401, 403)
    tech = c.post(
        "/api/users",
        headers=h,
        json={
            "email": "removal@example.test",
            "password": "TechPassword123!",
            "name": "Outro técnico",
            "role": "tech",
        },
    ).json()["id"]
    c.put("/api/contracts/" + cid + "/users/" + tech, headers=h)
    token = c.post(
        "/api/login", json={"email": "removal@example.test", "password": "TechPassword123!"}
    ).json()["token"]
    t = {"Authorization": "Bearer " + token}
    assert c.delete(url + "?base_revision=1", headers=t).status_code == 403
    removed = c.delete(url + "?base_revision=1", headers=h)
    assert removed.status_code == 200 and removed.json()["revision"] == 2
    assert c.get(url, headers=h).status_code == 404
    assert c.get("/api/reports/" + rid, headers=h).json()["photos"] == []
    assert (
        c.delete(url + "?base_revision=1", headers=h).json()["revision"] == 2
    ), "Retry must be idempotent"
    assert (
        c.put(
            "/api/reports/" + rid, headers=h, json={**body, "base_revision": 2, "status": "closed"}
        ).status_code
        == 422
    ), "Removed mandatory photo must block completion"
    assert c.get("/api/reports/" + rid + "/pdf", headers=h).status_code == 409
    assert c.get("/api/reports/" + rid + "/bundle", headers=h).status_code == 409
    a["EST.01"]["value"] = "NA"
    assert (
        c.put(
            "/api/reports/" + rid,
            headers=h,
            json={**body, "base_revision": 2, "payload": {"answers": a}, "status": "closed"},
        ).status_code
        == 200
    )
    with ZipFile(BytesIO(c.get("/api/reports/" + rid + "/bundle", headers=h).content)) as z:
        assert not any(n.startswith("fotografias/") for n in z.namelist())
    assert (
        c.delete(url + "?base_revision=3", headers=h).status_code == 409
    ), "Even retry must not edit a closed report"
    with app.state.factory() as db:
        assert db.get(Photo, pid) is None


def test_catalogue_uses_bulk_summaries_without_loading_report_payloads(setup):
    from sqlalchemy import event

    c, h, contract, app = setup
    cid = contract["id"]
    upload(c, h, cid, True, xlsx(tuple("BULK-" + str(i).zfill(3) for i in range(20))))
    rows = c.get("/api/contracts/" + cid + "/chargers", headers=h).json()
    rid = str(uuid.uuid4())
    assert (
        c.put(
            "/api/reports/" + rid,
            headers=h,
            json={
                "checklist_version": TEMPLATE["version"], "charger_id": rows[0]["id"],
                "base_revision": 0,
                "status": "draft",
                "payload": {"answers": {}, "large": "x" * 100000},
            },
        ).status_code
        == 200
    )
    queries = []
    engine = app.state.factory.kw["bind"]

    def count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        r = c.get("/api/contracts/" + cid + "/chargers", headers=h)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert r.status_code == 200 and len(r.json()) == 20
    assert r.json()[0]["reports"][0]["id"] == rid
    report_queries = [q for q in queries if "FROM reports" in q]
    # Token revocation is rechecked after the User lock; retention follows
    # the contract row lock. Still
    # bulk queries only: no report payload/photo loads or per-charger queries.
    assert len(report_queries) == 1 and len(queries) <= 8, queries
    selected = report_queries[0].split("FROM")[0]
    assert "reports.payload" not in selected and "reports.template" not in selected


def test_photo_metadata_includes_size_without_fetching_blobs(setup):
    from sqlalchemy import event

    c, h, contract, app = setup
    cid = contract["id"]
    upload(c, h, cid, True)
    charger = c.get("/api/contracts/" + cid + "/chargers", headers=h).json()[0]
    rid = str(uuid.uuid4())
    pid = str(uuid.uuid4())
    assert (
        c.put(
            "/api/reports/" + rid,
            headers=h,
            json={
                "checklist_version": TEMPLATE["version"], "charger_id": charger["id"],
                "base_revision": 0,
                "status": "draft",
                "payload": {"answers": {}},
            },
        ).status_code
        == 200
    )
    data = BytesIO()
    Image.new("RGB", (20, 20), "blue").save(data, format="PNG")
    assert (
        c.put(
            f"/api/reports/{rid}/photos/{pid}",
            headers=h,
            files={"file": ("evidence.png", data.getvalue())},
            data={"checkpoint": "EST.01", "base_revision": "1"},
        ).status_code
        == 200
    )
    queries = []
    engine = app.state.factory.kw["bind"]

    def record(conn, cursor, statement, params, context, many):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        report = c.get("/api/reports/" + rid, headers=h).json()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert report["photos"][0]["size"] == len(data.getvalue())
    sql = [q for q in queries if "FROM photos" in q]
    assert len(sql) == 1 and "length(photos.data)" in sql[0]
    assert "photos.data," not in sql[0].split("FROM")[0]
