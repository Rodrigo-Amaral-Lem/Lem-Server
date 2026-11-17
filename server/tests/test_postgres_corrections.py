# SPDX-License-Identifier: GPL-3.0-only
"""Run correction identity and replacement races against real PostgreSQL locks.

Uses the same loopback-only disposable-schema fixture as the branding checks.
No production URL is accepted; unset LEM_TEST_POSTGRES_URL skips these cases.
"""
import pytest

from test_postgres_branding import postgres_setup
from test_report_corrections import (
    correction_case,
    test_two_concurrent_corrections_only_one_replaces_and_loser_survives,
    test_cancel_racing_commit_never_removes_both_reports,
)
from test_correction_identity import (
    test_correction_updates_shared_station_and_preserves_history,
    test_staged_or_cancelled_identity_edit_does_not_change_station,
    test_untouched_legacy_identity_answers_do_not_change_station,
    test_competing_identity_edits_commit_only_the_winners_fields,
    test_failed_correction_rolls_back_identity_and_both_reports,
    test_newer_intervention_is_never_silently_overwritten,
)


@pytest.fixture
def setup(postgres_setup):
    # Reuse the API scenarios unchanged: SQLite cannot validate FOR UPDATE races.
    return postgres_setup


def test_correction_uses_same_lock_order_as_ordinary_closure(correction_case):
    """A concurrent catalogue writer must not create a charger/catalogue cycle."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from sqlalchemy import event, select
    from app.models import Charger
    from app.stations import lock_catalogue
    from test_report_corrections import start
    from test_correction_identity import edited_body

    c, h, _, app, _ = correction_case
    working = start(correction_case)
    engine = app.state.factory.kw['bind']
    catalogue_requested = Event()

    def track_request(connection, cursor, statement, parameters, context, many):
        if 'station_catalogue_lock' in statement.lower() and 'FOR UPDATE' in statement:
            catalogue_requested.set()

    with app.state.factory() as writer:
        # Simulate the first lock taken by ordinary closure/import/console edits.
        lock_catalogue(writer)
        event.listen(engine, 'before_cursor_execute', track_request)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(c.post, f'/api/reports/{working["id"]}/correction/commit',
                                     headers=h, json=edited_body(working))
                try:
                    assert catalogue_requested.wait(10), 'Correction never requested the catalogue lock'
                    # The correction is waiting for us. It must not already hold
                    # this later lock, or two valid operations can deadlock.
                    writer.scalar(select(Charger).where(Charger.id == working['charger_id'])
                                  .with_for_update(nowait=True))
                finally:
                    writer.rollback()
                response = future.result(timeout=15)
                assert response.status_code == 200, response.text
        finally:
            event.remove(engine, 'before_cursor_execute', track_request)
