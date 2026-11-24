# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Checklist v6: manual insulation readings and revised photo requirements."""
import copy
import pytest
from app.main import FROZEN_TEMPLATES
from test_workflow import setup
from test_rules_056 import report, photo

@pytest.mark.parametrize('key',[f'MAN.{n}' for n in range(16,20)])
@pytest.mark.parametrize('status',['OK','NOK'])
def test_assessment_cannot_replace_manual_value(setup,key,status):
    c,h,rid,body=report(setup)
    for value in ['',status]:
        body['payload']['answers'][key]={'value':value,'assessment':status,'notes':'Teste efetuado'}
        r=c.put('/api/reports/'+rid,headers=h,json=body)
        assert r.status_code==422 and key in r.text,r.text
    body['payload']['answers'][key]={'value':'600,5 MΩ','assessment':status,'notes':'Teste efetuado'}
    photo(c,h,rid,key)
    r=c.put('/api/reports/'+rid,headers=h,json=body)
    assert r.status_code==200,r.text
    assert r.json()['payload']['answers'][key]==body['payload']['answers'][key]

@pytest.mark.parametrize('key',[f'MAN.{n}' for n in range(16,20)])
def test_na_exempts_reading_and_nok_needs_notes(setup,key):
    c,h,rid,body=report(setup)
    body['payload']['answers'][key]={'value':'450','assessment':'NOK','notes':''}
    r=c.put('/api/reports/'+rid,headers=h,json=body)
    assert r.status_code==422 and 'observação' in r.text
    body['payload']['answers'][key]={'value':'NA','notes':''}
    rejected=c.put('/api/reports/'+rid,headers=h,json=body)
    assert rejected.status_code==422 and 'observação' in rejected.text
    body['payload']['answers'][key]['notes']='Ensaio não aplicável ao equipamento'
    assert c.put('/api/reports/'+rid,headers=h,json=body).status_code==200

@pytest.mark.parametrize('key',['SITE.02','SITE.03','SITE.04'])
def test_site_requires_photo(setup,key):
    c,h,rid,body=report(setup)
    body['payload']['answers'][key]={'value':'OK','notes':''}
    r=c.put('/api/reports/'+rid,headers=h,json=body)
    assert r.status_code==422 and key in r.text and 'fotografia' in r.text
    photo(c,h,rid,key)
    assert c.put('/api/reports/'+rid,headers=h,json=body).status_code==200

def test_serial_is_multiple_and_photo_optional(setup):
    c,h,rid,body=report(setup)
    body['payload']['answers']['EST.04']={'value':'CH-001 / D1-002 / D2-003','notes':''}
    r=c.put('/api/reports/'+rid,headers=h,json=body)
    assert r.status_code==200,r.text
    assert r.json()['snapshot']['charger']['serial']=='CH-001 / D1-002 / D2-003'

@pytest.mark.parametrize('assessment',[None,True,{},'invalid'])
def test_invalid_assessment_rejected_even_in_draft(setup,assessment):
    c,h,rid,body=report(setup);body.pop('status')
    body['payload']['answers']['MAN.16']={'value':'600','assessment':assessment,'notes':''}
    r=c.put('/api/reports/'+rid,headers=h,json=body)
    assert r.status_code==422,r.text


def test_pdf_preserves_reading_and_assessment(setup):
    from io import BytesIO
    from pypdf import PdfReader
    c,h,rid,body=report(setup)
    body['payload']['answers']['MAN.16']={'value':'600,5 MΩ','assessment':'OK','notes':''}
    photo(c,h,rid,'MAN.16')
    assert c.put('/api/reports/'+rid,headers=h,json=body).status_code==200
    response=c.get('/api/reports/'+rid+'/pdf',headers=h)
    assert response.status_code==200
    text=''.join(page.extract_text(extraction_mode="layout") for page in PdfReader(BytesIO(response.content)).pages)
    assert '600,5 MΩ' in text and 'Avaliação: OK' in text

@pytest.mark.parametrize('value',['','OK','NOK','NA','30 mA'])
def test_trip_reading_and_photo_required_when_applicable(setup,value):
    from app.models import Photo
    c,h,rid,body=report(setup)
    body['payload']['answers']['MAN.13']={'value':value,'notes':'Teste efetuado'}
    with setup[3].state.factory() as db:
        for p in db.query(Photo).filter_by(report_id=rid,checkpoint='MAN.13').all(): db.delete(p)
        db.commit()
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==(200 if value=='NA' else 422),response.text
    if value=='NA': return
    assert ('algarismos' if value in ('', 'OK', 'NOK') else 'fotografia') in response.text,response.text
    photo(c,h,rid,'MAN.13')
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==(422 if value in ('','OK','NOK') else 200),response.text

@pytest.mark.parametrize('value',['OK','NOK','NA'])
def test_filters_status_choices(setup,value):
    c,h,rid,body=report(setup)
    body['payload']['answers']['MAN.21']={'value':value,'notes':'Filtro verificado'}
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==200,response.text

def test_existing_v6_draft_receives_corrections_but_closed_history_is_frozen():
    from app.checklist import corrected_draft_template
    old=copy.deepcopy(FROZEN_TEMPLATES[7])
    for section in old['sections']:
        for item in section['items']:
            if item['id'] in ('MAN.12','MAN.18'):
                item.pop('reading_required',None)
                item.pop('photo_always',None)
                item['photo_required']=False
            if item['id']=='MAN.19': item['kind']='text'
            if item['id']=='SEG.01': item.pop('na_notes_required', None)
    corrected=corrected_draft_template(old,'draft')
    items={i['id']:i for s in corrected['sections'] for i in s['items']}
    assert items['SEG.01']['na_notes_required']
    assert not items['MAN.13']['photo_always'] and items['MAN.13']['reading_required']
    assert items['MAN.20']['reading_required'] and items['MAN.21']['kind']=='status'
    assert corrected_draft_template(old,'closed')==old
    assert corrected_draft_template(old,'closed')!=corrected

@pytest.mark.parametrize('value,notes,expected', [('OK','',200),('NOK','',422),('NA',' ',200),('NOK','Limpeza incompleta',200),('NA','Não aplicável ao equipamento',200)])
def test_man02_status_requires_reason(setup,value,notes,expected):
    c,h,rid,body=report(setup)
    body['payload']['answers']['MAN.02']={'value':value,'notes':notes}
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==expected,response.text
    if expected==422: assert 'MAN.02' in response.text and 'observação' in response.text


def test_est18_and_man02_existing_draft_correction():
    from app.checklist import corrected_draft_template
    old=copy.deepcopy(FROZEN_TEMPLATES[7])
    for section in old['sections']:
        section['items']=[i for i in section['items'] if i['id']!='EST.18']
        for item in section['items']:
            if item['id']=='MAN.02':
                item['kind']='text'
                item.pop('na_notes_required',None)
    fixed=corrected_draft_template(old,'draft')
    items={i['id']:i for s in fixed['sections'] for i in s['items']}
    assert items['MAN.02']['kind']=='status' and not items['MAN.02']['na_notes_required']
    assert items['EST.18']['kind']=='notes' and items['EST.18']['optional']
    assert corrected_draft_template(fixed,'draft')==fixed
    assert corrected_draft_template(old,'closed')==old
