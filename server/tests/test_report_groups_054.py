# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Release 0.1.54: checklist semantics, signature validation and exported evidence."""
import copy, uuid
from io import BytesIO
from zipfile import ZipFile
import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader
from test_workflow import setup, upload, answers
from app.main import TEMPLATE


def test_exact_groups_questions_and_photo_requirements():
    sections=TEMPLATE['sections']
    assert [s['id'] for s in sections]==['SEG','SITE','QVE','EST','MAN','CARG','FIN']
    groups={s['id']:s['items'] for s in sections}
    # EST.10 was retired; MAN.21 now holds after-maintenance photos and the
    # previous general observations moved to MAN.22 without renumbering EST.
    assert len(groups['SEG'])==7 and len(groups['EST'])==17 and len(groups['QVE'])==14 and len(groups['MAN'])==22
    assert 'EST.10' not in {i['id'] for i in groups['EST']}
    assert groups['MAN'][-1]['id'] == 'MAN.22'
    assert [groups[key][-1]['kind'] for key in ('EST', 'QVE', 'MAN')] == ['notes'] * 3
    items={i['id']:i for s in sections for i in s['items']}
    for key in ['SEG.01','SITE.02','SITE.03','SITE.04','EST.06','EST.13','EST.16','QVE.01','QVE.04','FIN.01']:
        assert items[key]['photo_required'], key
    assert not items['EST.04']['photo_required']
    assert not any(i['photo_required'] for i in groups['SEG'][1:])
    assert items['EST.06']['label']=='Verifique o ID da estação'
    assert items['QVE.03']['label']=='Botoneira de emergência - Verifique a dupla sinalização'
    assert items['MAN.13']['label']=='Proteção diferencial QVE e Carregador - Teste manual do dispositivo'
    assert items['MAN.01']['label'].startswith('Atualize o software')
    assert items['FIN.01']['label']=='Fotografia da Estação de Carregamento após intervenção'
    assert items['FIN.02']['supports_signature']
    assert not any(i['id'].startswith(('PC','PG')) for i in items.values())


def report(setup):
    client,h,contract,_=setup
    upload(client,h,contract['id'],True)
    charger=client.get('/api/contracts/'+contract['id']+'/chargers',headers=h).json()[0]
    rid=str(uuid.uuid4())
    body={'checklist_version': TEMPLATE["version"], 'charger_id':charger['id'],'base_revision':0,'status':'draft','payload':{'answers':answers()}}
    body['payload']['answers']['FIN.02']['value']='Técnico de teste'
    r=client.put('/api/reports/'+rid,headers=h,json=body)
    assert r.status_code==200,r.text
    body['base_revision']=r.json()['revision']
    return client,h,rid,body


def signature(photo_id):
    return {'FIN.02':{'photo_id':photo_id,'signed_by':'Técnico de teste','signed_at':'2026-11-13T10:00:00Z'}}


@pytest.mark.parametrize('invalid',[
    [], {'EST.01':{'photo_id':'x','signed_by':'x','signed_at':'x'}},
    {'FIN.02':[]}, {'FIN.02':{'photo_id':5,'signed_by':'Técnico de teste','signed_at':'2026-11-13T10:00:00Z'}},
    {'FIN.02':{'photo_id':'x','signed_by':'Outro técnico','signed_at':'2026-11-13T10:00:00Z'}},
    {'FIN.02':{'photo_id':'x','signed_by':'Técnico de teste','signed_at':'2026-11-13T10:00:00'}},
])
def test_bad_signature_metadata_rejected_without_revision_change(setup,invalid):
    c,h,rid,body=report(setup)
    body['payload']['signatures']=invalid
    r=c.put('/api/reports/'+rid,headers=h,json=body)
    assert r.status_code==422,r.text
    assert c.get('/api/reports/'+rid,headers=h).json()['revision']==1


def test_signature_requires_its_own_png_at_close_and_survives_exports(setup):
    c,h,rid,body=report(setup)
    pid=str(uuid.uuid4())
    body['payload']['signatures']=signature(pid)
    body['status']='closed'
    missing=c.put('/api/reports/'+rid,headers=h,json=body)
    assert missing.status_code==422 and 'assinatura em falta' in missing.text
    image=Image.new('RGB',(300,100),'white');ImageDraw.Draw(image).line([(20,70),(80,20),(180,80),(260,30)],fill='black',width=3)
    buf=BytesIO();image.save(buf,format='PNG');png=buf.getvalue()
    p=c.put(f'/api/reports/{rid}/photos/{pid}',headers=h,data={'checkpoint':'FIN.02','base_revision':str(body['base_revision'])},files={'file':('signature.png',png,'image/png')})
    assert p.status_code==200,p.text
    from test_rules_056 import photo
    photo(c,h,rid,'MAN.12')
    body['payload']['qve_copy']={'source_report_id':str(uuid.uuid4()),'source_charger_id':str(uuid.uuid4()),'source_station_id':'ORIGEM-QVE-001','copied_by':'Técnico de teste','copied_at':'2026-11-13T10:00:00Z'}
    closed=c.put('/api/reports/'+rid,headers=h,json=body)
    assert closed.status_code==200,closed.text
    assert closed.json()['payload']['signatures']==signature(pid)
    pdf=c.get('/api/reports/'+rid+'/pdf',headers=h)
    assert pdf.status_code==200,pdf.text[:200] if not pdf.content.startswith(b'%PDF') else ''
    text=''.join(p.extract_text() for p in PdfReader(BytesIO(pdf.content)).pages)
    assert 'Assinatura de Técnico de teste' in text
    assert 'ORIGEM-QVE-001' in text
    assert 'FIN - Informações Gerais e Final' in text
    data=c.get('/api/reports/'+rid+'/bundle',headers=h)
    assert data.status_code==200
    with ZipFile(BytesIO(data.content)) as z:
        assert z.read(f'fotografias/FIN.02/{closed.json()["identifier"]}_{pid}.png')==png


def test_gallery_image_at_different_checkpoint_does_not_satisfy_signature(setup):
    c,h,rid,body=report(setup)
    pid=str(uuid.uuid4());buf=BytesIO();Image.new('RGB',(30,30),'white').save(buf,format='PNG')
    p=c.put(f'/api/reports/{rid}/photos/{pid}',headers=h,data={'checkpoint':'QVE.01','base_revision':'1'},files={'file':('qve.png',buf.getvalue(),'image/png')})
    assert p.status_code==200
    body['status']='closed';body['payload']['signatures']=signature(pid)
    assert c.put('/api/reports/'+rid,headers=h,json=body).status_code==422
