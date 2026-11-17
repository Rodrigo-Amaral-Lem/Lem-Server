import json, uuid
from io import BytesIO
import pytest
from pypdf import PdfReader
from test_workflow import setup, xlsx, MAPPING
from app.pdf_report import build_pdf
from app.branding import selection
from app.main import TEMPLATE

def send(c,h,cid,logos,commit=True):
    return c.post(f'/api/contracts/{cid}/import',headers=h,files={'file':('test.xlsx',xlsx())},data={'mapping':json.dumps({**MAPPING,'report_logos':logos}),'commit':str(commit).lower()})

def test_branding_persistence_preview_validation_and_frozen_report(setup,tmp_path):
    c,h,contract,app=setup; cid=contract['id']
    def logos(): return c.get('/api/contracts',headers=h).json()[0]['report_logos']
    assert [r['id'] for r in logos()]==[]
    assert send(c,h,cid,['APS','Galp'],False).status_code==200
    assert [r['id'] for r in logos()]==[]
    for invalid in (['APS','APS'],['missing'],['APS','Galp','Lem'],[{}]):
        assert send(c,h,cid,invalid).status_code==422
    assert send(c,h,cid,['APS','Galp']).status_code==200
    assert [r['id'] for r in logos()]==['APS','Galp']
    charger=c.get(f'/api/contracts/{cid}/chargers',headers=h).json()[0]['id']
    rid=str(uuid.uuid4())
    response=c.put(f'/api/reports/{rid}',headers=h,json={'charger_id':charger,'base_revision':0,'checklist_version':TEMPLATE['version'],'payload':{'answers':{}},'status':'draft'})
    assert response.status_code==200,response.text
    report=c.get(f'/api/reports/{rid}',headers=h).json()
    assert [r['id'] for r in report['snapshot']['contract']['report_logos']]==['APS','Galp']
    assert send(c,h,cid,['Transdev']).status_code==200
    assert [r['id'] for r in logos()]==['Transdev']
    frozen=c.get(f'/api/reports/{rid}',headers=h).json()
    assert frozen['snapshot']==report['snapshot']
    pdf=build_pdf(frozen,[])
    (tmp_path/'two-logos.pdf').write_bytes(pdf)
    reader=PdfReader(BytesIO(pdf))
    assert len(reader.pages[0].images)==2  # Live contract presentation, frozen answers/identity.
    assert 'RELATÓRIO DE MANUTENÇÃO' in ' '.join(reader.pages[0].extract_text().split())
    frozen.pop('presentation')  # Compatibility fallback for pre-presentation offline reports.
    frozen['snapshot']['contract']['report_logos']=selection(['Transdev'])
    assert len(PdfReader(BytesIO(build_pdf(frozen,[]))).pages[0].images)==2
    for name, ids in [('none', []), ('one', ['APS']), ('two', ['APS', 'Galp']),
                      ('legacy-lem', ['Lem']), ('legacy-lem-partner', ['Lem', 'APS']),
                      ('legacy-missing', None)]:
        if ids is None:
            frozen['snapshot']['contract'].pop('report_logos', None)
        else:
            frozen['snapshot']['contract']['report_logos'] = selection(ids)
        pdf = build_pdf(frozen, [])
        (tmp_path / (name + '.pdf')).write_bytes(pdf)
        page = PdfReader(BytesIO(pdf)).pages[0]
        expected = 1 + len([i for i in (ids or []) if i != 'Lem'])
        assert len(page.images) == expected
        positions = []
        def image_position(op, operands, cm, tm):
            if op == b'Do': positions.append(cm.copy())
        page.extract_text(visitor_operand_before=image_position)
        assert len(positions) == expected
        assert positions[0][4] < 100  # Default issuer at the left margin.
        assert all(pos[4] > 230 for pos in positions[1:])
    assert send(c,h,cid,[]).status_code == 200
    assert logos() == []
    assert c.get(f'/api/reports/{rid}',headers=h).json()['snapshot'] == report['snapshot']
