# SPDX-License-Identifier: GPL-3.0-only
"""Cross-language migration contracts; keep future form changes lossless/retryable."""
from copy import deepcopy
import json
import os
from pathlib import Path
import random
import re
import subprocess

import pytest
from app.checklist import corrected_draft_template, corrected_draft_payload, for_charger
from app.main import TEMPLATE, LEGACY_TEMPLATE
from app.maintenance_migration import upgrade_checkpoint
from app.models import Report
from test_workflow import setup
from test_maintenance_v7 import legacy_fixture


def project(report):
    result = deepcopy(report)
    schema = result['template']
    if result['status'] != 'draft' or schema.get('frozen_upload') or schema['version'] not in (6, 7):
        return result
    for key in ('payload', '_inflight_payload'):
        if key in result:
            result[key] = corrected_draft_payload(result[key], schema, 'draft')
    if schema['version'] == 6 and 'photos' in result:
        result['photos'] = [dict(photo, checkpoint=upgrade_checkpoint(photo['checkpoint'])) for photo in result['photos']]
    result['template'] = corrected_draft_template(schema, 'draft')
    return result


def cases():
    rng = random.Random(20261117)
    result = []
    for n in range(48):
        schema = deepcopy(LEGACY_TEMPLATE if n % 2 == 0 else TEMPLATE)
        answers = {'FIN.03': {'value': 'Texto anterior '+str(n), 'notes': 'Nota atual '+str(n)},
                   'EST.10': {'value': 'NOK', 'notes': 'Limpeza '+str(n)},
                   'EST.18': {'value': '', 'notes': 'Observações '+str(n)},
                   'MAN.11': {'value': rng.choice(['0,03 A', '1 A', '30 mA', '30', 'NA'])},
                   'MAN.12': {'value': rng.choice(['15 mA', '30 mA', '31,5 mA', '30', 'NA'])},
                   'MAN.21': {'value': '', 'notes': 'Manutenção '+str(n)}}
        if schema['version'] == 7:
            answers.pop('EST.10')
            answers['MAN.22'] = answers.pop('MAN.21')
        payload = {'answers': answers}
        report = {'status': 'draft', 'template': schema, 'payload': deepcopy(payload),
                  '_inflight_payload': deepcopy(payload),
                  'photos': [{'id': 'unchanged', 'checkpoint': 'MAN.21', 'digest': 'unchanged-bytes'}]}
        if n % 12 in (2, 3):
            report['status'] = 'closed'
        if n % 12 in (4, 5):
            schema['frozen_upload'] = True
        result.append(report)
    # Future/ancient versions cannot be reinterpreted by assuming that an ID or
    # unit still has today's meaning. New migrations need an explicit contract.
    for version in (3, 8, 99):
        for status in ('draft', 'closed'):
            other = deepcopy(result[0])
            other['template']['version'] = version
            other['status'] = status
            result.append(other)
    collision = deepcopy(result[0])
    for payload_key in ('payload', '_inflight_payload'):
        collision[payload_key]['answers']['MAN.21'] = {'value': 'V'*8000, 'notes': 'N'*8000}
        collision[payload_key]['answers']['MAN.22'] = {'value': '', 'notes': 'G'*8000}
    result.append(collision)
    return result


def native_projection(tmp_path, reports):
    binary = os.getenv('LEM_MIGRATION_BINARY')
    if not binary:
        pytest.skip('Set LEM_MIGRATION_BINARY to the native Lem-migration-projection executable')
    source, output = tmp_path/'input.json', tmp_path/'output.json'
    source.write_text(json.dumps(reports, ensure_ascii=False), encoding='utf-8')
    subprocess.run([str(Path(binary).resolve()), str(source), str(output)], check=True, timeout=20)
    return json.loads(output.read_text(encoding='utf-8'))


def test_maintained_migration_reaches_the_canonical_form():
    # Bumping shared/checklist-v1.json alone must fail the release checks. The
    # migration chain and native/server cases need to advance together.
    migrated = corrected_draft_template(LEGACY_TEMPLATE, 'draft')
    assert migrated['version'] == TEMPLATE['version']
    assert corrected_draft_template(migrated, 'draft') == migrated


