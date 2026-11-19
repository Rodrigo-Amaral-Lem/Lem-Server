# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Reproduced import and PDF failures from the general 0.1.35 review."""

import json
import pytest
from test_workflow import setup, upload, xlsx, MAPPING, answers
from app.main import TEMPLATE
from app.pdf_report import build_pdf


@pytest.mark.parametrize("row", [100, 1.5, True, False, None, "invalid", float("inf")])
def test_bad_header_row_is_validation_error_without_partial_import(setup, row):
    client, headers, contract, app = setup
    cid = contract["id"]
    response = client.post(
        f"/api/contracts/{cid}/import",
        headers=headers,
        files={"file": ("test.xlsx", xlsx())},
        data={"mapping": json.dumps({**MAPPING, "header_row": row}), "commit": "true"},
    )
    assert response.status_code == 422, response.text
    assert client.get(f"/api/contracts/{cid}/chargers", headers=headers).json() == []
    # A rejected import must not prevent the next valid one.
    assert upload(client, headers, cid, True).json() == {"created": 2, "updated": 0}


def test_pdf_supports_metadata_longer_than_one_page():
    report = {
        "id": "test",
        "revision": 1,
        "status": "closed",
        "technician": "Técnico",
        "started": "2026-11-10T01:00:00+00:00",
        "finished": "2026-11-10T02:00:00+00:00",
        "snapshot": {
            "charger": {"external_id": "TEST", "address": "Morada detalhada " * 1500},
            "contract": {"name": "Test", "company": "Test"},
        },
        "template": TEMPLATE,
        "payload": {"answers": answers()},
    }
    data = build_pdf(report, [])
    assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
    from pypdf import PdfReader
    from io import BytesIO
    assert len(PdfReader(BytesIO(data)).pages) > 1


# --- Review after 0.1.35 R2: unexportable closed reports and import preview ---

import re
import uuid
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile, ZIP_DEFLATED
import openpyxl
from PIL import Image
from app.images import inspect_image, InvalidImage
from app.importer import analyse


def _jpeg(size=(1600, 1200)):
    b = BytesIO()
    Image.effect_noise(size, 60).convert("RGB").save(b, "JPEG", quality=90)
    return b.getvalue()


def _png():
    b = BytesIO()
    Image.new("RGB", (64, 64), "red").save(b, "PNG")
    return b.getvalue()


def _damaged_png():
    data = bytearray(_png())
    data[data.find(b"IDAT") + 6] ^= 0xFF
    return bytes(data)


def _report(found=None):
    return {
        "id": "test",
        "revision": 1,
        "status": "closed",
        "technician": "Técnico",
        "started": "2026-11-10T01:00:00+00:00",
        "finished": "2026-11-10T02:00:00+00:00",
        "snapshot": {
            "charger": {"external_id": "TEST"},
            "contract": {"name": "Test", "company": "Test"},
        },
        "template": TEMPLATE,
        "payload": {"answers": {**answers(), **(found or {})}},
    }


def test_complete_images_are_accepted_with_their_type():
    assert inspect_image(_jpeg()) == "image/jpeg"
    assert inspect_image(_png()) == "image/png"


@pytest.mark.parametrize("kind", ["truncated-jpeg", "damaged-png", "text", "gif"])
def test_undecodable_images_are_validation_errors(kind):
    gif = BytesIO()
    Image.new("RGB", (8, 8)).save(gif, "GIF")
    data = {
        "truncated-jpeg": _jpeg()[:20000],
        "damaged-png": _damaged_png(),
        "text": b"not an image",
        "gif": gif.getvalue(),
    }[kind]
    with pytest.raises(InvalidImage):
        inspect_image(data)


