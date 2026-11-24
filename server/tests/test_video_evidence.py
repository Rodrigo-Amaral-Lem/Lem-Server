# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

from app.main import TEMPLATE
"""Real MP4 inspection and evidence workflow/export regressions."""
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile
from pypdf import PdfReader
from sqlalchemy import event
import shutil
import subprocess
import uuid
import pytest
from app.media import inspect_evidence
from app.images import InvalidImage
from app.exports import write_bundle
from app.pdf_report import build_pdf
from test_bug_regressions import _report, _jpeg
from test_workflow import setup, upload, answers, add_signature

@pytest.fixture
def video(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required (included in the production image)")
    target = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target)], check=True)
    return target.read_bytes()


def test_media_formats_and_limits(video, monkeypatch):
    assert inspect_evidence(video) == "video/mp4"
    assert inspect_evidence(_jpeg()) == "image/jpeg"
    for invalid in [b"", b"1234ftypisom" + b"\0" * 32, video[:40], b"x" * (20*1024*1024+1)]:
        with pytest.raises(InvalidImage): inspect_evidence(invalid)
    monkeypatch.setattr("app.media.subprocess.run", lambda *a, **k: SimpleNamespace(stdout=b'{"format":{"duration":"121","format_name":"mov,mp4"},"streams":[{"codec_type":"video","codec_name":"h264","width":64,"height":64}]}'))
    with pytest.raises(InvalidImage): inspect_evidence(video)


def test_video_export_keeps_original(video):
    photo = SimpleNamespace(id="clip", checkpoint="EST.01", mime="video/mp4", data=video)
    pdf = build_pdf(_report(), [photo])
    assert pdf.startswith(b"%PDF")
    target = BytesIO()
    write_bundle(target, {}, [photo], pdf)
    with ZipFile(target) as z:
        assert z.read("videos/EST.01/Carregador_Contrato_clip.mp4") == video


