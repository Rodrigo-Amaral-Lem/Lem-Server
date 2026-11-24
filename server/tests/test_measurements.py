# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

from app.main import TEMPLATE
import json
import uuid
from pathlib import Path
from io import BytesIO
import pytest
from PIL import Image
from pypdf import PdfReader
from pypdf.generic import ContentStream
from app.measurements import format_value, outside
from test_workflow import setup, upload, answers, add_signature

CASES = json.loads((Path(__file__).resolve().parents[2] / "shared/measurement-cases.json").read_text())

@pytest.mark.parametrize("case", CASES)
def test_shared_measurement_rules(case):
    if case.get("invalid"):
        with pytest.raises(ValueError): format_value(case["id"], case["input"])
    else:
        assert format_value(case["id"], case["input"]) == case["formatted"]
        assert outside(case["id"], case["input"]) == case["nok"]

def draft(setup):
    c, h, contract, app = setup
    upload(c, h, contract["id"], True)
    charger = c.get(f'/api/contracts/{contract["id"]}/chargers', headers=h).json()[0]
    path = "/api/reports/" + str(uuid.uuid4())
    body = {"checklist_version": TEMPLATE["version"], "charger_id": charger["id"], "base_revision": 0, "payload": {"answers": answers()}}
    result = c.put(path, headers=h, json=body)
    assert result.status_code == 200, result.text
    from test_rules_056 import photo
    photo(c, h, path.rsplit('/', 1)[1], 'MAN.13')
    add_signature(c, h, path.rsplit('/', 1)[1], body["payload"])
    return c, h, path, {**body, "base_revision": 1, "status": "closed"}

@pytest.mark.parametrize("key,value", [("MAN.04","1000"),("MAN.07","230,123"),("MAN.20","100000"),("MAN.10","49,")])
def test_invalid_measurements_block_closure_without_changing_report(setup, key, value):
    c,h,path,body = draft(setup)
    body["payload"]["answers"][key]["value"] = value
    assert c.put(path, headers=h, json=body).status_code == 422
    saved = c.get(path,headers=h).json()
    assert saved["status"] == "draft" and saved["revision"] == 1

@pytest.mark.parametrize("key,value,formatted,nok", [("MAN.07","238.3","238,3 V",False),("MAN.04","359,99","359,99 V",True),("MAN.10","50","50 V",True),("MAN.20","4564,4","4564,4 Ω",False)])
def test_complete_measurements_export_with_units_and_nok(setup,key,value,formatted,nok):
    c,h,path,body = draft(setup)
    image = BytesIO(); Image.new("RGB",(8,8),"cyan").save(image,format="PNG")
    result=c.put(path+"/photos/"+str(uuid.uuid4()),headers=h,
        data={"checkpoint":key,"base_revision":"1"},files={"file":("x.png",image.getvalue(),"image/png")})
    assert result.status_code==200,result.text
    body["payload"]["answers"][key]["value"]=value
    result=c.put(path,headers=h,json=body)
    assert result.status_code==200,result.text
    assert result.json()["payload"]["answers"][key]["value"]==formatted
    pdf=c.get(path+"/pdf",headers=h)
    assert pdf.status_code==200,pdf.text
    reader=PdfReader(BytesIO(pdf.content))
    text="\n".join(p.extract_text(extraction_mode="layout") for p in reader.pages)
    assert formatted in text
    assert ("NOK" in text)==nok
    red=False
    for page in reader.pages:
        for operands,op in ContentStream(page.get_contents(),reader).operations:
            if op in (b"rg", b"scn") and len(operands)==3:
                red=red or all(abs(float(a)-b)<0.0001 for a,b in zip(operands,[161/255,38/255,38/255]))
    assert red==nok

def test_not_applicable_exempts_measurement_evidence_and_drafts_keep_partial_typing(setup):
    c,h,path,body=draft(setup)
    partial={**body,"status":"draft"}
    partial["payload"]["answers"]["MAN.07"]["value"]="238,"
    result=c.put(path,headers=h,json=partial)
    assert result.status_code==200,result.text
    assert result.json()["payload"]["answers"]["MAN.07"]["value"]=="238,"
    body["base_revision"]=2
    for key in [f"MAN.{i:02d}" for i in range(4,11)]+["MAN.20"]:
        body["payload"]["answers"][key]["value"]="Não Aplicavel"
    result=c.put(path,headers=h,json=body)
    assert result.status_code==200,result.text
    assert result.json()["payload"]["answers"]["MAN.07"]["value"]=="Não Aplicável"