def test_native_and_server_migrate_payload_and_lost_ack_identically(tmp_path):
    reports = cases()
    projected = native_projection(tmp_path, reports)
    for n, (original, actual) in enumerate(zip(reports, projected)):
        expected = project(original)
        assert actual == expected, f'Native/server projection differs in case {n}'
        assert actual['payload'] == actual['_inflight_payload'], f'Lost ACK becomes a false conflict in case {n}'
        assert project(expected) == expected, f'Migration is not idempotent in case {n}'
        assert actual['photos'][0]['id'] == 'unchanged'
        ids = [item['id'] for section in actual['template']['sections'] for item in section['items']]
        assert len(ids) == len(set(ids)), f'Migration produced duplicate item IDs in case {n}'
    assert native_projection(tmp_path, projected) == projected


def test_generated_connector_questions_inherit_all_template_rules(tmp_path):
    schema = deepcopy(TEMPLATE)
    prototype = next(i for s in schema['sections'] for i in s['items'] if i['id'].startswith('CARG.01.'))
    # A future rule/help addition must survive expansion to physical connectors.
    prototype.update(photo_always=True, help='Instrução do formulário', future_rule={'revision': 2})
    details = {'connectors': '2x AC Type 2; CCS2'}
    expected = for_charger(schema, details)
    generated = [i for s in expected['sections'] for i in s['items'] if i['id'].startswith('CARG.01.')]
    assert len(generated) == 3
    for item in generated:
        for key, value in prototype.items():
            if key not in ('id', 'label'):
                assert item.get(key) == value, f'Connector expansion dropped {key}'
    actual = native_projection(tmp_path, [{'operation': 'connectors', 'template': schema, 'details': details}])
    assert actual == [expected]


def test_maximum_legacy_notes_survive_merge_save_reload_and_export(setup, tmp_path):
    from io import BytesIO
    from pypdf import PdfReader
    from zipfile import ZipFile
    from test_workflow import add_signature
    c, h, _, app, rid, _, _ = legacy_fixture(setup)
    path = '/api/reports/'+rid
    # Each source field satisfied the former 8000-character limit. Their
    # concatenation is valid historical work, not newly oversized input.
    notes = {key: (key+' texto ') * 500 for key in ('FINAL-V', 'FINAL-N', 'CLEAN', 'GENERAL')}
    notes.update({key: (key+' texto ') * 500 for key in ('MAN-OLD-V', 'MAN-OLD-N', 'MAN-CUR-N')})
    assert all(len(text) <= 8000 for text in notes.values())
    with app.state.factory() as db:
        stored = db.get(Report, rid)
        payload = deepcopy(stored.payload)
        payload['answers']['FIN.03'] = {'value': notes['FINAL-V'], 'notes': notes['FINAL-N']}
        payload['answers']['EST.10']['notes'] = notes['CLEAN']
        payload['answers']['EST.18'] = {'value': '', 'notes': notes['GENERAL']}
        payload['answers']['MAN.21'] = {'value': notes['MAN-OLD-V'], 'notes': notes['MAN-OLD-N']}
        payload['answers']['MAN.22'] = {'value': '', 'notes': notes['MAN-CUR-N']}
        stored.payload = payload
        db.commit()
    projected = c.get(path, headers=h).json()
    final_text = notes['FINAL-V']+'\n'+notes['FINAL-N']
    equipment_text = notes['GENERAL']+'\n'+notes['CLEAN']
    assert len(final_text) > 8000 and len(equipment_text) > 8000
    assert projected['payload']['answers']['FIN.03']['notes'] == final_text
    maintenance_text = '\n'.join(notes[key] for key in ('MAN-CUR-N', 'MAN-OLD-V', 'MAN-OLD-N'))
    assert len(maintenance_text) == 24002
    assert projected['payload']['answers']['MAN.22']['notes'] == maintenance_text
    data = {'charger_id': projected['charger_id'], 'base_revision': projected['revision'],
            'checklist_version': 7, 'status': 'draft', 'payload': projected['payload']}
    saved = c.put(path, headers=h, json=data)
    assert saved.status_code == 200, saved.text
    assert c.get(path, headers=h).json()['payload'] == saved.json()['payload']
    data['payload']['answers']['MAN.21'] = {'value': 'NA', 'notes': 'Ensaio não aplicável'}
    add_signature(c, h, rid, data['payload'], revision=saved.json()['revision'])
    data.update(status='closed', base_revision=saved.json()['revision'])
    closed = c.put(path, headers=h, json=data)
    assert closed.status_code == 200, closed.text
    export = c.get(path+'/bundle', headers=h)
    assert export.status_code == 200, export.text[:100]
    with ZipFile(BytesIO(export.content)) as archive:
        document = json.loads(archive.read('report.json'))
        assert document['payload']['answers']['FIN.03']['notes'] == final_text
        assert document['payload']['answers']['EST.18']['notes'] == equipment_text
        assert document['payload']['answers']['MAN.22']['notes'] == maintenance_text
        pdf = archive.read(document['identifier']+'.pdf')
        (tmp_path/'merged-notes.pdf').write_bytes(pdf)
        # Qt can wrap at hyphens, including across a page boundary. Remove only
        # this fixture's repeated headers/footers before counting complete markers;
        # a raw substring count would mistake normal pagination for lost content.
        decoration = {'Lem · Relatório de manutenção PCVE',
                      'Lem · '+document['snapshot']['charger']['external_id']}
        lines = [line for page in PdfReader(BytesIO(pdf)).pages for line in page.extract_text().splitlines()
                 if line not in decoration and not re.fullmatch(r'Página \d+', line)]
        text = re.sub(r'\s+', '', ''.join(lines))
        for marker in notes:
            assert text.count(marker) == 500, f'PDF lost some {marker} text across page breaks'


