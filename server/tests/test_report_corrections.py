# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import uuid
import pytest
from sqlalchemy import select, func, event
from test_workflow import setup, add_signature
from test_export_resources import make_report
from app.models import Report, Photo, ReportCorrection, DeletedReport


@pytest.fixture
def correction_case(setup):
    c, headers, contract, app = setup
    h = {**headers, 'X-Lem-Client-Platform': 'windows'}
    rid = make_report(c, h, contract, closed=True)
    original = c.get(f'/api/reports/{rid}', headers=h).json()
    return c, h, contract, app, original


def start(case, headers=None, new_id=None):
    c, h, contract, app, original = case
    result = c.post(f'/api/reports/{original["id"]}/corrections', headers=headers or h,
                    json={'id': new_id or str(uuid.uuid4()), 'base_revision': original['revision']})
    assert result.status_code == 200, result.text
    return result.json()


def body(report):
    payload = deepcopy(report['payload'])
    payload.update(started='2026-10-01T09:00:00+01:00', finished='2026-10-01T11:30:12+01:00')
    payload['answers']['FIN.03']['notes'] = 'Correção validada'
    return {'charger_id': report['charger_id'], 'checklist_version': report['template']['version'],
            'base_revision': report['revision'], 'status': 'closed', 'payload': payload}


@pytest.mark.parametrize('platform',['windows','linux'])
def test_atomic_replacement_preserves_evidence_owner_and_retry(correction_case,platform):
    c,h,contract,app,original = correction_case
    h['X-Lem-Client-Platform']=platform
    draft = start(correction_case)
    assert start(correction_case, new_id=draft['id'])['id'] == draft['id']
    assert c.get(f'/api/reports/{original["id"]}',headers=h).json() == original
    assert draft['photos'][0]['id'] != original['photos'][0]['id']
    assert draft['payload']['signatures']['FIN.02']['photo_id'] == draft['photos'][0]['id']
    first = c.get(f'/api/reports/{original["id"]}/photos/{original["photos"][0]["id"]}',headers=h).content
    url = f'/api/reports/{draft["id"]}/correction/commit'
    data = body(draft)
    result = c.post(url,headers=h,json=data)
    assert result.status_code == 200, result.text
    saved = result.json()
    assert saved['owner_id'] == original['owner_id'] and saved['status']=='closed'
    assert saved['id'] != original['id'] and saved['identifier'] == original['identifier']
    assert saved['payload']['finished'] == data['payload']['finished']
    assert c.post(url,headers=h,json=data).json() == saved  # Lost response is safe.
    altered = deepcopy(data); altered['payload']['answers']['FIN.03']['notes']='different'
    assert c.post(url,headers=h,json=altered).status_code == 409
    gone = c.get(f'/api/reports/{original["id"]}',headers=h)
    assert gone.status_code == 410 and gone.json()['detail']['replacement_id']==draft['id']
    new_bytes = c.get(f'/api/reports/{draft["id"]}/photos/{draft["photos"][0]["id"]}',headers=h).content
    assert new_bytes == first
    with app.state.factory() as db:
        assert db.get(Report, original['id']) is None
        assert db.scalar(select(func.count()).select_from(Photo).where(Photo.report_id==original['id']))==0
        assert db.get(DeletedReport, original['id'])
    rows = c.get(f'/api/contracts/{contract["id"]}/chargers',headers=h).json()
    row = next(r for r in rows if r['id']==draft['charger_id'])
    assert [r['id'] for r in row['reports']]==[draft['id']]
    assert row['report_replacements']=={original['id']:draft['id']}


def test_changing_only_start_keeps_public_filename_and_replaces_uuid(correction_case):
    from datetime import datetime,timedelta
    c,h,contract,app,original=correction_case
    draft=start(correction_case); data=body(draft)
    data['payload']['finished']=original['finished']
    data['payload']['started']=(datetime.fromisoformat(original['finished'])-timedelta(hours=1)).isoformat()
    result=c.post(f'/api/reports/{draft["id"]}/correction/commit',headers=h,json=data)
    assert result.status_code==200,result.text
    assert result.json()['id']!=original['id']
    assert result.json()['identifier']==original['identifier']


