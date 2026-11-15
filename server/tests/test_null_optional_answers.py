# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
import pytest
from app.main import TEMPLATE
from test_workflow import setup
from test_rules_056 import report


def test_legacy_closed_report_upload_with_null_optional_answers(setup):
    c,h,rid,body=report(setup)
    optional=[i['id'] for g in TEMPLATE['sections'] for i in g['items'] if i.get('optional')]
    for key in optional: body['payload']['answers'][key]=None
    original=deepcopy(body['payload'])
    body['status']='draft'
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==200,response.text
    saved=response.json()
    assert saved['payload']['signatures']==original['signatures']
    for key,a in original['answers'].items():
        if key not in optional: assert saved['payload']['answers'][key]==a
    assert all(key not in saved['payload']['answers'] for key in optional)
    body['base_revision']=saved['revision'];body['status']='closed'
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==200,response.text
    assert response.json()['status']=='closed'
    assert c.get('/api/reports/'+rid+'/pdf',headers=h).status_code==200

@pytest.mark.parametrize('key,value',[('SEG.01',None),('SITE.06',42),('SITE.06',{'notes':None}),('UNKNOWN',None)])
def test_other_invalid_answers_are_not_silently_discarded(setup,key,value):
    c,h,rid,body=report(setup)
    body['payload']['answers'][key]=value
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==422,response.text
    if key!='UNKNOWN': assert key in response.text
