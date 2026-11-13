import pytest
from io import BytesIO
from pypdf import PdfReader
from test_workflow import setup
from test_rules_056 import report, photo

@pytest.mark.parametrize('key',['QVE.14','MAN.21'])
def test_general_notes_media_and_export(setup,key):
    c,h,rid,body=report(setup)
    body['payload']['answers'][key]={'value':'','notes':'Observacao geral de teste '+key}
    photo(c,h,rid,key)
    photo(c,h,rid,key,video=True)
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==200,response.text
    pdf=c.get('/api/reports/'+rid+'/pdf',headers=h)
    assert pdf.status_code==200
    text=''.join(p.extract_text() for p in PdfReader(BytesIO(pdf.content)).pages)
    assert 'Observacao geral de teste '+key in text
    assert c.get('/api/reports/'+rid+'/bundle',headers=h).status_code==200

def test_general_notes_can_be_omitted(setup):
    c,h,rid,body=report(setup)
    for key in ['QVE.14','MAN.21']:body['payload']['answers'].pop(key,None)
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==200,response.text
