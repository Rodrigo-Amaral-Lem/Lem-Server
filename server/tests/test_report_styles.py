# SPDX-License-Identifier: GPL-3.0-only
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pypdf import PdfReader
from test_workflow import setup, xlsx, MAPPING, answers, add_signature
from app.main import TEMPLATE


def test_saved_format_survives_new_session_and_server_restart(setup):
    from fastapi.testclient import TestClient
    from app.main import create_app
    c, h, contract, app = setup
    cid = contract['id']
    response = put(c, h, cid, 'CSMTEC', ['APS', 'Galp'])
    assert response.status_code == 200, response.text
    saved = response.json()
    engine = app.state.factory.kw['bind']
    database_url = engine.url.render_as_string(hide_password=False)
    engine.dispose()
    restarted = create_app(database_url)
    with TestClient(restarted, headers={'X-Lem-Client-Version': '0.2.6'}) as fresh:
        login = fresh.post('/api/login', json={'email': 'admin@lem.pt', 'password': 'StrongPassword123!'})
        assert login.status_code == 200, login.text
        auth = {'Authorization': 'Bearer ' + login.json()['token']}
        assert style(fresh, auth, cid) == saved
        readback = fresh.get(f'/api/contracts/{cid}/presentation', headers=auth)
        assert readback.json() == saved
        assert readback.headers['cache-control'] == 'no-store'


def test_admin_exports_legacy_technician_report_with_current_contract_brand(setup):
    from copy import deepcopy
    from zipfile import ZipFile
    from app.models import Report
    from test_workflow import upload
    c, admin, contract, app = setup
    cid = contract['id']
    tech = c.post('/api/users', headers=admin, json={'email': 'legacy-tech@example.test',
        'name': 'Técnico histórico', 'role': 'tech', 'password': 'StrongPassword123!'}).json()
    assert c.put(f'/api/contracts/{cid}/users/{tech["id"]}', headers=admin).status_code == 200
    token = c.post('/api/login', json={'email': 'legacy-tech@example.test', 'password': 'StrongPassword123!'}).json()['token']
    technician = {'Authorization': 'Bearer ' + token}
    assert upload(c, admin, cid, True).status_code == 200
    charger = c.get(f'/api/contracts/{cid}/chargers', headers=technician).json()[0]['id']
    rid = str(uuid.uuid4())
    body = {'charger_id': charger, 'base_revision': 0, 'checklist_version': TEMPLATE['version'],
            'status': 'draft', 'payload': {'answers': answers()}}
    assert c.put(f'/api/reports/{rid}', headers=technician, json=body).status_code == 200
    add_signature(c, technician, rid, body['payload'])
    body.update(base_revision=1, status='closed')
    assert c.put(f'/api/reports/{rid}', headers=technician, json=body).status_code == 200
    # Preserve the database shape used before contract presentation existed.
    with app.state.factory() as db:
        stored = db.get(Report, rid)
        snapshot = deepcopy(stored.snapshot)
        for key in ('id', 'presentation', 'report_logos'):
            snapshot['contract'].pop(key, None)
        stored.snapshot = snapshot
        db.commit()
    before = c.get(f'/api/reports/{rid}', headers=admin).json()
    assert before['owner_id'] == tech['id'] and before['presentation']['issuer'] == 'Lem'
    for revision, issuer in enumerate(('CSMTEC', 'Lem', 'CSMTEC')):
        assert put(c, admin, cid, issuer, ['APS', 'Galp'], revision).status_code == 200
        direct = c.get(f'/api/reports/{rid}/pdf', headers=admin)
        bundle = c.get(f'/api/reports/{rid}/bundle', headers=admin)
        assert direct.status_code == bundle.status_code == 200
        with ZipFile(BytesIO(bundle.content)) as archive:
            saved = json.loads(archive.read(next(name for name in archive.namelist() if name.endswith(".json"))))
            assert saved['presentation']['issuer'] == issuer
            bundled_pdf = archive.read(saved['identifier'] + '.pdf')
            for data in (direct.content, bundled_pdf):
                page = PdfReader(BytesIO(data)).pages[0]
                assert issuer in page.extract_text()
                assert len(page.images) == 3
                if issuer == 'Lem':
                    assert 'CSMTEC' not in page.extract_text()
            for key in ('id', 'owner_id', 'status', 'revision', 'snapshot', 'payload', 'photos', 'identifier'):
                assert saved[key] == before[key], key


def style(client, headers, cid):
    return next(c for c in client.get('/api/contracts', headers=headers).json() if c['id'] == cid)['presentation']


def put(client, headers, cid, issuer='CSMTEC', logos=None, revision=0):
    return client.put(f'/api/contracts/{cid}/presentation', headers=headers,
        json={'issuer': issuer, 'client_logos': logos or [], 'base_revision': revision})


