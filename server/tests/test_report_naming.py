# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote
from zipfile import ZipFile
import json
import uuid
from copy import deepcopy

import pytest
from pypdf import PdfReader
from app.main import TEMPLATE
from app.models import Contract, Report
from app.report_naming import EVIDENCE_EXTENSIONS, report_identifier, component, content_disposition
from test_workflow import setup, upload, answers, add_signature

CASES = json.loads((Path(__file__).parents[2] / "shared/report-naming-cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_shared_report_names(case):
    assert report_identifier(case["report"]) == case["expected"]


def test_bounded_unicode_names_and_header():
    assert component("🚗" * 100, "fallback", 73) == "🚗" * 18
    report = {"status": "closed", "finished": "2026-11-16T17:35:08Z", "snapshot": {
        "charger": {"external_id": "🚗" * 100}, "contract": {"name": "á" * 200}}}
    name = report_identifier(report)
    assert len((name + ".pdf").encode("utf-8")) < 255
    header = content_disposition(name, "pdf")
    assert header.isascii() and "\r" not in header and "\n" not in header
    assert unquote(header.split("filename*=UTF-8''")[1]) == name + ".pdf"


def test_frozen_identifier_matches_catalogue_pdf_zip_and_original_uuid(setup):
    client, headers, contract, app = setup
    assert upload(client, headers, contract["id"], True).status_code == 200
    catalogue_url = f'/api/contracts/{contract["id"]}/chargers'
    charger = client.get(catalogue_url, headers=headers).json()[0]
    rid = str(uuid.uuid4())
    url = f"/api/reports/{rid}"
    payload = {"answers": answers(), "started": "2026-07-01T22:05:06Z", "finished": "2026-07-01T23:05:06Z"}
    payload["answers"]["EST.15"] = {"value": "NA", "notes": "Sem POS", "suggested_notes": "Sem POS"}
    payload["answers"]["EST.18"] = {"value": "", "notes": "POS referido pelo técnico"}
    body = {"checklist_version": TEMPLATE["version"], "charger_id": charger["id"],
            "status": "draft", "base_revision": 0, "payload": payload}
    draft = client.put(url, headers=headers, json=body)
    assert draft.status_code == 200, draft.text
    expected = charger["external_id"] + "_" + component(contract["name"], "Contrato", 112)
    assert draft.json()["identifier"] == expected
    add_signature(client, headers, rid, payload)
    closed = client.put(url, headers=headers, json={**body, "base_revision": 1, "status": "closed"})
    assert closed.status_code == 200, closed.text
    document = closed.json()
    assert document["identifier"] == expected and document["id"] == rid
    # Catalogue renaming must not rename historical reports or subsequent downloads.
    with app.state.factory() as db:
        db.get(Contract, contract["id"]).name = "Renamed live contract"
        saved = db.get(Report, rid)
        schema = deepcopy(saved.template)
        old_item = next(item for section in schema["sections"] for item in section["items"] if item["id"] == "EST.15")
        old_item["label"] = old_item["label"].replace("(TPM)", "(POS)")
        saved.template = schema
        db.commit()
    summary = client.get(catalogue_url, headers=headers).json()[0]["reports"][0]
    assert summary["id"] == rid and summary["identifier"] == expected
    assert summary["finished"] == document["finished"]
    saved_doc = client.get(url, headers=headers).json()
    assert saved_doc["identifier"] == expected and saved_doc["template"] == schema
    assert saved_doc["payload"]["answers"]["EST.15"] == payload["answers"]["EST.15"]
    photo = document["photos"][0]
    ext = EVIDENCE_EXTENSIONS[photo["mime"]]
    evidence = client.get(f'{url}/photos/{photo["id"]}', headers=headers)
    evidence_name = f'{expected}_{photo["id"]}.{ext}'
    assert evidence.status_code == 200 and evidence.headers["content-disposition"].startswith("inline;")
    assert unquote(evidence.headers["content-disposition"].split("filename*=UTF-8''")[1]) == evidence_name
    for kind, extension in (("pdf", "pdf"), ("bundle", "zip")):
        result = client.get(url + "/" + kind, headers=headers)
        assert result.status_code == 200, result.text[:100]
        assert unquote(result.headers["content-disposition"].split("filename*=UTF-8''")[1]) == expected + "." + extension
        if kind == "bundle":
            with ZipFile(BytesIO(result.content)) as archive:
                assert [name for name in archive.namelist() if name.endswith(".pdf")] == [expected + ".pdf"]
                assert json.loads(archive.read(expected + ".json"))["identifier"] == expected
                assert archive.read(f'fotografias/{photo["checkpoint"]}/{evidence_name}') == evidence.content
                pdf = archive.read(expected + ".pdf")
        else:
            pdf = result.content
        reader = PdfReader(BytesIO(pdf))
        assert reader.metadata.title == expected
        text = " ".join(page.extract_text() for page in reader.pages)
        assert expected in text.replace("\n", "")
        assert "(TPM)" in text and "Sem TPM" in text
        assert "POS referido pelo técnico" in text
