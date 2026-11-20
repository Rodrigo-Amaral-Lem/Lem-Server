# SPDX-License-Identifier: GPL-3.0-only
"""Real HTTP clients keep original downloads open while an admin syncs a correction.

The sender is paused after headers, rather than sleeping in a database query:
this reproduces a slow/disconnected device without depending on TCP buffer sizes.
PostgreSQL cases use the disposable loopback-only schema fixture.
"""
import asyncio
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
import socket
import time
import importlib
import uuid

import httpx
import pytest
import uvicorn

from app.main import TEMPLATE
from test_workflow import setup as sqlite_setup, upload, answers, add_signature
from test_postgres_branding import postgres_setup
from test_report_corrections import start, body


@pytest.fixture(params=['sqlite', 'postgres'])
def setup(request):
    return request.getfixturevalue('postgres_setup' if request.param == 'postgres' else 'sqlite_setup')


@pytest.fixture
def technician(setup):
    c, h, contract, _ = setup
    account = {'email': 'download-tech@example.test', 'name': 'Download technician',
               'role': 'tech', 'password': 'TestDownloadPassword123!'}
    created = c.post('/api/users', headers=h, json=account)
    assert created.status_code == 200, created.text
    assert c.put(f'/api/contracts/{contract["id"]}/users/{created.json()["id"]}', headers=h).status_code == 200
    login = c.post('/api/login', json={'email': account['email'], 'password': account['password']})
    assert login.status_code == 200, login.text
    return {'Authorization': 'Bearer '+login.json()['token'], 'X-Lem-Client-Version': '0.5.4'}


@pytest.fixture
def correction_case(setup, technician):
    c, h, contract, app = setup
    assert upload(c, h, contract['id'], True).status_code == 200
    charger = c.get(f'/api/contracts/{contract["id"]}/chargers', headers=technician).json()[0]
    rid = str(uuid.uuid4())
    payload = {'answers': answers()}
    data = {'charger_id': charger['id'], 'base_revision': 0, 'checklist_version': TEMPLATE['version'],
            'status': 'draft', 'payload': payload}
    saved = c.put(f'/api/reports/{rid}', headers=technician, json=data)
    assert saved.status_code == 200, saved.text
    add_signature(c, technician, rid, payload)
    saved = c.put(f'/api/reports/{rid}', headers=technician,
                  json={**data, 'base_revision': 1, 'status': 'closed'})
    assert saved.status_code == 200, saved.text
    h = {**h, 'X-Lem-Client-Platform': 'windows'}
    original = c.get(f'/api/reports/{rid}', headers=h).json()
    return c, h, contract, app, original


@contextmanager
def held_download_server(app, count):
    entered = [Event() for _ in range(count)]
    release = Event()

    async def slow_sender(scope, receive, send):
        slot = dict(scope.get('headers', [])).get(b'x-test-download-slot')

        async def held_send(message):
            if slot is not None and message['type'] == 'http.response.body':
                entered[int(slot)].set()
                # Only the wire is held; the API, thread pool and other clients
                # run normally on this same uvicorn event loop.
                while not release.is_set():
                    await asyncio.sleep(.01)
            await send(message)

        await app(scope, receive, held_send)

    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    address = f'http://127.0.0.1:{listener.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(slow_sender, log_level='error', lifespan='off'))
    thread = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(.02)
        assert server.started
        yield address, entered, release
    finally:
        release.set()
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()