@pytest.mark.parametrize("kind", ["truncated-jpeg", "damaged-png"])
def test_damaged_photo_upload_is_422_not_stored(setup, kind):
    client, headers, contract, app = setup
    cid = contract["id"]
    upload(client, headers, cid, True)
    charger = client.get(f"/api/contracts/{cid}/chargers", headers=headers).json()[0]
    rid = str(uuid.uuid4())
    body = {
        "checklist_version": TEMPLATE["version"], "charger_id": charger["id"],
        "base_revision": 0,
        "status": "draft",
        "payload": {"answers": {}},
    }
    assert client.put(f"/api/reports/{rid}", headers=headers, json=body).status_code == 200
    data = _jpeg()[:20000] if kind == "truncated-jpeg" else _damaged_png()
    response = client.put(
        f"/api/reports/{rid}/photos/{uuid.uuid4()}",
        headers=headers,
        data={"checkpoint": "EST.01", "base_revision": "1"},
        files={"file": ("foto.jpg", data, "application/octet-stream")},
    )
    assert response.status_code == 422, response.text
    assert client.get(f"/api/reports/{rid}", headers=headers).json()["photos"] == []


@pytest.mark.parametrize("length", [3000, 8000])
def test_pdf_supports_text_answer_longer_than_one_page(length):
    item = next(i["id"] for s in TEMPLATE["sections"] for i in s["items"] if i["kind"] == "text")
    data = build_pdf(_report({item: {"value": "medição " * (length // 8), "notes": ""}}), [])
    assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")


def test_pdf_reports_previously_stored_damaged_photo_instead_of_failing():
    photos = [
        SimpleNamespace(checkpoint="EST.01", filename="boa.jpg", data=_jpeg()),
        SimpleNamespace(checkpoint="EST.01", filename="cortada.jpg", data=_jpeg()[:20000]),
    ]
    data = build_pdf(_report(), photos)
    assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
    # Branding may embed its own colour image and alpha mask. Only the valid
    # evidence should add an image beyond the logo-only document.
    baseline = build_pdf(_report(), [])
    assert data.count(b"/Subtype /Image") == baseline.count(b"/Subtype /Image") + 1


def test_preview_of_workbook_without_dimension_shows_the_table():
    w = openpyxl.Workbook()
    s = w.active
    s.title = "Parque"
    s.append(["Identificador", "Modelo", "Local"])
    for n in range(5):
        s.append([f"ID{n}", "DC60", "Aveiro"])
    original = BytesIO()
    w.save(original)
    stripped = BytesIO()
    with (
        ZipFile(BytesIO(original.getvalue())) as source,
        ZipFile(stripped, "w", ZIP_DEFLATED) as out,
    ):
        for entry in source.infolist():
            content = source.read(entry)
            if entry.filename.endswith("sheet1.xml"):
                content, count = re.subn(rb"<dimension[^>]*/>", b"", content)
                assert count == 1
            out.writestr(entry, content)
    sheet = analyse(stripped.getvalue())["sheets"][0]
    assert sheet["sample"] == analyse(original.getvalue())["sheets"][0]["sample"]
    assert sheet["sample"][0] == ["Identificador", "Modelo", "Local"] and len(sheet["sample"]) == 6


# --- Bounded exports, private API schema and IPv6 login buckets ---

from tempfile import SpooledTemporaryFile
from app.exports import write_bundle, file_chunks
from app.security import LoginLimiter, client_network


def test_bundle_is_written_one_photo_at_a_time_and_streamed():
    loads = []

    class Lazy:
        def __init__(self, pid, mime, data):
            self.id, self.checkpoint, self.mime, self._data = pid, "EST.01", mime, data

        @property
        def data(self):
            loads.append(self.id)
            return self._data

    jpeg, png = _jpeg((320, 240)), _png()
    photos = [Lazy("a", "image/jpeg", jpeg), Lazy("b", "image/png", png)]
    spool = SpooledTemporaryFile(max_size=1024)
    size = write_bundle(spool, {"id": "r", "nome": "ação"}, photos, b"%PDF-test")
    data = b"".join(file_chunks(spool))
    assert spool.closed and len(data) == size and loads == ["a", "b"]
    with ZipFile(BytesIO(data)) as z:
        assert sorted(z.namelist()) == sorted([
            "fotografias/EST.01/Carregador_Contrato_a.jpg",
            "fotografias/EST.01/Carregador_Contrato_b.png",
            "Carregador_Contrato.json",
            "Carregador_Contrato.pdf",
        ])
        assert (
            z.read("fotografias/EST.01/Carregador_Contrato_a.jpg") == jpeg
            and z.read("fotografias/EST.01/Carregador_Contrato_b.png") == png
        )
        assert z.read("Carregador_Contrato.pdf") == b"%PDF-test"
        assert json.loads(z.read("Carregador_Contrato.json")) == {"id": "r", "nome": "ação"}
        assert z.testzip() is None


def test_ipv6_addresses_in_one_subscriber_prefix_share_a_login_bucket():
    assert client_network("2001:db8:1:2::1") == client_network("2001:db8:1:2:ffff::9")
    assert client_network("2001:db8:1:2::1") != client_network("2001:db8:1:3::1")
    assert client_network("::ffff:192.0.2.7") == client_network("192.0.2.7") == "192.0.2.7"
    assert client_network("testclient") == "testclient" and client_network("") == ""
    limiter = LoginLimiter(clock=lambda: 100.0)
    for n in range(30):
        limiter.consume(f"2001:db8:1:2::{n + 1:x}", f"user{n}@example.invalid")
    with pytest.raises(Exception) as blocked:
        limiter.consume("2001:db8:1:2:aaaa::1", "another@example.invalid")
    assert blocked.value.status_code == 429
    limiter.consume("2001:db8:1:3::1", "another@example.invalid")


def test_api_schema_is_private_unless_enabled(setup, tmp_path, monkeypatch):
    client, headers, _, _ = setup
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path, headers=headers).status_code == 404
    monkeypatch.setenv("LEM_API_DOCS", "1")
    from fastapi.testclient import TestClient
    from app.main import create_app

    enabled = TestClient(create_app("sqlite:///" + str(tmp_path / "docs.db")))
    assert enabled.get("/openapi.json").status_code == 200


def test_closed_report_exports_with_bounded_photo_loading(setup):
    client, headers, contract, app = setup
    cid = contract["id"]
    upload(client, headers, cid, True)
    charger = client.get(f"/api/contracts/{cid}/chargers", headers=headers).json()[0]
    rid = str(uuid.uuid4())
    body = {
        "checklist_version": TEMPLATE["version"], "charger_id": charger["id"],
        "base_revision": 0,
        "status": "draft",
        "payload": {"answers": answers()},
    }
    draft = client.put(f"/api/reports/{rid}", headers=headers, json=body)
    assert draft.status_code == 200
    identifier = draft.json()["identifier"]
    required = [i["id"] for s in TEMPLATE["sections"] for i in s["items"] if i["photo_required"]]
    originals = {}
    for checkpoint in required:
        pid = str(uuid.uuid4())
        name = f"fotografias/{checkpoint}/{identifier}_{pid}.jpg"
        originals[name] = _jpeg((320, 240))
        response = client.put(
            f"/api/reports/{rid}/photos/{pid}",
            headers=headers,
            data={"checkpoint": checkpoint, "base_revision": "1"},
            files={"file": ("foto.jpg", originals[name])},
        )
        assert response.status_code == 200, response.text
    from test_workflow import add_signature
    add_signature(client, headers, rid, body["payload"])
    closed = client.put(
        f"/api/reports/{rid}",
        headers=headers,
        json={**body, "base_revision": 1, "status": "closed"},
    )
    assert closed.status_code == 200, closed.text
    document = client.get(f"/api/reports/{rid}/pdf", headers=headers)
    assert document.status_code == 200 and document.content.startswith(b"%PDF-")
    archive = client.get(f"/api/reports/{rid}/bundle", headers=headers)
    assert archive.status_code == 200
    assert int(archive.headers["content-length"]) == len(archive.content)
    with ZipFile(BytesIO(archive.content)) as z:
        assert z.testzip() is None and z.read(closed.json()["identifier"] + ".pdf").startswith(b"%PDF-")
        for name, data in originals.items():
            assert z.read(name) == data
