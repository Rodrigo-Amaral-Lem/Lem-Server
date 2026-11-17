"""Optional import metadata must not relax report identity requirements at closure."""
import copy
import json
import uuid
from io import BytesIO

import openpyxl
import pytest

from app.importer import analyse, parse
from app.import_profiles import STATIONS_COLUMNS
from app.main import TEMPLATE
from test_workflow import setup, answers, add_signature

OPTIONAL = {'serial', 'model', 'energy_supply', 'power', 'power_limit'}
REQUIRED = {'external_id', 'address', 'region', 'manufacturer', 'connectors'}


def workbook(empty=OPTIONAL):
    values = {'external_id': 'OPTIONAL-001', 'address': 'Rua de teste', 'region': 'Norte',
              'manufacturer': 'Fabricante de teste', 'connectors': 'CCS2', 'serial': '000012',
              'model': 'Modelo X', 'energy_supply': 'BT', 'power': '60', 'power_limit': '0'}
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = 'Stations'
    sheet.append([label for _, _, label, _ in STATIONS_COLUMNS])
    sheet.append(['' if field in empty else values.get(field, '') for _, field, _, _ in STATIONS_COLUMNS])
    output = BytesIO()
    book.save(output)
    return output.getvalue()


@pytest.mark.parametrize('empty', [{field} for field in sorted(OPTIONAL)] + [OPTIONAL])
def test_optional_cells_and_recommended_mapping(empty):
    data = workbook(empty)
    mapping = analyse(data)['sheets'][0]['recommended_mapping']
    assert set(mapping['required_fields']) == REQUIRED
    # Older saved profiles may still advertise these as required: current server rules prevail.
    mapping['required_fields'] = list(REQUIRED | OPTIONAL)
    result = parse(data, mapping)
    assert result['errors'] == [] and result['count'] == 1
    assert all(result['records'][0]['details'][field] == '' for field in empty)
    if 'serial' not in empty:
        assert result['records'][0]['details']['serial'] == '000012'
    if 'power_limit' not in empty:
        assert result['records'][0]['details']['power_limit'] == '0'


@pytest.mark.parametrize('field', sorted(REQUIRED))
def test_remaining_required_cells(field):
    result = parse(workbook(OPTIONAL | {field}), {'sheet': 'Stations'})
    assert result['count'] == 0
    assert any(error.get('field') == field for error in result['errors'])


def test_import_empty_metadata_draft_and_complete_identity_before_closure(setup):
    client, headers, contract, _ = setup
    cid = contract['id']
    url = f'/api/contracts/{cid}/import'
    for commit in ('false', 'true'):
        response = client.post(url, headers=headers, files={'file': ('stations.xlsx', workbook())},
                               data={'mapping': json.dumps({'sheet': 'Stations'}), 'commit': commit})
        assert response.status_code == 200, response.text
        if commit == 'false':
            assert response.json()['count'] == 1 and response.json()['errors'] == []
            assert client.get(f'/api/contracts/{cid}/chargers', headers=headers).json() == []
    charger = client.get(f'/api/contracts/{cid}/chargers', headers=headers).json()[0]
    assert all(charger['details'][field] == '' for field in OPTIONAL)
    rid = str(uuid.uuid4())
    payload = {'answers': answers()}
    payload['answers']['EST.03'] = {'value': '', 'notes': ''}
    payload['answers']['EST.04'] = {'value': '', 'notes': ''}
    body = {'charger_id': charger['id'], 'checklist_version': TEMPLATE['version'],
            'base_revision': 0, 'status': 'draft', 'payload': payload}
    draft = client.put(f'/api/reports/{rid}', headers=headers, json=body)
    assert draft.status_code == 200, draft.text
    body['base_revision'] = draft.json()['revision']
    add_signature(client, headers, rid, payload, body['base_revision'])
    body['status'] = 'closed'
    for key in ('EST.03', 'EST.04'):
        for empty in ('', '   '):
            attempt = copy.deepcopy(body)
            attempt['payload']['answers']['EST.03']['value'] = 'Modelo completo X'
            attempt['payload']['answers']['EST.04']['value'] = '000012'
            attempt['payload']['answers'][key]['value'] = empty
            response = client.put(f'/api/reports/{rid}', headers=headers, json=attempt)
            assert response.status_code == 422, response.text
            assert key + ': falta resposta' in response.json()['detail']['items']
            assert client.get(f'/api/reports/{rid}', headers=headers).json()['status'] == 'draft'
            current = client.get(f'/api/contracts/{cid}/chargers', headers=headers).json()[0]
            assert current['details']['model'] == current['details']['serial'] == ''
    payload['answers']['EST.03']['value'] = 'Modelo completo X'
    payload['answers']['EST.04']['value'] = '000012'
    response = client.put(f'/api/reports/{rid}', headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'closed'
    current = client.get(f'/api/contracts/{cid}/chargers', headers=headers).json()[0]['details']
    assert current['model'] == 'Modelo completo X' and current['serial'] == '000012'
    assert current['energy_supply'] == current['power'] == current['power_limit'] == ''
