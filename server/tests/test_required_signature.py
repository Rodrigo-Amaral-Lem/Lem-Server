# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
import pytest
from test_workflow import setup
from test_rules_056 import report

@pytest.mark.parametrize("failure", ["missing", "name_changed", "image_missing"])
def test_signature_required_to_close(setup, failure):
    c,h,rid,body=report(setup)
    valid=deepcopy(body)
    if failure=="missing": body["payload"].pop("signatures")
    elif failure=="name_changed": body["payload"]["answers"]["FIN.02"]["value"]="Outro técnico"
    else: body["payload"]["signatures"]["FIN.02"]["photo_id"]="missing"
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==422 and ('assin' in response.text.lower()),response.text
    response=c.put('/api/reports/'+rid,headers=h,json=valid)
    assert response.status_code==200,response.text

def test_unsigned_draft_allowed(setup):
    c,h,rid,body=report(setup)
    body["status"]="draft";body["payload"].pop("signatures")
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==200,response.text
    item=next(i for g in response.json()["template"]["sections"] for i in g["items"] if i["id"]=="FIN.02")
    assert item["signature_required"]