@pytest.mark.parametrize('platform', ['', 'android', 'macos', 'ios', 'web'])
def test_desktop_only_gate_for_all_staging_operations(correction_case, platform):
    c,h,contract,app,original = correction_case
    draft = start(correction_case)
    blocked = {**h, 'X-Lem-Client-Platform': platform}
    base = f'/api/reports/{draft["id"]}'
    assert c.post(f'/api/reports/{original["id"]}/corrections',headers=blocked,
        json={'id':str(uuid.uuid4()),'base_revision':original['revision']}).status_code==403
    for method,path,data in [('GET',base,None),('PUT',base,body(draft)),
            ('POST',base+'/correction/commit',body(draft)),('DELETE',base+'?base_revision=1',None),
            ('GET',base+'/photos/'+draft['photos'][0]['id'],None)]:
        assert c.request(method,path,headers=blocked,json=data).status_code==403
    rows=c.get(f'/api/contracts/{contract["id"]}/chargers',headers=blocked).json()
    assert draft['id'] not in [r['id'] for row in rows for r in row['reports']]


@pytest.mark.parametrize('bad', ['missing_answer','missing_signature','invalid_dates','timezone_missing','missing_photo','oversized'])
def test_failed_validation_never_loses_original(correction_case,bad):
    c,h,contract,app,original=correction_case
    draft=start(correction_case); data=body(draft)
    if bad=='missing_answer': data['payload']['answers'].pop('EST.03')
    if bad=='missing_signature': data['payload'].pop('signatures')
    if bad=='invalid_dates': data['payload']['started']='2027-01-01T00:00:00Z'
    if bad=='timezone_missing': data['payload']['finished']='2026-10-01T11:30:12'
    if bad=='oversized': data['payload']['extra']='x'*300001
    if bad=='missing_photo':
        pid=draft['photos'][0]['id']
        assert c.delete(f'/api/reports/{draft["id"]}/photos/{pid}?base_revision=1',headers=h).status_code==200
        data['base_revision']=2
    result=c.post(f'/api/reports/{draft["id"]}/correction/commit',headers=h,json=data)
    assert result.status_code in (422,413),result.text
    assert c.get(f'/api/reports/{original["id"]}',headers=h).json()==original
    assert c.get(f'/api/reports/{draft["id"]}',headers=h).json()['status']=='draft'


def test_two_concurrent_corrections_only_one_replaces_and_loser_survives(correction_case):
    c,h,contract,app,original=correction_case
    drafts=[start(correction_case),start(correction_case)]
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda r:c.post(f'/api/reports/{r["id"]}/correction/commit',headers=h,json=body(r)),drafts))
    assert sorted(r.status_code for r in results)==[200,409], [r.text for r in results]
    loser=drafts[next(i for i,r in enumerate(results) if r.status_code==409)]
    preserved=c.get(f'/api/reports/{loser["id"]}',headers=h).json()
    assert preserved['status']=='draft' and preserved['photos']==loser['photos']
    with app.state.factory() as db:
        assert len(list(db.scalars(select(Report))))==2
        assert db.scalar(select(func.count()).select_from(ReportCorrection).where(ReportCorrection.state=='committed'))==1


def test_cancel_retention_stale_revision_and_ordinary_close(correction_case):
    c,h,contract,app,original=correction_case
    draft=start(correction_case); rid=draft['id']
    assert c.put(f'/api/reports/{rid}',headers=h,json=body(draft)).status_code==409
    stale=body(draft); stale['base_revision']=99
    assert c.post(f'/api/reports/{rid}/correction/commit',headers=h,json=stale).status_code==409
    assert c.delete(f'/api/reports/{original["id"]}?base_revision={original["revision"]}',headers=h).status_code==409
    assert c.post(f'/api/contracts/{contract["id"]}/trash',headers=h,json={'confirmation':'APAGAR'}).status_code==200
    assert c.post(f'/api/reports/{rid}/correction/commit',headers=h,json=body(draft)).status_code==423
    assert c.post(f'/api/contracts/{contract["id"]}/restore',headers=h).status_code==200
    assert c.delete(f'/api/reports/{rid}?base_revision=1',headers=h).status_code==200
    assert c.delete(f'/api/reports/{rid}?base_revision=1',headers=h).status_code==200
    assert c.get(f'/api/reports/{original["id"]}',headers=h).json()==original


def test_other_administrator_and_technician_cannot_edit_working_copy(correction_case):
    c,h,contract,app,original=correction_case
    draft=start(correction_case)
    for role in ('admin','tech'):
        email=role+'-correction@example.test'
        user=c.post('/api/users',headers=h,json={'email':email,'name':role,'role':role,'password':'OtherPassword123!'}).json()
        c.put(f'/api/contracts/{contract["id"]}/users/{user["id"]}',headers=h)
        token=c.post('/api/login',json={'email':email,'password':'OtherPassword123!'}).json()['token']
        other={'Authorization':'Bearer '+token,'X-Lem-Client-Platform':'linux'}
        assert c.get(f'/api/reports/{draft["id"]}',headers=other).status_code==403
        assert c.post(f'/api/reports/{draft["id"]}/correction/commit',headers=other,json=body(draft)).status_code==403
        if role=='tech':
            assert c.post(f'/api/reports/{original["id"]}/corrections',headers=other,
                json={'id':str(uuid.uuid4()),'base_revision':original['revision']}).status_code==403


