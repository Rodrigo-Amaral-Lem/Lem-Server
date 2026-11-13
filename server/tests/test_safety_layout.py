from app.main import TEMPLATE
from test_workflow import setup
from test_rules_056 import report

def test_safety_optional_notes_and_nok_without_notes(setup):
    c,h,rid,body=report(setup)
    assert [s['id'] for s in TEMPLATE['sections']]==['SEG','SITE','QVE','EST','MAN','CARG','FIN']
    body['payload']['answers'].pop('SEG.07',None)
    for n in range(2,7): body['payload']['answers'][f'SEG.{n:02d}']={'value':'NOK','notes':''}
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==200,response.text

def test_safety_notes_only_can_close(setup):
    c,h,rid,body=report(setup)
    body['payload']['answers']['SEG.07']={'value':'','notes':'Acesso condicionado durante o trabalho'}
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==200,response.text