def test_video_upload_retry_delete_and_required_photo(setup, video):
    c, h, contract, app = setup
    upload(c, h, contract["id"], True)
    chargers = c.get(f'/api/contracts/{contract["id"]}/chargers', headers=h).json()
    result = c.put('/api/reports/' + str(uuid.uuid4()), headers=h, json={"checklist_version": TEMPLATE["version"], "charger_id":chargers[0]["id"],"base_revision":0,"status":"draft","payload":{"answers":{}}})
    assert result.status_code == 200, result.text
    report = result.json(); rid = report['id']; pid = str(uuid.uuid4())
    path = f'/api/reports/{rid}/photos/{pid}'
    data = {"checkpoint":"EST.01", "base_revision":str(report['revision'])}
    for _ in range(2):
        response = c.put(path, headers=h, data=data, files={"file":("clip.mp4",video,"video/mp4")})
        assert response.status_code == 200, response.text
    fetched = c.get(path, headers=h)
    assert fetched.content == video and fetched.headers['content-type'].startswith('video/mp4')
    current = c.get(f'/api/reports/{rid}',headers=h).json()
    assert current['photos'][0]['mime'] == 'video/mp4'
    payload = {"answers":answers()}
    add_signature(c, h, rid, payload, revision=current['revision'])
    payload["answers"]["EST.01"] = {"value":"OK","notes":""}
    closed = c.put(f'/api/reports/{rid}', headers=h,
                   json={"checklist_version": TEMPLATE["version"], "charger_id":chargers[0]["id"],"base_revision":current['revision'],"status":"closed","payload":payload})
    assert closed.status_code == 422, closed.text
    assert 'EST.01: falta fotografia' in closed.text
    removed = c.delete(path,headers=h,params={"base_revision":current['revision']})
    assert removed.status_code == 200, removed.text

    # Re-add both types and close: the real PDF/ZIP endpoints must handle mixed media.
    revision = removed.json()["revision"]
    data["base_revision"] = str(revision)
    assert c.put(path, headers=h, data=data, files={"file":("clip.mp4",video)}).status_code == 200
    trip_data=dict(data,checkpoint='MAN.13')
    trip_path=f'/api/reports/{rid}/photos/{uuid.uuid4()}'
    assert c.put(trip_path,headers=h,data=trip_data,files={'file':('trip.jpg',_jpeg())}).status_code==200
    picture_path = f'/api/reports/{rid}/photos/{uuid.uuid4()}'
    assert c.put(picture_path, headers=h, data=data, files={"file":("photo.jpg",_jpeg())}).status_code == 200
    closed = c.put(f'/api/reports/{rid}', headers=h,
                   json={"checklist_version": TEMPLATE["version"], "charger_id":chargers[0]["id"],"base_revision":revision,"status":"closed","payload":payload})
    assert closed.status_code == 200, closed.text
    video_reads = []
    engine = app.state.factory.kw["bind"]

    def record_video_reads(_connection, _cursor, statement, parameters, _context, _executemany):
        if "SELECT photos.data" in statement and pid in parameters:
            video_reads.append(pid)

    event.listen(engine, "before_cursor_execute", record_video_reads)
    try:
        pdf = c.get(f'/api/reports/{rid}/pdf', headers=h)
        assert pdf.status_code == 200, pdf.text
        text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf.content)).pages)
        assert "Vídeo (ficheiro incluído na exportação ZIP)" in text
        assert not video_reads
        bundle = c.get(f'/api/reports/{rid}/bundle', headers=h)
        assert video_reads == [pid]
    finally:
        event.remove(engine, "before_cursor_execute", record_video_reads)
    assert bundle.status_code == 200, bundle.text
    with ZipFile(BytesIO(bundle.content)) as z:
        assert z.read(f'videos/EST.01/{report["identifier"]}_{pid}.mp4') == video
    assert c.delete(path,headers=h,params={"base_revision":closed.json()["revision"]}).status_code == 409

    # A desktop correction inherits this mixed-media report. Remove one cloned
    # attachment by ID, then verify the original video bytes in the replacement.
    from test_report_corrections import start, body
    admin = {**h, 'X-Lem-Client-Platform': 'windows'}
    draft = start((c, admin, contract, app, closed.json()))
    mapping = draft['correction']['photo_map']
    removed = mapping[trip_path.rsplit('/', 1)[1]]
    base = f'/api/reports/{draft["id"]}'
    result = c.delete(f'{base}/photos/{removed}', headers=admin, params={'base_revision': draft['revision']})
    assert result.status_code == 200, result.text
    corrected = body(draft)
    corrected['base_revision'] = result.json()['revision']
    result = c.post(base + '/correction/commit', headers=admin, json=corrected)
    assert result.status_code == 200, result.text
    assert {p['id'] for p in result.json()['photos']} == set(mapping.values()) - {removed}
    bundle = c.get(base + '/bundle', headers=admin)
    assert bundle.status_code == 200, bundle.text
    with ZipFile(BytesIO(bundle.content)) as archive:
        assert archive.read(f'videos/EST.01/{report["identifier"]}_{mapping[pid]}.mp4') == video
        assert not any(removed in member for member in archive.namelist())

@pytest.mark.parametrize('duration,accepted', [(60, True), (120, True), (120.5, True), (120.501, False), (0, False)])
def test_new_duration_boundary(monkeypatch, duration, accepted):
    import json
    probe = {'format': {'duration': str(duration), 'format_name': 'mov,mp4'},
             'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'width': 64, 'height': 64}]}
    monkeypatch.setattr('app.media.subprocess.run', lambda *a, **k: SimpleNamespace(stdout=json.dumps(probe).encode()))
    data = b'1234ftypisom' + b'\0' * 32
    if accepted:
        assert inspect_evidence(data) == 'video/mp4'
    else:
        with pytest.raises(InvalidImage): inspect_evidence(data)


def test_new_video_size_boundary_and_photo_cap(monkeypatch):
    probe = b'{"format":{"duration":"120","format_name":"mov,mp4"},"streams":[{"codec_type":"video","codec_name":"h264","width":64,"height":64}]}'
    monkeypatch.setattr('app.media.subprocess.run', lambda *a, **k: SimpleNamespace(stdout=probe))
    data = b'1234ftypisom' + b'\0' * (60 * 1024 * 1024 - 12)
    assert inspect_evidence(data) == 'video/mp4'
    with pytest.raises(InvalidImage): inspect_evidence(data + b'x')
    with pytest.raises(InvalidImage): inspect_evidence(b'x' * (20 * 1024 * 1024 + 1))
