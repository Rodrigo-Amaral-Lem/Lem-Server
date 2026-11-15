# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

from app.main import TEMPLATE
from test_workflow import setup
from test_rules_056 import report

import pytest

@pytest.mark.parametrize('key', [f'SEG.{n:02d}' for n in range(2,7)])
@pytest.mark.parametrize('value', ['NOK', 'NA'])
def test_safety_required_observation(setup, key, value):
    c,h,rid,body=report(setup)
    body['payload']['answers'][key]={'value':value,'notes':'   '}
    required = value == 'NOK' or key in ('SEG.02','SEG.04')
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code == (422 if required else 200), response.text
    if required:
        assert key in response.text and 'observação' in response.text
        body['payload']['answers'][key]['notes']='Motivo registado pelo técnico'
        response=c.put('/api/reports/'+rid,headers=h,json=body)
        assert response.status_code==200,response.text


def test_safety_notes_only_can_close(setup):
    c,h,rid,body=report(setup)
    body['payload']['answers']['SEG.07']={'value':'','notes':'Acesso condicionado durante o trabalho'}
    response=c.put('/api/reports/'+rid,headers=h,json=body)
    assert response.status_code==200,response.text
