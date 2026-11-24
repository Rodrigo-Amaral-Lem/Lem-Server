# SPDX-License-Identifier: GPL-3.0-only
"""MAN.16–20 require photographs; N/A requires a reason, not an attachment."""
from copy import deepcopy
from io import BytesIO
import uuid

import pytest
from PIL import Image

from app.checklist import corrected_draft_template
from app.main import TEMPLATE, FROZEN_TEMPLATES
from test_workflow import setup, upload, answers, add_signature
from test_video_evidence import video
from test_report_corrections import correction_case, start, body
from app.models import Report


def prepared(setup, key, value, notes='', assessment='OK'):
    client, headers, contract, app = setup
    assert upload(client, headers, contract['id'], True).status_code == 200
    charger = client.get(f'/api/contracts/{contract["id"]}/chargers', headers=headers).json()[0]
    path = '/api/reports/' + str(uuid.uuid4())
    payload = {'answers': answers()}
    payload['answers'][key] = {'value': value, 'notes': notes, 'assessment': assessment}
    data = {'charger_id': charger['id'], 'checklist_version': TEMPLATE['version'],
            'base_revision': 0, 'status': 'draft', 'payload': payload}
    assert client.put(path, headers=headers, json=data).status_code == 200
    add_signature(client, headers, path.rsplit('/', 1)[1], payload)
    data.update(base_revision=1, status='closed')
    return client, headers, path, data


@pytest.mark.parametrize('key', [f'MAN.{n}' for n in range(16, 21)])
@pytest.mark.parametrize('assessment', ['OK', 'NOK'])
def test_measurement_requires_evidence_for_both_assessments(setup, key, assessment):
    c, h, path, data = prepared(setup, key, '501', 'Resultado verificado', assessment)
    result = c.put(path, headers=h, json=data)
    assert result.status_code == 422 and key + ': falta fotografia' in result.text
    assert c.get(path, headers=h).json()['status'] == 'draft'
    image = BytesIO()
    Image.new('RGB', (16, 24), 'blue').save(image, format='PNG')
    assert c.put(path + '/photos/' + str(uuid.uuid4()), headers=h,
                 data={'checkpoint': key, 'base_revision': '1'},
                 files={'file': ('reading.png', image.getvalue(), 'image/png')}).status_code == 200
    saved = c.put(path, headers=h, json=data)
    assert saved.status_code == 200, saved.text


@pytest.mark.parametrize('key', [f'MAN.{n}' for n in range(16, 21)])
def test_na_needs_observation_without_media(setup, key):
    c, h, path, data = prepared(setup, key, 'NA')
    rejected = c.put(path, headers=h, json=data)
    assert rejected.status_code == 422 and key + ': N/A precisa de observação' in rejected.text
    assert 'falta fotografia' not in rejected.text
    data['payload']['answers'][key]['notes'] = 'Ensaio não realizado: equipamento em serviço'
    assert c.put(path, headers=h, json=data).status_code == 200


@pytest.mark.parametrize('key', [f'MAN.{n}' for n in range(16, 21)])
def test_real_video_never_satisfies_mandatory_photograph(setup, video, key):
    c, h, path, data = prepared(setup, key, '501', 'Leitura registada')
    result = c.put(path + '/photos/' + str(uuid.uuid4()), headers=h,
                   data={'checkpoint': key, 'base_revision': '1'},
                   files={'file': ('reading.mp4', video, 'video/mp4')})
    assert result.status_code == 200, result.text
    saved = c.put(path, headers=h, json=data)
    assert saved.status_code == 422 and key + ': falta fotografia' in saved.text


def test_old_v6_drafts_upgrade_but_closed_history_is_unchanged():
    old = deepcopy(FROZEN_TEMPLATES[7])
    for section in old['sections']:
        for item in section['items']:
            if 'MAN.14' <= item['id'] <= 'MAN.18':
                item['photo_required'] = False
                item.pop('na_notes_required', None)
    closed = deepcopy(old)
    assert corrected_draft_template(old, 'closed') == closed
    upgraded = corrected_draft_template(old, 'draft')
    for section in upgraded['sections']:
        for item in section['items']:
            if 'MAN.16' <= item['id'] <= 'MAN.20':
                assert item['photo_required'] and item['na_notes_required'] and not item['photo_always']


def test_direct_correction_of_old_closed_template_enforces_photograph(correction_case):
    c, headers, _, app, original = correction_case
    with app.state.factory() as db:
        report = db.get(Report, original['id'])
        template = deepcopy(FROZEN_TEMPLATES[7])
        for section in template['sections']:
            for item in section['items']:
                if item['id'] == 'MAN.14':
                    item['photo_required'] = False
        payload = deepcopy(report.payload)
        from app.maintenance_migration import trip_time_checkpoint
        current = payload['answers']
        payload['answers'] = {item['id']: current[trip_time_checkpoint(item['id'])]
                              for group in template['sections'] for item in group['items']
                              if trip_time_checkpoint(item['id']) in current}
        payload['answers']['MAN.14'] = {'value': '600 MΩ', 'notes': '', 'assessment': 'OK'}
        report.template = template
        report.payload = payload
        db.commit()
    original.update(c.get(f'/api/reports/{original["id"]}', headers=headers).json())
    working = start(correction_case)
    result = c.post(f'/api/reports/{working["id"]}/correction/commit', headers=headers, json=body(working))
    assert result.status_code == 422 and 'MAN.16: falta fotografia' in result.text
    assert c.get(f'/api/reports/{original["id"]}', headers=headers).json() == original