@pytest.mark.parametrize('devices', [1, 8])
def test_sync_correction_while_original_downloads_are_held(correction_case, technician, devices):
    c, h, contract, app, original = correction_case
    working = start(correction_case)
    rid = working['id']
    photo = original['photos'][0]
    path = f'/api/reports/{original["id"]}/photos/{photo["id"]}'
    expected = c.get(path, headers=h).content
    headers = {**h, 'X-Lem-Client-Version': '0.5.4'}
    engine = app.state.factory.kw['bind']

    with held_download_server(app, devices) as (url, entered, release):
        with ThreadPoolExecutor(max_workers=devices) as pool:
            def download(slot):
                with httpx.Client(base_url=url, headers=technician, trust_env=False, timeout=20) as client:
                    with client.stream('GET', path, headers={'X-Test-Download-Slot': str(slot)}) as response:
                        assert response.status_code == 200
                        if slot % 2:
                            return None  # Simulate a device disconnecting before its body arrives.
                        return response.read()

            pending = [pool.submit(download, n) for n in range(devices)]
            try:
                assert all(signal.wait(10) for signal in entered)
                assert engine.pool.checkedout() == 0
                with httpx.Client(base_url=url, headers=headers, trust_env=False, timeout=5) as admin:
                    before = time.monotonic()
                    metadata = admin.get(f'/api/reports/{rid}')
                    assert metadata.status_code == 200, metadata.text
                    invalid = body(working)
                    invalid['payload']['finished'] = 'invalid'
                    rejected = admin.post(f'/api/reports/{rid}/correction/commit', json=invalid)
                    assert rejected.status_code == 422, rejected.text
                    assert admin.get(f'/api/reports/{original["id"]}').json() == original
                    assert admin.get(f'/api/reports/{rid}').json()['status'] == 'draft'
                    staged = body(working)
                    staged['status'] = 'draft'
                    saved = admin.put(f'/api/reports/{rid}', json=staged)
                    assert saved.status_code == 200, saved.text
                    commit_body = body(saved.json())
                    result = admin.post(f'/api/reports/{rid}/correction/commit', json=commit_body)
                    assert result.status_code == 200, result.text
                    # Retry a lost acknowledgement while the old bytes are still
                    # leased. It must neither duplicate nor delete the new report.
                    assert admin.post(f'/api/reports/{rid}/correction/commit', json=commit_body).json() == result.json()
                    gone = admin.get(f'/api/reports/{original["id"]}')
                    assert gone.status_code == 410
                    assert gone.json()['detail']['replacement_id'] == rid
                    assert result.json()['status'] == 'closed'
                    assert result.json()['owner_id'] == original['owner_id']
                    print(f'{engine.dialect.name}: {devices} held downloads; GET/stage/commit/retry in {time.monotonic()-before:.3f}s')
                    assert not release.is_set()
            finally:
                release.set()
            assert [job.result(timeout=10) for job in pending] == [expected if n % 2 == 0 else None for n in range(devices)]
        deadline = time.monotonic() + 5
        while any(row['readers'] for row in app.state.evidence_cache.entries.values()) and time.monotonic() < deadline:
            time.sleep(.02)
        assert all(row['readers'] == 0 for row in app.state.evidence_cache.entries.values())
        assert engine.pool.checkedout() == 0
    copied = working['correction']['photo_map'][photo['id']]
    assert c.get(f'/api/reports/{rid}/photos/{copied}', headers=h).content == expected


@pytest.mark.parametrize('kind', ['pdf', 'bundle'])
def test_slow_export_render_does_not_block_correction(correction_case, monkeypatch, kind):
    c, h, contract, app, original = correction_case
    working = start(correction_case)
    renderer = importlib.import_module('app.pdf_report')
    build = renderer.build_pdf
    rendering, release = Event(), Event()

    def slow_build(document, photos):
        rendering.set()
        assert release.wait(20), 'Test did not release the renderer'
        # Render the original snapshot after it has been replaced in the DB.
        # Staged bytes must still be complete, including its old signature.
        return build(document, photos)

    monkeypatch.setattr(renderer, 'build_pdf', slow_build)
    with held_download_server(app, 0) as (url, _, _):
        headers = {**h, 'X-Lem-Client-Version': '0.5.4'}
        with httpx.Client(base_url=url, headers=headers, trust_env=False, timeout=8) as admin:
            with ThreadPoolExecutor(max_workers=1) as pool:
                exporting = pool.submit(admin.get, f'/api/reports/{original["id"]}/{kind}')
                try:
                    assert rendering.wait(10)
                    # A renderer can take minutes. Keeping its DB connection or
                    # source row locks here blocks correction and other writers.
                    assert app.state.factory.kw['bind'].pool.checkedout() == 0
                    branding = admin.put(f'/api/contracts/{contract["id"]}/presentation',
                        json={'issuer': 'CSMTEC', 'client_logos': [], 'base_revision': 0})
                    assert branding.status_code == 200, branding.text
                    assert admin.get(f'/api/reports/{working["id"]}').status_code == 200
                    result = admin.post(f'/api/reports/{working["id"]}/correction/commit', json=body(working))
                    assert result.status_code == 200, result.text
                    assert admin.get(f'/api/reports/{original["id"]}').status_code == 410
                    assert not release.is_set()
                finally:
                    release.set()
                exported = exporting.result(timeout=15)
                assert exported.status_code == 200, exported.text[:200]
                if kind == 'pdf':
                    assert exported.content.startswith(b'%PDF-')
                else:
                    import json
                    from io import BytesIO
                    from zipfile import ZipFile
                    from hashlib import sha256
                    with ZipFile(BytesIO(exported.content)) as archive:
                        assert archive.testzip() is None
                        document = json.loads(archive.read(next(name for name in archive.namelist() if name.endswith(".json"))))
                        assert document['id'] == original['id']
                        assert document['payload'] == original['payload']
                        assert document['presentation'] == original['presentation']
                        for photo in document['photos']:
                            member = next(name for name in archive.namelist()
                                          if f'/{document["identifier"]}_{photo["id"]}.' in name)
                            assert sha256(archive.read(member)).hexdigest() == photo['digest']


