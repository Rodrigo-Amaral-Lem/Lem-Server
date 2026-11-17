# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

import pytest
import hashlib
import json
from zipfile import ZipFile
from io import BytesIO
from pypdf import PdfReader
from test_workflow import setup
from test_rules_056 import report, photo


def assert_notes_photo_in_pdf(pdf):
    """Inspect decoded image pixels, not just a label or unused PDF resource."""
    reader = PdfReader(BytesIO(pdf))
    matches = []
    for page in reader.pages:
        drawn = set()
        resources = page['/Resources'].get('/XObject', {})
        def record_image(op, operands, cm, tm):
            if op == b'Do':
                reference = resources[operands[0]].indirect_reference
                drawn.add((reference.idnum, reference.generation))
        page.extract_text(visitor_operand_before=record_image)
        for entry in page.images:
            reference = entry.indirect_reference
            if (entry.image.size == (67, 43) and reference is not None and
                    (reference.idnum, reference.generation) in drawn):
                matches.append(entry.image.convert('RGB'))
    assert matches, 'The general-observations photograph is absent from the PDF'
    assert any(all(abs(actual - expected) < 5 for actual, expected in
                   zip(image.getpixel((30, 20)), (184, 36, 127))) for image in matches)

@pytest.mark.parametrize('key',['SEG.07','SITE.06','QVE.14','EST.18','MAN.22','CARG.07','FIN.03'])
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
    archive = c.get('/api/reports/'+rid+'/bundle',headers=h)
    assert archive.status_code == 200
    # A successful response is insufficient: every original must be in the ZIP,
    # including optional notes fields, with its exact bytes and checkpoint path.
    with ZipFile(BytesIO(archive.content)) as zipped:
        assert zipped.testzip() is None
        manifest = json.loads(zipped.read('report.json'))
        media = [p for p in manifest['photos'] if p['checkpoint'] == key]
        assert len(media) == 2
        for p in media:
            folder, ext = ('videos', 'mp4') if p['mime'].startswith('video/') else ('fotografias', 'png')
            original = zipped.read(f"{folder}/{key}/{p['id']}.{ext}")
            assert hashlib.sha256(original).hexdigest() == p['digest']


@pytest.mark.parametrize('empty_answer', [None, {'value': '', 'notes': ''}])
def test_general_notes_zip_keeps_media_without_any_observation_text(setup, empty_answer):
    c, h, rid, body = report(setup)
    keys = ['SEG.07', 'SITE.06', 'QVE.14', 'EST.18', 'MAN.22', 'CARG.07', 'FIN.03']
    expected = {}
    for key in keys:
        body['payload']['answers'][key] = empty_answer
        for video in (False, True):
            pid = photo(c, h, rid, key, video=video)
            folder, ext = ('videos', 'mp4') if video else ('fotografias', 'png')
            expected[f'{folder}/{key}/{pid}.{ext}'] = c.get(
                f'/api/reports/{rid}/photos/{pid}', headers=h).content
    saved = c.put('/api/reports/' + rid, headers=h, json=body)
    assert saved.status_code == 200, saved.text
    archive = c.get('/api/reports/' + rid + '/bundle', headers=h)
    assert archive.status_code == 200, archive.text
    with ZipFile(BytesIO(archive.content)) as zipped:
        for filename, original in expected.items():
            assert zipped.read(filename) == original

def test_general_notes_can_be_omitted(setup):
    c,h,rid,body=report(setup)
    for key in ['SITE.06','QVE.14','EST.18','MAN.22','CARG.07','FIN.03']:body['payload']['answers'].pop(key,None)
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==200,response.text


