# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

import pytest
from io import BytesIO
from pypdf import PdfReader
from test_workflow import setup
from test_rules_056 import report, photo

@pytest.mark.parametrize('key',['SITE.06','QVE.14','EST.18','MAN.21','CARG.07','FIN.03'])
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
    for key in ['SITE.06','QVE.14','EST.18','MAN.21','CARG.07','FIN.03']:body['payload']['answers'].pop(key,None)
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==200,response.text


def test_site_notes_added_once_to_existing_draft_only():
    from copy import deepcopy
    from app.main import TEMPLATE
    from app.checklist import corrected_draft_template
    old = deepcopy(TEMPLATE)
    site = next(s for s in old['sections'] if s['id'] == 'SITE')
    site['items'] = [i for i in site['items'] if i['id'] != 'SITE.06']
    fixed = corrected_draft_template(old, 'draft')
    items = next(s['items'] for s in fixed['sections'] if s['id'] == 'SITE')
    assert items[-1]['id'] == 'SITE.06'
    assert items[-1]['kind'] == 'notes' and items[-1]['optional']
    assert corrected_draft_template(fixed, 'draft') == fixed
    assert corrected_draft_template(old, 'closed') == old


def test_legacy_final_notes_preserved():
    from app.checklist import corrected_draft_payload, corrected_draft_template
    template = {"version": 6, "sections": [{"id": "FIN", "items": [{"id": "FIN.03", "kind": "text", "label": "Observações", "photo_required": False}]}]}
    fixed = corrected_draft_template(template, "draft")
    item = fixed["sections"][0]["items"][0]
    assert item["kind"] == "notes" and item["optional"]
    payload = {"answers": {"FIN.03": {"value": "Texto anterior", "notes": "Comentário"}}}
    migrated = corrected_draft_payload(payload, template, "draft")
    assert migrated["answers"]["FIN.03"] == {"value": "", "notes": "Texto anterior\nComentário"}
    assert corrected_draft_payload(migrated, fixed, "draft") == migrated
    assert corrected_draft_payload(payload, template, "closed") == payload
    assert corrected_draft_template(template, "closed") == template
