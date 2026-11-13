# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

from app.main import TEMPLATE
"""Physical station identity is shared; contract reports and permissions are not."""
import copy, json, sqlite3, uuid
from io import BytesIO
import pytest
from PIL import Image
from sqlalchemy import select, func
from test_workflow import setup, xlsx, upload, answers
from app.models import Station, Charger, Report, database, Contract


def shared(setup):
    c,h,contract,app=setup
    data=xlsx(('SHARED-001',))
    assert upload(c,h,contract['id'],True,data).status_code==200
    other=c.post('/api/contracts',headers=h,json={'company':'Galp','year':2026,'semester':2,'kind':'TRE'}).json()
    assert upload(c,h,other['id'],True,data).status_code==200
    a=c.get(f"/api/contracts/{contract['id']}/chargers",headers=h).json()[0]
    b=c.get(f"/api/contracts/{other['id']}/chargers",headers=h).json()[0]
    return c,h,contract,other,a,b,app


def draft(c,h,charger):
    rid=str(uuid.uuid4())
    body={'checklist_version': TEMPLATE["version"], 'charger_id':charger['id'],'base_revision':0,'status':'draft','payload':{'answers':answers()}}
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==200,response.text
    body['base_revision']=response.json()['revision']
    return rid,body,response.json()


def png():
    b=BytesIO();Image.new('RGB',(40,30),'cyan').save(b,format='PNG');return b.getvalue()


def close(c,h,rid,body,**values):
    body=copy.deepcopy(body)
    for key,value in values.items():body['payload']['answers'][key]={'value':value,'notes':''}
    if body['payload']['answers']['EST.04']['value']!='NA':
        r=c.put(f'/api/reports/{rid}/photos/{uuid.uuid4()}',headers=h,
            data={'checkpoint':'EST.04','base_revision':str(body['base_revision'])},files={'file':('serial.png',png(),'image/png')})
        assert r.status_code==200,r.text
    body['status']='closed'
    return c.put('/api/reports/'+rid,headers=h,json=body)


def test_one_physical_station_and_two_contract_memberships(setup):
    c,h,con,other,a,b,app=shared(setup)
    assert a['id']!=b['id'] and a['station_id']==b['station_id']
    with app.state.factory() as db:
        assert db.scalar(select(func.count()).select_from(Station))==1
        assert db.scalar(select(func.count()).select_from(Charger))==2
    rid,body,_=draft(c,h,a)
    assert c.get(f"/api/contracts/{other['id']}/chargers",headers=h).json()[0]['reports']==[]
    # Correcting one report updates both catalogues while a prior report stays frozen.
    historic,hbody,hreport=draft(c,h,b)
    old=close(c,h,historic,hbody)
    assert old.status_code==200,old.text
    r=close(c,h,rid,body,**{'EST.02':'Novo fabricante','EST.03':'Modelo completo X','EST.04':'SN-NEW-001'})
    assert r.status_code==200,r.text
    assert r.json()['station_update']['station_id']==a['station_id']
    for contract in (con,other):
        row=c.get(f"/api/contracts/{contract['id']}/chargers",headers=h).json()[0]
        assert (row['details']['manufacturer'],row['details']['model'],row['details']['serial'])==('Novo fabricante','Modelo completo X','SN-NEW-001')
    past=c.get('/api/reports/'+historic,headers=h).json()
    assert past['snapshot']==old.json()['snapshot']
    # A later stale workbook cannot undo fields verified during closure.
    assert upload(c,h,other['id'],True,xlsx(('SHARED-001',))).status_code==200
    row=c.get(f"/api/contracts/{other['id']}/chargers",headers=h).json()[0]
    assert row['details']['serial']=='SN-NEW-001' and row['details']['model']=='Modelo completo X'


def test_draft_and_failed_closure_cannot_update_identity(setup):
    c,h,con,other,a,b,app=shared(setup)
    rid,body,_=draft(c,h,a)
    body['payload']['answers']['EST.02']={'value':'Changed in draft','notes':''}
    r=c.put('/api/reports/'+rid,headers=h,json=body)
    assert r.status_code==200
    body['base_revision']=r.json()['revision']
    assert c.get(f"/api/contracts/{other['id']}/chargers",headers=h).json()[0]['details'].get('manufacturer') is None
    body['status']='closed';body['payload']['answers']['EST.05']={'value':'OK','notes':''}
    assert c.put('/api/reports/'+rid,headers=h,json=body).status_code==422
    assert c.get(f"/api/contracts/{other['id']}/chargers",headers=h).json()[0]['details'].get('manufacturer') is None


def test_stale_report_does_not_overwrite_a_newer_correction(setup):
    c,h,con,other,a,b,app=shared(setup)
    first,fbody,_=draft(c,h,a);second,sbody,sreport=draft(c,h,b)
    assert close(c,h,first,fbody,**{'EST.03':'Corrected model'}).status_code==200
    conflict=close(c,h,second,sbody,**{'EST.03':'Different correction'})
    assert conflict.status_code==409,conflict.text
    assert c.get('/api/reports/'+second,headers=h).json()['status']=='draft'
    row=c.get(f"/api/contracts/{other['id']}/chargers",headers=h).json()[0]
    assert row['details']['model']=='Corrected model'
    # An unchanged original model in another report does not revert the shared value.
    third,tbody,_=draft(c,h,b)
    # Simulate an older snapshot without overwriting server-owned snapshots from the client.
    with app.state.factory() as db:
        r=db.get(Report,third);snap=copy.deepcopy(r.snapshot);snap['charger']['model']='DC60';r.snapshot=snap;db.commit()
    assert close(c,h,third,tbody,**{'EST.03':'DC60'}).status_code==200
    assert c.get(f"/api/contracts/{con['id']}/chargers",headers=h).json()[0]['details']['model']=='Corrected model'


