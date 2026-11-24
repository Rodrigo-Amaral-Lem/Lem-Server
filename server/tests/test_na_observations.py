# SPDX-License-Identifier: GPL-3.0-only
import pytest
from copy import deepcopy
from app.main import TEMPLATE, FROZEN_TEMPLATES
from app.checklist import corrected_draft_template
from test_workflow import setup
from test_rules_056 import report

KEYS='SITE.02 SITE.03 SITE.04 QVE.02 QVE.04 QVE.09 QVE.10 QVE.11 QVE.12 EST.01 EST.16 EST.17 MAN.13 MAN.14 MAN.15 MAN.20 MAN.22'.split()

@pytest.mark.parametrize('key',KEYS)
def test_na_requires_submitted_reason(setup,key):
    c,h,rid,body=report(setup)
    body['payload']['answers'][key]={'value':'NA','notes':' '}
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==422 and key in response.text and 'observação' in response.text,response.text
    body['payload']['answers'][key]['notes']='Justificação verificada no local'
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==200,response.text

def test_draft_upgrade_and_closed_history():
    old=deepcopy(FROZEN_TEMPLATES[7])
    for section in old['sections']:
        if section['id']=='SITE': section['title']='SITE - Site'
        section['items']=[i for i in section['items'] if i['id']!='CARG.07']
        for item in section['items']:
            if item['id']=='SITE.05': item['label']='SITE - '+item['label']
            if item['id']=='SITE.01': item['label']='SITE - Verifique a limpeza geral'
            if item['id'] in KEYS or item['id']=='QVE.03':
                item.pop('na_notes_required',None);item.pop('na_notes_default',None)
            if item['id']=='MAN.02':item['na_notes_required']=True
            if item['id']=='MAN.12':item['photo_always']=True
            if item['id']=='FIN.02':item.pop('signature_required',None);item.pop('media_disabled',None);item.pop('notes_visible',None)
            if item['id'] in ['QVE.01','QVE.02','QVE.03']:item['label']=item['label'].replace('Botoneira','Botão')
    fixed=corrected_draft_template(old,'draft')
    items={i['id']:i for s in fixed['sections'] for i in s['items']}
    canonical={i['id']:i for s in TEMPLATE['sections'] for i in s['items']}
    for key in KEYS+['SITE.05','SITE.01','MAN.02','FIN.02','CARG.07','QVE.01','QVE.03']:
        assert items[key]==canonical[key],key
    assert next(s for s in fixed['sections'] if s['id']=='SITE')['title']=='SITE - Posto de carregamento'
    assert corrected_draft_template(fixed,'draft')==fixed
    assert corrected_draft_template(old,'closed')==old


def test_connector_suggestions_and_measurement_labels():
    from app.checklist import for_charger
    expanded=for_charger(TEMPLATE, {'connectors':'CCS2, CCS2, T2'})
    items={i['id']:i for s in expanded['sections'] for i in s['items']}
    connectors=[i for key,i in items.items() if key.startswith('CARG.01.')]
    assert len(connectors)==3
    assert all(i['na_notes_default']=='Veículo usado não compatível' for i in connectors)
    for key in ['MAN.11','MAN.13']:
        assert 'intensidade' in items[key]['label'] and 'corrente' not in items[key]['label']
    old=for_charger(FROZEN_TEMPLATES[7], {'connectors':'CCS2, CCS2, T2'})
    for section in old['sections']:
        for item in section['items']:
            if item['id'].startswith('CARG.01.'): item.pop('na_notes_default',None)
            if item['id'] in ['MAN.11','MAN.12']: item['label']=item['label'].replace('intensidade','corrente')
    assert corrected_draft_template(old,'draft')==expanded
    assert corrected_draft_template(old,'closed')==old