def test_import_and_existing_closed_report_use_live_presentation_without_changing_content(setup):
    c, h, contract, app = setup
    cid = contract['id']
    assert style(c, h, cid) == {'issuer': 'Lem', 'revision': 0, 'client_logos': []}
    mapping = {**MAPPING, 'report_issuer': 'CSMTEC', 'report_logos': ['APS', 'Galp'], 'branding_revision': 0}
    def send(commit):
        return c.post(f'/api/contracts/{cid}/import', headers=h, files={'file': ('test.xlsx', xlsx())},
            data={'mapping': json.dumps(mapping), 'commit': str(commit).lower()})
    assert send(False).status_code == 200
    assert style(c, h, cid)['issuer'] == 'Lem'
    assert send(True).status_code == 200
    assert style(c, h, cid)['issuer'] == 'CSMTEC'
    charger = c.get(f'/api/contracts/{cid}/chargers', headers=h).json()[0]['id']
    rid = str(uuid.uuid4())
    body = {'charger_id': charger, 'base_revision': 0, 'checklist_version': TEMPLATE['version'],
            'payload': {'answers': answers()}, 'status': 'draft'}
    assert c.put(f'/api/reports/{rid}', headers=h, json=body).status_code == 200
    add_signature(c, h, rid, body['payload'])
    body.update(base_revision=1, status='closed')
    closed = c.put(f'/api/reports/{rid}', headers=h, json=body)
    assert closed.status_code == 200, closed.text
    before = closed.json()
    assert before['presentation']['issuer'] == 'CSMTEC'
    assert put(c, h, cid, 'Lem', ['Transdev'], revision=1).status_code == 200
    after = c.get(f'/api/reports/{rid}', headers=h).json()
    assert after['presentation']['issuer'] == 'Lem'
    for key in ('revision', 'status', 'owner_id', 'snapshot', 'payload', 'identifier'):
        assert after[key] == before[key], key
    pdf = c.get(f'/api/reports/{rid}/pdf', headers=h)
    assert pdf.status_code == 200
    reader = PdfReader(BytesIO(pdf.content))
    assert len(reader.pages[0].images) == 2
    assert 'CSMTEC' not in reader.pages[0].extract_text()
    # An old import preview must not overwrite the newer administrator's choices.
    assert send(True).status_code == 409
    assert style(c, h, cid)['revision'] == 2


def test_only_administrators_and_compare_revision_protect_settings(setup):
    c, h, contract, app = setup
    cid = contract['id']
    user = c.post('/api/users', headers=h, json={'email':'tech@example.test', 'name':'Tech', 'role':'tech', 'password':'StrongPassword123!'}).json()
    token = c.post('/api/login', json={'email':'tech@example.test', 'password':'StrongPassword123!'}).json()['token']
    tech = {'Authorization': 'Bearer ' + token}
    assert put(c, tech, cid).status_code == 403
    assert put(c, h, cid, 'Unknown').status_code == 422
    for logos in (['Lem'], ['CSMTEC'], ['APS', 'APS'], ['missing'], ['APS', 'Galp', 'Transdev']):
        assert put(c, h, cid, logos=logos).status_code == 422
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda issuer: put(c, h, cid, issuer, ['APS']), ['CSMTEC', 'Lem']))
    assert sorted(r.status_code for r in results) == [200, 409]
    final = style(c, h, cid)
    assert final['revision'] == 1
    assert put(c, h, cid, final['issuer'], ['APS'], 1).json()['revision'] == 1
    assert put(c, h, cid, 'Lem', [], 0).status_code == 409
    # Existing-row compare-and-swap must also reject one writer and preserve a
    # matching issuer/logo pair, even though SQLite does not implement row locks.
    for revision in range(1, 5):
        choices = [('CSMTEC', ['Galp']), ('Lem', ['Transdev'])] if revision % 2 else [('CSMTEC', ['APS']), ('Lem', ['Galp','APS'])]
        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(lambda choice: put(c, h, cid, choice[0], choice[1], revision), choices))
        assert sorted(r.status_code for r in responses) == [200,409]
        saved = style(c, h, cid)
        winner = next(choice for choice, response in zip(choices, responses) if response.status_code == 200)
        assert (saved['issuer'], [logo['id'] for logo in saved['client_logos']]) == winner
        assert saved['revision'] == revision + 1


def test_lem_to_csmtec_changes_existing_and_new_exports(setup):
    from test_export_resources import make_report
    from zipfile import ZipFile
    c,h,contract,app=setup
    rid=make_report(c,h,contract,closed=True)
    initial=c.get(f'/api/reports/{rid}',headers=h).json()
    assert initial['presentation']['issuer']=='Lem'
    assert put(c,h,contract['id'],'CSMTEC',['APS','Galp']).status_code==200
    assert c.get(f'/api/contracts/{contract["id"]}/presentation',headers=h).json()['issuer']=='CSMTEC'
    new=make_report(c,h,contract,closed=True)
    for report_id in (rid,new):
        response=c.get(f'/api/reports/{report_id}/bundle',headers=h)
        assert response.status_code==200,response.text[:100]
        with ZipFile(BytesIO(response.content)) as archive:
            report=json.loads(archive.read(next(name for name in archive.namelist() if name.endswith(".json"))))
            assert report['presentation']['issuer']=='CSMTEC'
            reader=PdfReader(BytesIO(archive.read(report['identifier']+'.pdf')))
            assert 'CSMTEC' in reader.pages[0].extract_text()
            assert len(reader.pages[0].images)==3
    after=c.get(f'/api/reports/{rid}',headers=h).json()
    for key in ('owner_id','payload','revision','status','snapshot','identifier'):
        assert after[key]==initial[key]
