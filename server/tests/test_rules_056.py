# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Manual assessments, connector photos, and explicit N/A reasons."""
import copy, uuid
from io import BytesIO
from pathlib import Path
import pytest
from PIL import Image
from app.main import TEMPLATE
from app.checklist import for_charger
from app.measurements import format_value
from test_workflow import setup, upload, answers


def report(setup):
    c,h,contract,_=setup
    assert upload(c,h,contract['id'],True).status_code==200
    charger=c.get('/api/contracts/'+contract['id']+'/chargers',headers=h).json()[0]
    rid=str(uuid.uuid4())
    body={'checklist_version':TEMPLATE['version'],'charger_id':charger['id'],'base_revision':0,'payload':{'answers':answers()}}
    r=c.put('/api/reports/'+rid,headers=h,json=body)
    assert r.status_code==200,r.text
    photo(c,h,rid,'MAN.12')
    body['base_revision']=r.json()['revision'];body['status']='closed'
    return c,h,rid,body


def photo(c,h,rid,key,video=False):
    if video:data=(Path(__file__).with_name('fixtures')/'evidence-clip.mp4').read_bytes();name='test.mp4';mime='video/mp4'
    else:
        b=BytesIO();Image.new('RGB',(40,30),'cyan').save(b,format='PNG');data=b.getvalue();name='test.png';mime='image/png'
    r=c.put(f'/api/reports/{rid}/photos/{uuid.uuid4()}',headers=h,
        data={'checkpoint':key,'base_revision':'1'},files={'file':(name,data,mime)})
    assert r.status_code==200,r.text


def test_generated_connector_rules_are_inherited_for_every_connector():
    t=for_charger(TEMPLATE,{'connectors':'CCS2, CCS2, T2'})
    items=[i for s in t['sections'] for i in s['items'] if i['id'].startswith('CARG.01.')]
    assert len(items)==3 and all(i['photo_required'] and i['na_notes_required'] for i in items)
    labels={i['id']:i for s in t['sections'] for i in s['items']}
    assert labels['MAN.18']['label']=='Valor da resistência da terra'
    assert labels['MAN.19']['label']=='Verifique o estado dos filtros'
    assert all(labels[f'MAN.{i:02d}']['allow_status'] for i in range(14,19))
    assert labels['CARG.05']['label']=='Verifique o correto funcionamento dos meters no carregador - tire fotografias'


@pytest.mark.parametrize('key',['CARG.01.01','CARG.05'])
def test_na_requires_a_reason_but_no_photo(setup,key):
    c,h,rid,body=report(setup)
    body['payload']['answers'][key]={'value':'NA','notes':''}
    r=c.put('/api/reports/'+rid,headers=h,json=body)
    assert r.status_code==422 and key in r.text and 'observação' in r.text
    body['payload']['answers'][key]['notes']='Sem veículo/conector compatível para efetuar o teste.'
    r=c.put('/api/reports/'+rid,headers=h,json=body)
    assert r.status_code==200,r.text


@pytest.mark.parametrize('key',['CARG.01.01','CARG.05'])
def test_ok_requires_an_actual_photo(setup,key):
    c,h,rid,body=report(setup)
    body['payload']['answers'][key]={'value':'OK','notes':''}
    r=c.put('/api/reports/'+rid,headers=h,json=body)
    assert r.status_code==422 and 'falta fotografia' in r.text
    photo(c,h,rid,key)
    assert c.put('/api/reports/'+rid,headers=h,json=body).status_code==200


def test_video_does_not_replace_required_photo(setup):
    c,h,rid,body=report(setup)
    body['payload']['answers']['CARG.01.01']={'value':'OK','notes':''}
    photo(c,h,rid,'CARG.01.01',video=True)
    r=c.put('/api/reports/'+rid,headers=h,json=body)
    assert r.status_code==422 and 'falta fotografia' in r.text


@pytest.mark.parametrize('key',['MAN.12','MAN.18'])
@pytest.mark.parametrize('value',['OK','NOK'])
def test_manual_ok_nok_cannot_replace_reading(setup,key,value):
    c,h,rid,body=report(setup)
    body['payload']['answers'][key]={'value':value,'notes':'Dispositivo reprovado' if value=='NOK' else ''}
    r=c.put('/api/reports/'+rid,headers=h,json=body)
    assert r.status_code==422,r.text
    assert key in r.text
    body['payload']['answers'][key]={'value':'30 mA' if key=='MAN.12' else '120 Ω','assessment':value,'notes':'Teste efetuado'}
    r=c.put('/api/reports/'+rid,headers=h,json=body)
    assert r.status_code==200,r.text
