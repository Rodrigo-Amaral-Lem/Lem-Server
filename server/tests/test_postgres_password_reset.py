# SPDX-License-Identifier: GPL-3.0-only
"""Repeat password reset permissions/races against real PostgreSQL row locks."""
import pytest
from test_postgres_branding import postgres_setup
from test_password_reset import (
    test_reset_revokes_all_sessions_preserves_account_and_contracts,
    test_only_admin_can_reset_only_technicians,
    test_concurrent_resets_and_admin_targets,
)


@pytest.fixture
def setup(postgres_setup):
    return postgres_setup
