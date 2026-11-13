from app.main import TEMPLATE
"""Local attack regressions; never contact a deployed server or use real accounts."""

from io import BytesIO
from zipfile import ZipFile
import pytest
import openpyxl
from fastapi import HTTPException
from app.security import LoginLimiter
from app.importer import workbook, analyse, parse
from test_workflow import setup, upload


def test_login_spaces_and_case_do_not_bypass_limit(setup):
    c, h, contract, app = setup
    # Setup used one successful login for this pair.
    for n in range(4):
        response = c.post(
            "/api/login", json={"email": " " * n + "ADMIN@LEM.PT" + " " * n, "password": "wrong"}
        )
        assert response.status_code == 401
    blocked = c.post("/api/login", json={"email": "\tadmin@lem.pt ", "password": "wrong"})
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "60"


def test_unknown_user_still_verifies_password(setup, monkeypatch):
    import app.main as main

    c, _, _, _ = setup
    calls = []
    monkeypatch.setattr(main, "verify", lambda password, encoded: calls.append(encoded) or False)
    result = c.post("/api/login", json={"email": "absent@example.invalid", "password": "wrong"})
    assert result.status_code == 401 and len(calls) == 1


def test_independent_buckets_expiry_capacity():
    t = [100.0]
    limiter = LoginLimiter(clock=lambda: t[0])
    for n in range(20):
        limiter.consume(f"ip-{n}", "same@example.invalid")
    with pytest.raises(HTTPException) as e:
        limiter.consume("new-ip", " SAME@example.invalid ")
    assert e.value.status_code == 429
    limiter = LoginLimiter(clock=lambda: t[0])
    for n in range(30):
        limiter.consume("same-ip", f"user-{n}")
    with pytest.raises(HTTPException):
        limiter.consume("same-ip", "another")
    limiter = LoginLimiter(clock=lambda: t[0], capacity=3)
    limiter.consume("a", "a")
    with pytest.raises(HTTPException):
        limiter.consume("b", "b")
    assert len(limiter.buckets) == 3
    t[0] += 61
    limiter.consume("b", "b")
    assert len(limiter.buckets) == 3


def test_oversized_bodies_before_parsing_including_streaming(setup):
    c, h, _, _ = setup
    assert (
        c.post(
            "/api/login", content=b" " * 4097, headers={"Content-Type": "application/json"}
        ).status_code
        == 413
    )

    def stream():
        yield b"{" + b" " * 3000
        yield b" " * 3000 + b"}"

    result = c.post("/api/login", content=stream(), headers={"Content-Type": "application/json"})
    assert result.status_code == 413, result.text
    result = c.put(
        "/api/reports/invalid",
        headers={**h, "Content-Type": "application/json"},
        content=b" " * 524289,
    )
    assert result.status_code == 413


def xlsx_patch(filename, transform):
    w = openpyxl.Workbook()
    w.active.append(["ID"])
    w.active.append(["TEST"])
    source = BytesIO()
    w.save(source)
    target = BytesIO()
    with ZipFile(source) as old, ZipFile(target, "w") as new:
        for name in old.namelist():
            data = old.read(name)
            new.writestr(name, transform(data) if name == filename else data)
    return target.getvalue()


def test_xml_entities_and_exaggerated_dimensions_rejected():
    data = xlsx_patch(
        "xl/workbook.xml",
        lambda b: b'<!DOCTYPE workbook [<!ENTITY label "INJECTED">]>'
        + b.replace(b'name="Sheet"', b'name="&label;"'),
    )
    with pytest.raises(HTTPException) as e:
        analyse(data)
    assert e.value.status_code == 422
    data = xlsx_patch("xl/worksheets/sheet1.xml", lambda b: b.replace(b"A1:A2", b"A1:XFD100000"))
    with pytest.raises(HTTPException) as e:
        workbook(data)
    assert e.value.status_code == 422


def test_cross_contract_photo_and_export_permissions(setup):
    c, admin, contract, app = setup
    upload(c, admin, contract["id"], True)
    charger = c.get("/api/contracts/" + contract["id"] + "/chargers", headers=admin).json()[0]
    import uuid

    rid = str(uuid.uuid4())
    assert (
        c.put(
            "/api/reports/" + rid,
            headers=admin,
            json={"checklist_version": TEMPLATE["version"], "charger_id": charger["id"], "base_revision": 0, "payload": {"answers": {}}},
        ).status_code
        == 200
    )
    c.post(
        "/api/users",
        headers=admin,
        json={
            "email": "outsider@example.invalid",
            "name": "Outsider",
            "password": "StrongOutsider123!",
        },
    )
    token = c.post(
        "/api/login", json={"email": "outsider@example.invalid", "password": "StrongOutsider123!"}
    ).json()["token"]
    outsider = {"Authorization": "Bearer " + token}
    for suffix in ["", "/exists", "/pdf", "/bundle", "/photos/" + str(uuid.uuid4())]:
        assert c.get("/api/reports/" + rid + suffix, headers=outsider).status_code == 403
    assert c.delete("/api/reports/" + rid + "?base_revision=1", headers=outsider).status_code == 403
    assert c.get("/api/users", headers=outsider).status_code == 403