def test_failure_after_deleting_original_rolls_back_everything(correction_case):
    c,h,contract,app,original=correction_case
    draft=start(correction_case)
    engine=app.state.factory.kw['bind']
    def fail(connection,cursor,statement,params,context,many):
        if statement.lstrip().lower().startswith('insert into deleted_reports'):
            raise OSError('simulated storage failure after original deletion')
    event.listen(engine,'before_cursor_execute',fail)
    try:
        with pytest.raises(OSError):
            c.post(f'/api/reports/{draft["id"]}/correction/commit',headers=h,json=body(draft))
    finally:
        event.remove(engine,'before_cursor_execute',fail)
    assert c.get(f'/api/reports/{original["id"]}',headers=h).json()==original
    assert c.get(f'/api/reports/{draft["id"]}',headers=h).json()==draft
    assert c.post(f'/api/reports/{draft["id"]}/correction/commit',headers=h,json=body(draft)).status_code==200


@pytest.mark.parametrize('iteration',range(6))
def test_cancel_racing_commit_never_removes_both_reports(correction_case,iteration):
    c,h,contract,app,original=correction_case
    draft=start(correction_case); rid=draft['id']
    with ThreadPoolExecutor(2) as pool:
        commit=pool.submit(c.post,f'/api/reports/{rid}/correction/commit',headers=h,json=body(draft))
        cancel=pool.submit(c.delete,f'/api/reports/{rid}?base_revision=1&correction_only=true',headers=h)
        results=[commit.result(),cancel.result()]
    assert sorted(r.status_code for r in results) in ([200,409],[200,410]),[r.text for r in results]
    with app.state.factory() as db:
        reports=list(db.scalars(select(Report)))
        assert len(reports)==1
        assert reports[0].status=='closed'
        assert db.scalar(select(func.count()).select_from(Photo).where(Photo.report_id==reports[0].id))==len(original['photos'])


def test_begin_racing_original_deletion_has_recoverable_outcome(correction_case):
    c,h,contract,app,original=correction_case
    new_id=str(uuid.uuid4())
    with ThreadPoolExecutor(2) as pool:
        opening=pool.submit(c.post,f'/api/reports/{original["id"]}/corrections',headers=h,
            json={'id':new_id,'base_revision':original['revision']})
        deleting=pool.submit(c.delete,f'/api/reports/{original["id"]}?base_revision={original["revision"]}',headers=h)
        results=[opening.result(),deleting.result()]
    assert sorted(r.status_code for r in results) in ([200,409],[200,410]),[r.text for r in results]
    with app.state.factory() as db:
        if db.get(Report,new_id):
            assert db.get(Report,original['id']) is not None
        else:
            assert db.get(DeletedReport,original['id']) is not None


def test_contract_purge_cleans_working_copy_and_replacement_history(correction_case):
    from datetime import timedelta
    from app.contract_trash import utc_now
    c,h,contract,app,original=correction_case
    draft=start(correction_case)
    assert c.post(f'/api/contracts/{contract["id"]}/trash',headers=h,json={'confirmation':'APAGAR'}).status_code==200
    assert app.state.purge_expired_contracts(utc_now()+timedelta(days=31))==1
    with app.state.factory() as db:
        for model in (Report,Photo,ReportCorrection,DeletedReport):
            assert db.scalar(select(func.count()).select_from(model))==0


def test_console_cancels_orphaned_copy_without_deleting_original(correction_case,monkeypatch):
    from test_admin_cli import command
    c,h,contract,app,original=correction_case
    draft=start(correction_case)
    monkeypatch.setattr('builtins.input',lambda _: 'APAGAR')
    with pytest.raises(ValueError,match='correção'):
        command(app,['reports','owner',draft['id'],original['owner_id']])
    with pytest.raises(ValueError,match='correção'):
        command(app,['reports','delete',original['id']])
    assert command(app,['reports','delete',draft['id']])['deleted']
    assert c.get(f'/api/reports/{original["id"]}',headers=h).json()==original
    with app.state.factory() as db:
        assert db.get(ReportCorrection,draft['id']).state=='cancelled'