def test_shared_identity_does_not_grant_access_to_other_contract_reports(setup):
    c,h,con,other,a,b,app=shared(setup)
    rid,body,_=draft(c,h,b)
    user=c.post('/api/users',headers=h,json={'email':'one-contract@test.pt','name':'Tech','password':'StrongPassword123!','role':'tech'}).json()['id']
    assert c.put(f"/api/contracts/{con['id']}/users/{user}",headers=h).status_code==200
    token=c.post('/api/login',json={'email':'one-contract@test.pt','password':'StrongPassword123!'}).json()['token']
    tech={'Authorization':'Bearer '+token}
    assert c.get(f"/api/contracts/{con['id']}/chargers",headers=tech).status_code==200
    assert c.get('/api/reports/'+rid,headers=tech).status_code==403


def test_legacy_rows_migrate_without_changing_memberships_or_losing_originals(tmp_path):
    path=tmp_path/'old.db'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE contracts (id VARCHAR(36) PRIMARY KEY, company VARCHAR(120), year INTEGER, semester INTEGER, kind VARCHAR(40), name VARCHAR(240) UNIQUE);
        CREATE TABLE chargers (id VARCHAR(36) PRIMARY KEY, contract_id VARCHAR(36) REFERENCES contracts(id), external_id VARCHAR(200), details JSON, UNIQUE(contract_id,external_id));''')
        db.execute("INSERT INTO contracts VALUES ('old','Galp',2025,2,'TRE','old')")
        db.execute("INSERT INTO contracts VALUES ('new','Galp',2026,1,'TRE','new')")
        db.execute('INSERT INTO chargers VALUES (?,?,?,?)',('link-old','old','ID-001',json.dumps({'model':'Old model','serial':'00001'})))
        db.execute('INSERT INTO chargers VALUES (?,?,?,?)',('link-new','new','ID-001',json.dumps({'model':'New model','serial':'00002'})))
    engine,factory=database('sqlite:///'+str(path))
    with factory() as db:
        old=db.get(Charger,'link-old');new=db.get(Charger,'link-new')
        assert old.station_id==new.station_id
        assert old.details==new.details=={'model':'New model','serial':'00002'}
        assert old.legacy_details=={'model':'Old model','serial':'00001'}
        assert new.legacy_details=={'model':'New model','serial':'00002'}
        sid=old.station_id
    engine.dispose()
    engine,factory=database('sqlite:///'+str(path))
    with factory() as db:
        assert db.get(Charger,'link-old').station_id==sid
        assert db.scalar(select(func.count()).select_from(Station))==1


def test_old_unsent_checklist_cannot_reinterpret_serial_as_manufacturer(setup):
    c,h,con,other,a,b,app=shared(setup)
    rid=str(uuid.uuid4())
    old={'checklist_version':3,'charger_id':a['id'],'base_revision':0,'status':'draft',
         'payload':{'answers':{'EST.02':{'value':'SERIAL-FROM-OLD-APP','notes':''}}}}
    response=c.put('/api/reports/'+rid,headers=h,json=old)
    assert response.status_code==409,response.text
    with app.state.factory() as db:assert db.get(Report,rid) is None
    assert c.get(f"/api/contracts/{con['id']}/chargers",headers=h).json()[0]['details'].get('manufacturer') is None


def test_old_server_report_keeps_its_frozen_schema_on_upgrade(setup):
    c,h,con,other,a,b,app=shared(setup)
    rid,body,old=draft(c,h,a)
    frozen={'id':'pcve-maintenance','version':3,'sections':[{'id':'EST','title':'Anterior',
        'items':[{'id':'EST.02','label':'Número de série antigo','kind':'text','photo_required':False}]}]}
    with app.state.factory() as db:
        r=db.get(Report,rid);r.template=frozen;r.payload={'answers':{}};db.commit()
    body['checklist_version']=3;body['status']='closed';body['payload']={'answers':{'EST.02':{'value':'SERIAL-OLD','notes':''}}}
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==200,response.text
    assert response.json()['template']==frozen
    assert c.get(f"/api/contracts/{con['id']}/chargers",headers=h).json()[0]['details'].get('manufacturer') is None


def test_purging_one_contract_preserves_station_and_other_membership(setup):
    from app.models import ContractTrash
    from datetime import datetime
    c,h,con,other,a,b,app=shared(setup)
    r=c.post(f"/api/contracts/{con['id']}/trash",headers=h,json={'confirmation':'APAGAR'})
    assert r.status_code==200,r.text
    with app.state.factory() as db:
        expiry=datetime.fromisoformat(db.get(ContractTrash,con['id']).purge_after)
    assert app.state.purge_expired_contracts(expiry)==1
    with app.state.factory() as db:
        assert db.get(Charger,a['id']) is None
        assert db.get(Station,a['station_id']) is not None
        assert db.get(Charger,b['id']).station_id==a['station_id']
        assert db.get(Charger,b['id']).details['model']=='DC60'
