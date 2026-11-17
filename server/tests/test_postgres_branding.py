# SPDX-License-Identifier: GPL-3.0-only
"""Opt-in real PostgreSQL/psycopg regressions in isolated disposable schemas.

LEM_TEST_POSTGRES_URL must name a loopback-only lem_branding_test database.
Every case creates and drops only its own random schema, never shared tables.
"""
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from app.main import create_app
import test_report_styles as cases


@pytest.fixture
def postgres_setup(monkeypatch):
    configured = os.getenv('LEM_TEST_POSTGRES_URL')
    if not configured:
        pytest.skip('Set LEM_TEST_POSTGRES_URL for isolated PostgreSQL branding checks')
    url = make_url(configured)
    assert url.drivername == 'postgresql+psycopg'
    assert url.host in ('localhost', '127.0.0.1', '::1') and url.database == 'lem_branding_test'
    schema = 'lem_test_' + uuid.uuid4().hex
    control = create_engine(url)
    with control.begin() as connection:
        connection.execute(CreateSchema(schema))
    scoped = url.update_query_dict({'options': '-csearch_path=' + schema})
    monkeypatch.setenv('ADMIN_PASSWORD', 'StrongPassword123!')
    app = None
    try:
        app = create_app(scoped.render_as_string(hide_password=False))
        with TestClient(app, headers={'X-Lem-Client-Version': '0.2.6'}) as client:
            token = client.post('/api/login', json={'email': 'admin@lem.pt', 'password': 'StrongPassword123!'}).json()['token']
            headers = {'Authorization': 'Bearer ' + token}
            contract = client.post('/api/contracts', headers=headers,
                json={'company': 'Galp', 'year': 2026, 'semester': 1, 'kind': 'TRE'}).json()
            yield client, headers, contract, app
    finally:
        if app is not None:
            app.state.factory.kw['bind'].dispose()
        with control.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        control.dispose()


@pytest.mark.parametrize('case', [
    cases.test_saved_format_survives_new_session_and_server_restart,
    cases.test_admin_exports_legacy_technician_report_with_current_contract_brand,
    cases.test_import_and_existing_closed_report_use_live_presentation_without_changing_content,
    cases.test_only_administrators_and_compare_revision_protect_settings,
    cases.test_lem_to_csmtec_changes_existing_and_new_exports,
], ids=lambda case: case.__name__)
def test_postgres_contract_presentation(postgres_setup, case):
    case(postgres_setup)