@pytest.mark.parametrize('version', [3, 6])
def test_corrected_old_est18_media_survives_pdf_zip_and_another_correction(setup, version):
    from copy import deepcopy
    from pathlib import Path
    import uuid
    from PIL import Image
    from app.models import Photo, Report
    from test_export_resources import make_report
    from test_report_corrections import start, body
    from test_workflow import answers, add_signature

    c, headers, contract, app = setup
    headers = {**headers, 'X-Lem-Client-Platform': 'windows'}
    rid = make_report(c, headers, contract, closed=True)
    image = BytesIO()
    Image.new('RGB', (67, 43), (184, 36, 127)).save(image, format='PNG')
    originals = {'image/png': image.getvalue(), 'video/mp4':
                 (Path(__file__).with_name('fixtures') / 'evidence-clip.mp4').read_bytes()}
    # Simulate the saved template and originals of an older installed release.
    # No observation text is needed for attached evidence to remain exportable.
    with app.state.factory() as db:
        stored = db.get(Report, rid)
        schema = deepcopy(stored.template)
        schema['version'] = version
        if version == 6:
            for section in schema['sections']:
                if section['id'] == 'MAN':
                    section['items'] = [item for item in section['items'] if item['id'] != 'MAN.21']
                    next(item for item in section['items'] if item['id'] == 'MAN.22')['id'] = 'MAN.21'
        stored.template = schema
        payload = deepcopy(stored.payload)
        payload['answers']['EST.18'] = None
        if version == 6:
            payload['answers'].pop('MAN.21', None)
            payload['answers']['MAN.21'] = payload['answers'].pop('MAN.22')
        stored.payload = payload
        for mime, data in originals.items():
            pid = str(uuid.uuid4())
            db.add(Photo(id=pid, report_id=rid, checkpoint='EST.18', filename=pid,
                         mime=mime, digest=hashlib.sha256(data).hexdigest(), data=data))
        db.commit()

    # Repeat correction to cover already-migrated historical checkpoints as well.
    for _ in range(2):
        original = c.get('/api/reports/' + rid, headers=headers).json()
        working = start((c, headers, contract, app, original))
        assert working['template']['version'] == 7
        photo_ids = {p['id'] for p in working['photos']}
        assert all(signature['photo_id'] in photo_ids
                   for signature in working['payload'].get('legacy_signatures', {}).values())
        working['payload']['answers'].update(answers())
        add_signature(c, headers, working['id'], working['payload'], revision=working['revision'])
        result = c.post('/api/reports/' + working['id'] + '/correction/commit',
                        headers=headers, json=body(working))
        assert result.status_code == 200, result.text
        rid = working['id']
        exported = c.get('/api/reports/' + rid + '/pdf', headers=headers)
        assert exported.status_code == 200, exported.text
        assert_notes_photo_in_pdf(exported.content)
        archive = c.get('/api/reports/' + rid + '/bundle', headers=headers)
        assert archive.status_code == 200, archive.text
        with ZipFile(BytesIO(archive.content)) as zipped:
            manifest = json.loads(zipped.read('report.json'))
            assert_notes_photo_in_pdf(zipped.read(manifest['identifier'] + '.pdf'))
            for mime, data in originals.items():
                digest = hashlib.sha256(data).hexdigest()
                media = next(p for p in manifest['photos'] if p['digest'] == digest)
                folder, ext = ('videos', 'mp4') if mime.startswith('video/') else ('fotografias', 'png')
                assert zipped.read(f"{folder}/{media['checkpoint']}/{media['id']}.{ext}") == data


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


def test_pdf_omits_local_and_displays_empty_observations(tmp_path):
    from app.pdf_report import build_pdf
    from app.main import TEMPLATE
    keys = [i['id'] for s in TEMPLATE['sections'] for i in s['items'] if i['kind'] == 'notes']
    assert len(keys) == 7
    # Missing, empty, whitespace-only and legacy null observation answers are all empty.
    a = {keys[1]: {}, keys[2]: {'value': '  ', 'notes': ' \n '}, keys[3]: None,
         keys[4]: {'value': '', 'notes': 'Nota escrita preservada'},
         keys[5]: {'value': 'Texto antigo preservado', 'notes': ''},
         keys[6]: {'value': 'Valor historico preservado', 'notes': 'Comentario preservado'}}
    items = [i for s in TEMPLATE['sections'] for i in s['items'] if i['kind'] == 'notes']
    items += [{'id': 'EST.02', 'label': 'Fabricante', 'kind': 'text', 'photo_required': False}]
    report = dict(id='test', revision=1, status='closed', technician='Teste', started='', finished='',
                  snapshot={'charger': {'external_id': 'TEST', 'location': 'LOCAL_OMITIDO', 'address': 'Morada preservada'},
                            'contract': {'name': 'Teste', 'company': 'Teste'}},
                  template={'sections': [{'id': 'TEST', 'title': 'Observações', 'items': items}]},
                  payload={'answers': a})
    pdf = build_pdf(report, [])
    (tmp_path / 'observations.pdf').write_bytes(pdf)
    text = '\n'.join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
    assert 'LOCAL_OMITIDO' not in text and 'Local' not in text
    assert 'Morada preservada' in text
    assert text.count('Sem observações') == 4
    assert text.count('Por preencher') == 1  # Only the ordinary unanswered item.
    for preserved in ['Nota escrita preservada', 'Texto antigo preservado',
                      'Valor historico preservado', 'Comentario preservado']:
        assert text.count(preserved) == 1
