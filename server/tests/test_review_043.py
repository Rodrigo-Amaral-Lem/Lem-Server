# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Concrete contract validation regressions identified during the source review."""
import pytest
from test_workflow import setup


@pytest.mark.parametrize("field", ["company", "kind"])
@pytest.mark.parametrize("blank", [" ", "\t\n", "\u00a0"])
def test_whitespace_contract_rejected_without_creating_record(setup, field, blank):
    client, headers, contract, app = setup
    before = client.get("/api/contracts", headers=headers).json()
    body = {"company": "Company", "year": 2026, "semester": 1, "kind": "TRE"}
    body[field] = blank
    assert client.post("/api/contracts", headers=headers, json=body).status_code == 422
    assert client.get("/api/contracts", headers=headers).json() == before


def test_contract_text_trimmed_before_storage(setup):
    client, headers, contract, app = setup
    result = client.post("/api/contracts", headers=headers, json={
        "company": "  Review client  ", "year": 2026, "semester": 2, "kind": " TRE "})
    assert result.status_code == 200, result.text
    assert result.json()["name"] == "2026_2ºS_Review client_TRE"