def test_rejected_frozen_upload_can_reopen_as_current_draft_atomically(setup):
    c, h, _, app, rid, pids, png = legacy_fixture(setup)
    with app.state.factory() as db:
        stored = db.get(Report, rid)
        stored.template = {**stored.template, 'frozen_upload': True}
        db.commit()
    path = '/api/reports/'+rid
    frozen = c.get(path, headers=h).json()
    ordinary = deepcopy(frozen)
    ordinary['template'].pop('frozen_upload')
    upgraded = project(ordinary)
    data = {'charger_id': frozen['charger_id'], 'base_revision': frozen['revision'],
            'checklist_version': 7, 'status': 'draft', 'payload': upgraded['payload']}
    # Stale and malformed reopen requests must not even remove the marker.
    stale = c.put(path, headers=h, json={**data, 'base_revision': 99})
    assert stale.status_code == 409
    invalid = deepcopy(data); invalid['payload']['answers']['MAN.01'] = 123
    assert c.put(path, headers=h, json=invalid).status_code == 422
    assert c.get(path, headers=h).json() == frozen
    saved = c.put(path, headers=h, json=data)
    assert saved.status_code == 200, saved.text
    assert saved.json()['template']['version'] == 7
    assert 'frozen_upload' not in saved.json()['template']
    assert c.get(path, headers=h).json()['payload'] == upgraded['payload']
    assert {p['checkpoint'] for p in saved.json()['photos']} == {'MAN.22', 'EST.18'}
    for pid in pids:
        assert c.get(path+'/photos/'+pid, headers=h).content == png


@pytest.mark.parametrize('bad_answers', [[], {'FIN.03': {'value': 123, 'notes': 'Text'}},
    {'FIN.03': {'value': 'Text', 'notes': []}}, {'FIN.03': {'value': '', 'notes': 'x'*32001}}])
def test_projection_still_rejects_malformed_or_oversized_input(setup, bad_answers):
    from test_export_resources import make_report
    c, h, contract, _ = setup
    rid = make_report(c, h, contract)
    path = '/api/reports/'+rid
    before = c.get(path, headers=h).json()
    result = c.put(path, headers=h, json={'charger_id': before['charger_id'],
        'base_revision': before['revision'], 'checklist_version': 7, 'status': 'draft',
        'payload': {'answers': bad_answers}})
    assert result.status_code == 422, result.text
    assert c.get(path, headers=h).json() == before
