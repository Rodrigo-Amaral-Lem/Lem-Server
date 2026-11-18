# SPDX-License-Identifier: GPL-3.0-only
"""A closed-report correction and the shared station identity commit together."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event

from app.models import Charger, Report, ReportCorrection, migrate_correction_identity
from test_workflow import setup, upload, xlsx, answers, add_signature
from test_report_corrections import correction_case, start, body
from test_stations_055 import draft as new_draft, close as close_report


IDENTITY = {'EST.02': 'Fabricante corrigido', 'EST.03': 'Modelo corrigido', 'EST.04': 'SN-CORRIGIDO'}
FIELDS = {'EST.02': 'manufacturer', 'EST.03': 'model', 'EST.04': 'serial'}


def edited_body(report, values=None):
    data = body(report)
    for checkpoint, value in (IDENTITY if values is None else values).items():
        data['payload']['answers'][checkpoint] = {'value': value, 'notes': ''}
    return data


def charger_row(case, contract=None):
    c, headers, initial_contract, _, original = case
    rows = c.get(f'/api/contracts/{(contract or initial_contract)["id"]}/chargers', headers=headers).json()
    external_id = original['snapshot']['charger']['external_id']
    return next(row for row in rows if row['external_id'] == external_id)


def commit(case, report, data):
    c, headers, *_ = case
    return c.post(f'/api/reports/{report["id"]}/correction/commit', headers=headers, json=data)


def review_migrated_form(case, working):
    """Old answers are historical: explicitly review/sign the current questions."""
    c, headers, *_ = case
    assert working['template']['version'] == 7
    assert working['template']['sections'][-1]['id'] == 'ANT'
    working['payload']['answers'].update(answers())
    add_signature(c, headers, working['id'], working['payload'], revision=working['revision'])


@pytest.mark.parametrize('checkpoint', IDENTITY)
def test_second_admin_corrects_identity_on_a_historical_report(correction_case, checkpoint):
    c, headers, _, _, original = correction_case
    # The catalogue has moved on before the administrator opens this correction.
    # The later report must retain the identity it recorded.
    later_id, later_body, _ = new_draft(c, headers, charger_row(correction_case))
    previous = 'Valor confirmado numa intervenção anterior'
    later = close_report(c, headers, later_id, later_body, **{checkpoint: previous})
    assert later.status_code == 200, later.text
    field = FIELDS[checkpoint]
    assert charger_row(correction_case)['details'][field] == previous
    original_url = f'/api/reports/{original["id"]}'
    signature_id = original['payload']['signatures']['FIN.02']['photo_id']
    signature = c.get(f'{original_url}/photos/{signature_id}', headers=headers)
    assert signature.status_code == 200

    credentials = {'email': 'identity-admin@example.test', 'password': 'OtherPassword123!'}
    created = c.post('/api/users', headers=headers, json={**credentials, 'name': 'Outro administrador', 'role': 'admin'})
    assert created.status_code == 200, created.text
    login = c.post('/api/login', json=credentials)
    assert login.status_code == 200, login.text
    editor = {**headers, 'Authorization': 'Bearer ' + login.json()['token']}
    case = (c, editor, *correction_case[2:])
    working = start(case)
    assert working['owner_id'] == created.json()['id'] != original['owner_id']
    assert working['snapshot'] == original['snapshot']
    data = edited_body(working, {checkpoint: IDENTITY[checkpoint]})
    staged = c.put(f'/api/reports/{working["id"]}', headers=editor, json={**data, 'status': 'draft'})
    assert staged.status_code == 200, staged.text
    assert charger_row(case)['details'][field] == previous
    assert c.get(original_url, headers=editor).json() == original

    data['base_revision'] = staged.json()['revision']
    result = commit(case, working, data)
    assert result.status_code == 200, result.text
    saved = result.json()
    assert saved['status'] == 'closed' and saved['owner_id'] == original['owner_id']
    assert saved['snapshot']['charger'][field] == IDENTITY[checkpoint]
    assert saved['payload']['answers'] == data['payload']['answers']
    assert charger_row(case)['details'][field] == IDENTITY[checkpoint]
    assert commit(case, working, data).json() == saved
    gone = c.get(original_url, headers=editor)
    assert gone.status_code == 410 and gone.json()['detail']['replacement_id'] == saved['id']
    copied_id = saved['payload']['signatures']['FIN.02']['photo_id']
    copied = c.get(f'/api/reports/{saved["id"]}/photos/{copied_id}', headers=editor)
    assert copied.status_code == 200 and copied.content == signature.content
    assert c.get(f'/api/reports/{later_id}', headers=headers).json()['snapshot'] == later.json()['snapshot']


@pytest.mark.parametrize('changed_after_open', [False, True])
def test_upgrade_preserves_pending_corrections_and_checks_later_station_writes(correction_case, changed_after_open):
    c, headers, _, app, original = correction_case
    checkpoint = 'EST.03'
    previous_model = 'Modelo registado antes de abrir a correção'
    later_id, later_body, _ = new_draft(c, headers, charger_row(correction_case))
    later = close_report(c, headers, later_id, later_body, **{checkpoint: previous_model})
    assert later.status_code == 200, later.text
    working = start(correction_case)
    current_model = previous_model
    if changed_after_open:
        current_model = 'Modelo registado durante a correção'
        another_id, another_body, _ = new_draft(c, headers, charger_row(correction_case))
        intervening = close_report(c, headers, another_id, another_body, **{checkpoint: current_model})
        assert intervening.status_code == 200, intervening.text

    working_url = f'/api/reports/{working["id"]}'
    original_url = f'/api/reports/{original["id"]}'
    data = edited_body(working, {checkpoint: IDENTITY[checkpoint]})
    staged = c.put(working_url, headers=headers, json={**data, 'status': 'draft'})
    assert staged.status_code == 200, staged.text
    data['base_revision'] = staged.json()['revision']
    engine = app.state.factory.kw['bind']
    # Recreate the previous schema on this disposable fixture before upgrading.
    with engine.begin() as connection:
        connection.exec_driver_sql('ALTER TABLE report_corrections DROP COLUMN identity_baseline')
    migrate_correction_identity(engine)
    migrate_correction_identity(engine)
    with app.state.factory() as db:
        assert db.get(ReportCorrection, working['id']).identity_baseline is None
    assert c.get(working_url, headers=headers).json() == staged.json()
    assert c.get(original_url, headers=headers).json() == original

    result = commit(correction_case, working, data)
    assert result.status_code == (409 if changed_after_open else 200), result.text
    if changed_after_open:
        assert result.json()['detail']['reason'] == 'station_identity_conflict'
        assert c.get(working_url, headers=headers).json() == staged.json()
        assert c.get(original_url, headers=headers).json() == original
        assert charger_row(correction_case)['details']['model'] == current_model
    else:
        assert result.json()['photos'] == staged.json()['photos']
        assert c.get(original_url, headers=headers).status_code == 410
        assert charger_row(correction_case)['details']['model'] == IDENTITY[checkpoint]


@pytest.mark.parametrize('platform,legacy', [('windows', False), ('linux', False), ('windows', True)])
def test_correction_updates_shared_station_and_preserves_history(correction_case, platform, legacy):
    c, headers, contract, app, original = correction_case
    headers['X-Lem-Client-Platform'] = platform
    if legacy:
        # Simulate a stored report predating automatic station updates. Its old
        # fields are archived; the current identity must be reviewed explicitly.
        with app.state.factory() as db:
            report = db.get(Report, original['id'])
            report.template = {**report.template, 'version': 3}
            db.commit()
        original.update(c.get(f'/api/reports/{original["id"]}', headers=headers).json())
    initial = charger_row(correction_case)
    other = c.post('/api/contracts', headers=headers,
                   json={'company': 'Galp', 'year': 2026, 'semester': 2, 'kind': 'TRE'}).json()
    workbook = xlsx((initial['external_id'],))
    assert upload(c, headers, other['id'], True, workbook).status_code == 200
    historical_id, historical_body, _ = new_draft(c, headers, charger_row(correction_case, other))
    historical = close_report(c, headers, historical_id, historical_body).json()

    working = start(correction_case)
    if legacy:
        review_migrated_form(correction_case, working)
    saved = commit(correction_case, working, edited_body(working))
    assert saved.status_code == 200, saved.text
    for membership in (contract, other):
        row = charger_row(correction_case, membership)
        assert row['external_id'] == initial['external_id']
        assert row['station_id'] == initial['station_id']
        for checkpoint, field in FIELDS.items():
            assert row['details'][field] == IDENTITY[checkpoint]
            assert saved.json()['snapshot']['charger'][field] == IDENTITY[checkpoint]
    assert saved.json()['station_update']['details'] == row['details']
    assert saved.json()['owner_id'] == original['owner_id']
    assert c.get(f'/api/reports/{historical_id}', headers=headers).json()['snapshot'] == historical['snapshot']
    # Reimporting the original workbook must not undo the verified correction.
    assert upload(c, headers, other['id'], True, workbook).status_code == 200
    assert charger_row(correction_case, other)['details']['serial'] == IDENTITY['EST.04']
    with app.state.factory() as db:
        station = db.get(Charger, working['charger_id']).station
        assert all(station.verified_fields[field]['report_id'] == working['id'] for field in FIELDS.values())


def test_staged_or_cancelled_identity_edit_does_not_change_station(correction_case):
    c, headers, _, _, original = correction_case
    before = charger_row(correction_case)['details']
    working = start(correction_case)
    data = edited_body(working)
    data['status'] = 'draft'
    staged = c.put(f'/api/reports/{working["id"]}', headers=headers, json=data)
    assert staged.status_code == 200, staged.text
    assert charger_row(correction_case)['details'] == before
    assert c.delete(f'/api/reports/{working["id"]}?base_revision={staged.json()["revision"]}', headers=headers).status_code == 200
    assert charger_row(correction_case)['details'] == before
    assert c.get(f'/api/reports/{original["id"]}', headers=headers).json() == original


def test_untouched_legacy_identity_answers_do_not_change_station(correction_case):
    c, headers, _, app, original = correction_case
    before = charger_row(correction_case)['details']
    with app.state.factory() as db:
        report = db.get(Report, original['id'])
        report.template = {**report.template, 'version': 3}
        payload = deepcopy(report.payload)
        payload['answers']['EST.03'] = {'value': 'Modelo histórico não publicado', 'notes': ''}
        report.payload = payload
        db.commit()
    original.update(c.get(f'/api/reports/{original["id"]}', headers=headers).json())
    working = start(correction_case)
    review_migrated_form(correction_case, working)
    assert any(answer.get('notes') == 'Modelo histórico não publicado'
               for answer in working['payload']['answers'].values())
    result = commit(correction_case, working, body(working))
    assert result.status_code == 200, result.text
    assert charger_row(correction_case)['details'] == before
    assert result.json()['snapshot'] == original['snapshot']


def test_competing_identity_edits_commit_only_the_winners_fields(correction_case):
    c, headers, *_ = correction_case
    working = [start(correction_case), start(correction_case)]
    values = [{key: value + suffix for key, value in IDENTITY.items()} for suffix in (' A', ' B')]
    data = [edited_body(report, changes) for report, changes in zip(working, values)]
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(commit, correction_case, report, payload)
                   for report, payload in zip(working, data)]
        results = [future.result() for future in futures]
    assert sorted(result.status_code for result in results) == [200, 409], [r.text for r in results]
    winner = next(index for index, result in enumerate(results) if result.status_code == 200)
    current = charger_row(correction_case)['details']
    for checkpoint, field in FIELDS.items():
        assert current[field] == values[winner][checkpoint]
    loser = working[1 - winner]
    preserved = c.get(f'/api/reports/{loser["id"]}', headers=headers).json()
    assert preserved['status'] == 'draft' and preserved['photos'] == loser['photos']


@pytest.mark.parametrize('failure', ['validation', 'storage'])
def test_failed_correction_rolls_back_identity_and_both_reports(correction_case, failure):
    c, headers, _, app, original = correction_case
    before = charger_row(correction_case)['details']
    working = start(correction_case)
    data = edited_body(working)
    engine = app.state.factory.kw['bind']

    def fail_after_replacement(connection, cursor, statement, params, context, many):
        if statement.lstrip().lower().startswith('insert into deleted_reports'):
            raise OSError('simulated disk failure after station update and original deletion')

    if failure == 'validation':
        data['payload']['answers'].pop('EST.03')
        assert commit(correction_case, working, data).status_code == 422
    else:
        event.listen(engine, 'before_cursor_execute', fail_after_replacement)
        try:
            with pytest.raises(OSError):
                commit(correction_case, working, data)
        finally:
            event.remove(engine, 'before_cursor_execute', fail_after_replacement)
    assert charger_row(correction_case)['details'] == before
    assert c.get(f'/api/reports/{original["id"]}', headers=headers).json() == original
    assert c.get(f'/api/reports/{working["id"]}', headers=headers).json() == working
    with app.state.factory() as db:
        assert not db.get(Charger, working['charger_id']).station.verified_fields


@pytest.mark.parametrize('change', ['conflict', 'unrelated', 'same_value'])
def test_newer_intervention_is_never_silently_overwritten(correction_case, change):
    c, headers, _, _, original = correction_case
    working = start(correction_case)
    next_id, next_body, _ = new_draft(c, headers, charger_row(correction_case))
    newer_model = IDENTITY['EST.03'] if change == 'same_value' else 'Modelo confirmado entretanto'
    response = close_report(c, headers, next_id, next_body, **{'EST.03': newer_model})
    assert response.status_code == 200, response.text
    # Replaying an opening request (e.g. after a lost reply) cannot move its
    # baseline forward and hide a concurrent catalogue change.
    assert start(correction_case, new_id=working['id'])['id'] == working['id']
    values = {'EST.04': IDENTITY['EST.04']} if change == 'unrelated' else IDENTITY
    result = commit(correction_case, working, edited_body(working, values))
    assert result.status_code == (409 if change == 'conflict' else 200), result.text
    current = charger_row(correction_case)['details']
    assert current['model'] == newer_model
    if change == 'conflict':
        assert result.json()['detail']['reason'] == 'station_identity_conflict'
        assert current.get('manufacturer') == original['snapshot']['charger'].get('manufacturer')
        assert current['serial'] == original['snapshot']['charger']['serial']
        assert c.get(f'/api/reports/{original["id"]}', headers=headers).status_code == 200
        assert c.get(f'/api/reports/{working["id"]}', headers=headers).json()['status'] == 'draft'
    else:
        assert current['serial'] == IDENTITY['EST.04']
        # A subsequent intervention wins over retries of an already committed copy.
        later_id, later_body, _ = new_draft(c, headers, charger_row(correction_case))
        assert close_report(c, headers, later_id, later_body, **{'EST.03': 'Modelo posterior'}).status_code == 200
        retry = commit(correction_case, working, edited_body(working, values))
        assert retry.status_code == 200, retry.text
        assert retry.json()['station_update']['details']['model'] == 'Modelo posterior'
        recovered = c.get(f'/api/reports/{working["id"]}', headers=headers).json()
        assert recovered['station_update'] == retry.json()['station_update']
        assert recovered['snapshot'] == result.json()['snapshot']
