# SPDX-License-Identifier: GPL-3.0-only
"""Boundary, migration and closed-correction safety regressions for checklist v7."""
from copy import deepcopy
from pathlib import Path
from io import BytesIO
import hashlib
import json
import uuid

import pytest
from PIL import Image
from sqlalchemy import select
from app.main import TEMPLATE, LEGACY_TEMPLATE
from app.checklist import corrected_draft_template, corrected_draft_payload
from app.maintenance_rules import calculate, apply_assessments, EARTH_FAIL_NOTE, EARTH_HIGH_NOTE, TRIP_NOTE
from app.maintenance_migration import upgrade_payload
from app.models import Report, Photo
from test_workflow import setup, upload, add_signature
from test_export_resources import make_report
from test_report_corrections import start, body

CASES = json.loads((Path(__file__).resolve().parents[2] / "shared/maintenance-cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES)
def test_exact_electrical_boundaries(case):
    answers = {key: {"value": case[name]} for key, name in
               (("MAN.11", "configured"), ("MAN.12", "trip"), ("MAN.18", "earth"))}
    result = calculate(answers)
    assert result.get("MAN.12", ("", ""))[0] == case["trip_state"]
    assert result.get("MAN.18", ("", ""))[0] == case["earth_state"]
    assert result.get("MAN.18", ("", ""))[1] == {"high": EARTH_HIGH_NOTE, "fail": EARTH_FAIL_NOTE, "": ""}[case["earth_note"]]


def test_custom_notes_and_dismissed_suggestions_are_not_replaced():
    answers = {"MAN.11": {"value": "1000 mA"}, "MAN.18": {"value": "51 Ω", "notes": "Nota do técnico"}}
    first = apply_assessments(answers, True)
    assert first["MAN.18"]["assessment"] == "NOK"
    assert first["MAN.18"]["notes"] == "Nota do técnico"
    answers["MAN.18"]["notes"] = ""
    first = apply_assessments(answers, True)
    assert first["MAN.18"]["suggested_notes"] == EARTH_FAIL_NOTE
    first["MAN.11"]["value"] = "300 mA"
    second = apply_assessments(first, True)
    assert second["MAN.18"]["assessment"] == "OK"
    assert second["MAN.18"]["notes"] == EARTH_HIGH_NOTE
    second["MAN.18"]["notes"] = ""
    second["MAN.18"].pop("suggested_notes")
    assert apply_assessments(second)["MAN.18"]["notes"] == ""


@pytest.mark.parametrize("raw,expected", [("0,03 A", "30 mA"), ("1 A", "1000 mA"),
    ("30.0 mA", "30 mA"), ("999,999 A", "999999 mA"), ("30", None), ("0.03", None),
    ("0.0001 A", None), ("1000 A", None), ("Regulação variável", None)])
def test_legacy_current_units_are_converted_only_when_unambiguous(raw, expected):
    old = {"answers": {"MAN.11": {"value": raw, "notes": "Manter"}}}
    answer = upgrade_payload(old)["answers"]["MAN.11"]
    assert old["answers"]["MAN.11"]["value"] == raw
    assert answer["notes"] == "Manter"
    if expected:
        assert answer["value"] == expected and "previous_value" not in answer
    else:
        assert answer["value"] == "" and answer["previous_value"] == raw


def test_template_migration_is_idempotent_and_closed_history_frozen():
    old = deepcopy(LEGACY_TEMPLATE)
    new = corrected_draft_template(old, "draft")
    assert new["version"] == 8
    items = {i["id"]: i for s in new["sections"] for i in s["items"]}
    assert "EST.10" not in items
    assert items["MAN.23"]["photo_required"] and items["MAN.23"]["kind"] == "status"
    assert items["MAN.24"]["kind"] == "notes"
    assert items["EST.15"]["na_notes_default"] == "Sem TPM"
    assert "(TPM)" in items["EST.15"]["label"]
    assert corrected_draft_template(new, "draft") == new
    assert corrected_draft_template(old, "closed") == old == LEGACY_TEMPLATE


def old_answers():
    return {i["id"]: {"value": "NA", "notes": "Não aplicável neste ensaio"}
            for s in LEGACY_TEMPLATE["sections"] for i in s["items"]}


def legacy_fixture(setup, closed=False):
    c, h, contract, app = setup
    rid = make_report(c, h, contract, closed=closed)
    image = BytesIO(); Image.new("RGB", (20, 30), "blue").save(image, format="PNG")
    png = image.getvalue()
    pids = [str(uuid.uuid4()), str(uuid.uuid4())]
    with app.state.factory() as db:
        report = db.get(Report, rid)
        payload = deepcopy(report.payload)
        # Keep signature name/reference created by the fixture.
        signature_answer = payload["answers"].get("FIN.02")
        payload["answers"] = old_answers()
        if signature_answer:
            payload["answers"]["FIN.02"] = signature_answer
        payload["answers"]["MAN.21"] = {"value": "", "notes": "Notas antigas da manutenção"}
        payload["answers"]["EST.10"] = {"value": "NOK", "notes": "Limpeza antiga"}
        report.template = deepcopy(LEGACY_TEMPLATE); report.payload = payload
        for pid, key in zip(pids, ("MAN.21", "EST.10")):
            db.add(Photo(id=pid, report_id=rid, checkpoint=key, filename=pid+".png",
                         mime="image/png", digest=hashlib.sha256(png).hexdigest(), data=png))
        db.commit()
    return c, h, contract, app, rid, pids, png


def test_existing_draft_upgrade_preserves_bytes_and_rejects_stale_writer(setup):
    c,h,contract,app,rid,pids,png = legacy_fixture(setup)
    path = '/api/reports/'+rid
    projected = c.get(path, headers=h).json()
    assert projected["template"]["version"] == 8
    a = projected["payload"]["answers"]
    assert a["MAN.24"]["notes"] == "Notas antigas da manutenção"
    assert a["EST.18"]["notes"].endswith("Limpeza antiga")
    assert "MAN.23" not in a and "EST.10" not in a
    assert projected["payload"]["retired_answers"]["EST.10"]["value"] == "NOK"
    assert {p['id']:p['checkpoint'] for p in projected['photos']}[pids[0]] == 'MAN.24'
    for pid in pids:
        assert c.get(path+'/photos/'+pid,headers=h).content == png
    data = {'charger_id':projected['charger_id'],'checklist_version':TEMPLATE["version"],'base_revision':projected['revision'],
            'status':'draft','payload':projected['payload']}
    saved = c.put(path,headers=h,json=data)
    assert saved.status_code == 200,saved.text
    assert c.put(path,headers=h,json=data).status_code == 409
    with app.state.factory() as db:
        assert db.get(Report,rid).template['version'] == 8
        assert [db.get(Photo,pid).checkpoint for pid in pids] == ['MAN.24','EST.18']
        assert all(db.get(Photo,pid).data == png for pid in pids)


def test_closed_legacy_correction_upgrades_and_requires_after_photograph(setup):
    c,h,contract,app,rid,pids,png = legacy_fixture(setup,closed=True)
    h={**h,'X-Lem-Client-Platform':'windows'}
    original=c.get('/api/reports/'+rid,headers=h).json()
    assert original['template']['version']==6
    draft=start((c,h,contract,app,original))
    assert draft['template']['version']==8
    assert draft['payload']['answers']['MAN.24']['notes']=='Notas antigas da manutenção'
    assert c.get('/api/reports/'+rid,headers=h).json()==original
    data=body(draft)
    data['payload']['answers'].update({'MAN.12': {'value': '120 ms'}, 'MAN.14': {'value': 'NA', 'notes': 'Em carga'}})
    data['payload']['answers']['MAN.23']={'value':'OK','notes':''}
    path='/api/reports/'+draft['id']
    result=c.post(path+'/correction/commit',headers=h,json=data)
    assert result.status_code==422 and 'MAN.23: falta fotografia' in result.text
    assert c.get('/api/reports/'+rid,headers=h).json()==original
    result=c.put(path+'/photos/'+str(uuid.uuid4()),headers=h,
                 data={'checkpoint':'MAN.23','base_revision':str(draft['revision'])},
                 files={'file':('after.png',png,'image/png')})
    assert result.status_code==200,result.text
    result=c.post(path+'/correction/commit',headers=h,json=data)
    assert result.status_code==200,result.text
    assert result.json()['template']['version']==8 and result.json()['status']=='closed'
    assert c.get('/api/reports/'+rid,headers=h).status_code==410
    assert c.post(path+'/correction/commit',headers=h,json=data).status_code==200


def test_photo_upload_uses_the_current_form_already_returned_by_get(setup):
    c, h, _, app, rid, pids, png = legacy_fixture(setup)
    path = '/api/reports/' + rid
    projected = c.get(path, headers=h).json()
    assert projected['template']['version'] == 8
    pid = str(uuid.uuid4())
    # A section can upload media before its next answers PUT. The API must accept
    # the MAN.24 checkpoint it already exposed, migrating existing evidence too.
    result = c.put(path + '/photos/' + pid, headers=h,
                   data={'checkpoint': 'MAN.24', 'base_revision': str(projected['revision'])},
                   files={'file': ('notes.png', png, 'image/png')})
    assert result.status_code == 200, result.text
    with app.state.factory() as db:
        assert db.get(Report, rid).template['version'] == 8
        assert db.get(Photo, pids[0]).checkpoint == 'MAN.24'
        assert db.get(Photo, pid).checkpoint == 'MAN.24'
        assert db.get(Photo, pids[0]).data == png


@pytest.mark.parametrize('failure', ['stale_revision', 'invalid_image'])
def test_failed_media_upload_does_not_partially_migrate_an_old_draft(setup, failure):
    c, h, _, app, rid, pids, png = legacy_fixture(setup)
    projected = c.get('/api/reports/' + rid, headers=h).json()
    result = c.put('/api/reports/' + rid + '/photos/' + str(uuid.uuid4()), headers=h,
                   data={'checkpoint': 'EST.18', 'base_revision': str(projected['revision'] -
                         (1 if failure == 'stale_revision' else 0))},
                   files={'file': ('notes.png', b'not an image' if failure == 'invalid_image' else png, 'image/png')})
    assert result.status_code == (409 if failure == 'stale_revision' else 422), result.text
    with app.state.factory() as db:
        assert db.get(Report, rid).template['version'] == 6
        assert [db.get(Photo, pid).checkpoint for pid in pids] == ['MAN.21', 'EST.10']
        assert all(db.get(Photo, pid).data == png for pid in pids)


def test_closed_offline_v6_can_complete_staged_upload_without_reinterpretation(setup):
    c,h,contract,app=setup
    upload(c,h,contract['id'],True)
    charger=c.get('/api/contracts/'+contract['id']+'/chargers',headers=h).json()[0]
    rid=str(uuid.uuid4());path='/api/reports/'+rid
    payload={'answers':old_answers(),'started':'2026-11-01T09:00:00Z','finished':'2026-11-01T10:00:00Z'}
    payload['answers']['MAN.21']={'value':'','notes':'Observação histórica'}
    data={'charger_id':charger['id'],'checklist_version':6,'base_revision':0,'status':'draft',
          'frozen_history':True,'payload':payload}
    staged=c.put(path,headers=h,json=data)
    assert staged.status_code==200,staged.text
    assert staged.json()['template']['version']==6 and staged.json()['template']['frozen_upload']
    assert c.get(path,headers=h).json()['payload']['answers']['MAN.21']['notes']=='Observação histórica'
    add_signature(c,h,rid,payload)
    data['base_revision']=1
    blocked=c.put(path,headers=h,json={**data,'frozen_history':False})
    assert blocked.status_code==409
    data['status']='closed'
    closed=c.put(path,headers=h,json=data)
    assert closed.status_code==200,closed.text
    assert closed.json()['template']['version']==6
    assert 'frozen_upload' not in closed.json()['template']
    assert closed.json()['payload']['answers']['MAN.21']['notes']=='Observação histórica'

def test_server_recalculates_forged_ok_and_requires_notes_before_closure(setup):
    c,h,contract,app=setup
    rid=make_report(c,h,contract)
    path='/api/reports/'+rid
    report=c.get(path,headers=h).json()
    payload=report['payload']
    a=payload['answers']
    a['MAN.11']={'value':'300','notes':''}
    a['MAN.13']={'value':'315','assessment':'OK','notes':''}
    a['MAN.20']={'value':'167','assessment':'OK','notes':''}
    data={'charger_id':report['charger_id'],'checklist_version':TEMPLATE["version"],'base_revision':1,'status':'draft','payload':payload}
    saved=c.put(path,headers=h,json=data)
    assert saved.status_code==200,saved.text
    assert saved.json()['payload']['answers']['MAN.13']['assessment']=='NOK'
    assert saved.json()['payload']['answers']['MAN.20']['assessment']=='NOK'
    assert saved.json()['payload']['answers']['MAN.13']['notes']==''
    image=BytesIO();Image.new('RGB',(20,30),'blue').save(image,format='PNG')
    for key in ('MAN.13','MAN.20'):
        result=c.put(path+'/photos/'+str(uuid.uuid4()),headers=h,
            data={'checkpoint':key,'base_revision':'2'},files={'file':('reading.png',image.getvalue(),'image/png')})
        assert result.status_code==200,result.text
    add_signature(c,h,rid,payload,revision=2)
    data.update(status='closed',base_revision=2)
    rejected=c.put(path,headers=h,json=data)
    assert rejected.status_code==422 and 'NOK precisa de' in rejected.text
    assert c.get(path,headers=h).json()['status']=='draft'
    a['MAN.13']['notes']='Confirmado pelo técnico'
    a['MAN.20']['notes']='Confirmado pelo técnico'
    a['MAN.11']['value']='0'
    rejected=c.put(path,headers=h,json=data)
    assert rejected.status_code==422 and 'superior a zero' in rejected.text
    a['MAN.11']['value']='300'
    closed=c.put(path,headers=h,json=data)
    assert closed.status_code==200,closed.text
    assert closed.json()['payload']['answers']['MAN.13']['assessment']=='NOK'
    assert closed.json()['payload']['answers']['MAN.20']['assessment']=='NOK'


def test_legacy_optional_null_does_not_drop_retired_notes():
    payload = {"answers": {"MAN.21": None, "EST.18": None,
        "EST.10": {"value": "NOK", "notes": "Limpeza por terminar"}}}
    migrated = upgrade_payload(payload)
    assert migrated["answers"]["EST.18"]["notes"] == "Limpeza por terminar"
    assert migrated["retired_answers"]["EST.10"] == payload["answers"]["EST.10"]
    assert migrated["answers"]["MAN.22"] is None


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
def test_ancient_closed_correction_preserves_content_and_enforces_current_form(setup, version):
    from test_workflow import answers
    c, h, contract, app = setup
    h = {**h, "X-Lem-Client-Platform": "windows"}
    rid = make_report(c, h, contract, closed=True)
    with app.state.factory() as db:
        source = db.get(Report, rid)
        schema = deepcopy(source.template)
        schema["version"] = version
        for section in schema["sections"]:
            for item in section["items"]:
                if item["id"] == "EST.02":
                    item["label"] = "Número de série antigo"
        source.template = schema
        payload = deepcopy(source.payload)
        payload["answers"]["EST.02"] = {"value": "S" * 8000, "notes": "N" * 8000}
        source.payload = payload
        db.commit()
    original = c.get("/api/reports/" + rid, headers=h).json()
    draft = start((c, h, contract, app, original))
    assert draft["template"]["version"] == 8
    previous = draft["template"]["sections"][-1]
    assert previous["id"] == "ANT"
    key = next(i["id"] for i in previous["items"]
               if i["label"] == "EST.02 — Número de série antigo — Resposta anterior")
    assert "EST.02" not in draft["payload"]["answers"]
    assert draft["payload"]["answers"][key]["notes"] == "S" * 8000
    assert draft["payload"]["answers"][key + ".OBS"]["notes"] == "N" * 8000
    assert "signatures" not in draft["payload"]
    assert draft["photos"][0]["checkpoint"].startswith("ANT.")
    assert draft["payload"]["legacy_signatures"]["FIN.02"]["photo_id"] == draft["photos"][0]["id"]
    data = {"charger_id": draft["charger_id"], "checklist_version": TEMPLATE["version"],
            "base_revision": draft["revision"], "status": "closed",
            "payload": deepcopy(draft["payload"])}
    data["payload"].update(started="2026-11-01T09:00:00Z", finished="2026-11-01T10:00:00Z")
    path = "/api/reports/" + draft["id"]
    # Neither archived answers nor the old signature satisfy the current form.
    rejected = c.post(path + "/correction/commit", headers=h, json=data)
    assert rejected.status_code == 422
    assert c.get("/api/reports/" + rid, headers=h).json() == original
    data["payload"]["answers"].update(answers())
    add_signature(c, h, draft["id"], data["payload"], revision=draft["revision"])
    broken = deepcopy(data)
    broken["payload"]["legacy_signatures"]["FIN.02"]["photo_id"] = original["photos"][0]["id"]
    rejected = c.post(path + "/correction/commit", headers=h, json=broken)
    assert rejected.status_code == 422 and "histórica" in rejected.json()["detail"]
    assert c.get("/api/reports/" + rid, headers=h).json() == original
    saved = c.post(path + "/correction/commit", headers=h, json=data)
    assert saved.status_code == 200, saved.text
    assert saved.json()["template"]["version"] == 8
    assert saved.json()["payload"]["answers"][key]["notes"] == "S" * 8000
    assert c.get("/api/reports/" + rid, headers=h).status_code == 410
    # The cloned historical signature bytes survive successful replacement.
    old_photo, new_photo = original["photos"][0], draft["photos"][0]
    assert old_photo["digest"] == new_photo["digest"]
    assert c.get(path + "/photos/" + new_photo["id"], headers=h).status_code == 200


def test_already_open_ancient_correction_get_matches_transactional_write(setup):
    c, h, contract, app = setup
    h = {**h, "X-Lem-Client-Platform": "windows"}
    rid = make_report(c, h, contract, closed=True)
    original = c.get("/api/reports/" + rid, headers=h).json()
    draft = start((c, h, contract, app, original))
    with app.state.factory() as db:
        stored = db.get(Report, draft["id"])
        stored.template = {**stored.template, "version": 3}
        db.commit()
    path = "/api/reports/" + draft["id"]
    projected = c.get(path, headers=h).json()
    assert projected["template"]["version"] == 8
    saved = c.put(path, headers=h, json={"charger_id": draft["charger_id"],
        "checklist_version": TEMPLATE["version"], "base_revision": draft["revision"],
        "status": "draft", "payload": projected["payload"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["payload"] == projected["payload"]
    assert saved.json()["photos"] == projected["photos"]
    assert saved.json()["template"] == projected["template"]
    assert c.get("/api/reports/" + rid, headers=h).json() == original