@pytest.mark.parametrize('kind', ['pdf', 'bundle'])
@pytest.mark.parametrize('failure', ['staging', 'rendering'])
def test_export_failure_releases_snapshot_and_preserves_both_reports(correction_case, monkeypatch, kind, failure):
    from pathlib import Path
    c, h, _, app, original = correction_case
    working = start(correction_case)
    temporary_paths = []
    write = Path.write_bytes

    def fail_write(path, data):
        if path.parent.name.startswith(('lem-pdf-', 'lem-bundle-')):
            temporary_paths.append(path.parent)
            raise OSError('Simulated staging disk failure')
        return write(path, data)

    def fail_render(document, photos):
        temporary_paths.extend(photo.source_path.parent for photo in photos)
        assert app.state.factory.kw['bind'].pool.checkedout() == 0
        raise OSError('Simulated renderer failure')

    if failure == 'staging':
        monkeypatch.setattr(Path, 'write_bytes', fail_write)
    else:
        monkeypatch.setattr('app.pdf_report.build_pdf', fail_render)
    with pytest.raises(OSError, match='Simulated'):
        c.get(f'/api/reports/{original["id"]}/{kind}', headers=h)
    assert temporary_paths and not any(path.exists() for path in temporary_paths)
    assert app.state.factory.kw['bind'].pool.checkedout() == 0
    assert c.get(f'/api/reports/{original["id"]}', headers=h).json() == original
    assert c.get(f'/api/reports/{working["id"]}', headers=h).json() == working
    # A failed export must leave no lock that prevents the saved correction.
    committed = c.post(f'/api/reports/{working["id"]}/correction/commit', headers=h, json=body(working))
    assert committed.status_code == 200, committed.text


def test_cold_cache_waiters_do_not_block_metadata_or_staging(correction_case, technician, monkeypatch):
    from threading import Barrier
    from sqlalchemy import select
    from app.models import Photo
    c, h, _, app, original = correction_case
    working = start(correction_case)
    photo = original['photos'][0]
    path = f'/api/reports/{original["id"]}/photos/{photo["id"]}'
    with app.state.factory() as db:
        expected = db.scalar(select(Photo.data).where(Photo.id == photo['id']))
    cache = app.state.evidence_cache
    acquire = cache.acquire
    ready, loading, release = Barrier(8), Event(), Event()
    loads = []

    def cold_acquire(key, size, load):
        def slow_load():
            loads.append(key)
            loading.set()
            assert release.wait(20)
            return load()
        ready.wait(timeout=10)
        return acquire(key, size, slow_load)

    monkeypatch.setattr(cache, 'acquire', cold_acquire)
    with held_download_server(app, 0) as (url, _, _):
        def download():
            with httpx.Client(base_url=url, headers=technician, trust_env=False, timeout=15) as device:
                response = device.get(path)
                assert response.status_code == 200, response.text
                return response.content

        with ThreadPoolExecutor(max_workers=8) as pool:
            downloads = [pool.submit(download) for _ in range(8)]
            try:
                assert loading.wait(10)
                assert app.state.factory.kw['bind'].pool.checkedout() == 0
                with httpx.Client(base_url=url, headers={**h, 'X-Lem-Client-Version': '0.5.4'},
                                  trust_env=False, timeout=5) as admin:
                    got = admin.get(f'/api/reports/{working["id"]}')
                    assert got.status_code == 200, got.text
                    staged = body(working)
                    staged['status'] = 'draft'
                    saved = admin.put(f'/api/reports/{working["id"]}', json=staged)
                    assert saved.status_code == 200, saved.text
                    assert admin.get(f'/api/reports/{original["id"]}').json() == original
                    assert not release.is_set()
            finally:
                release.set()
            assert [job.result(timeout=10) for job in downloads] == [expected]*8
    assert len(loads) == 1
    assert app.state.factory.kw['bind'].pool.checkedout() == 0
