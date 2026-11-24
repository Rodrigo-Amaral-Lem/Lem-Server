# SPDX-License-Identifier: GPL-3.0-only
from io import BytesIO
import uuid
import pytest
from PIL import Image
from sqlalchemy import select
from app.compatibility import VERSION
from test_workflow import setup
from test_export_resources import make_report


def evidence(setup):
    client, headers, contract, app = setup
    rid = make_report(client, headers, contract, closed=False)
    data = BytesIO()
    Image.effect_noise((128,128), 80).convert('RGB').save(data, 'PNG')
    original, pid = data.getvalue(), str(uuid.uuid4())
    response = client.put(f'/api/reports/{rid}/photos/{pid}', headers=headers,
        data={'checkpoint':'EST.01','base_revision':'1'}, files={'file':('original.png',original,'image/png')})
    assert response.status_code == 200, response.text
    return client, headers, f'/api/reports/{rid}/photos/{pid}', original, app


def test_ranges_resume_original_bytes_and_validate_access(setup):
    c,h,url,data,app = evidence(setup)
    full = c.get(url, headers=h)
    assert full.content == data and full.headers['accept-ranges'] == 'bytes'
    tag = full.headers['etag']
    part = c.get(url, headers={**h, 'Range':'bytes=17-', 'If-Range':tag})
    assert part.status_code == 206 and data[:17]+part.content == data
    assert part.headers['content-range'] == f'bytes 17-{len(data)-1}/{len(data)}'
    assert int(part.headers['content-length']) == len(data)-17
    assert c.get(url,headers={**h,'Range':'bytes=2-9'}).content == data[2:10]
    assert c.get(url,headers={**h,'Range':'bytes=-7'}).content == data[-7:]
    stale = c.get(url, headers={**h,'Range':'bytes=17-','If-Range':'"stale"'})
    assert stale.status_code == 200 and stale.content == data
    for value in ('bytes=9999999-', 'bytes=-0', 'bytes=10-2', 'bytes=bad', 'bytes='+'9'*5000+'-'):
        invalid = c.get(url,headers={**h,'Range':value})
        assert invalid.status_code == 416 and invalid.headers['content-range'] == f'bytes */{len(data)}'
    assert c.get(url,headers={'Range':'bytes=0-9'}).status_code in (401,403)
    foreign = url.replace(url.split('/')[3], str(uuid.uuid4()))
    assert c.get(foreign,headers={**h,'Range':'bytes=0-9'}).status_code == 404


def test_original_download_releases_database_before_sending(setup):
    c,h,url,data,app = evidence(setup)
    # Call the endpoint before any ASGI send; its database lease must be released.
    endpoint = next(route.endpoint for route in app.routes if getattr(route,'path','') == '/api/reports/{rid}/photos/{pid}' and 'GET' in (getattr(route,'methods',set())))
    from starlette.requests import Request
    from app.models import User
    with app.state.factory() as db:
        user = db.scalar(select(User).where(User.email == 'admin@lem.pt'))
        response = endpoint(url.split('/')[3], url.split('/')[-1], Request({'type':'http','headers':[]}), u=user, db=db)
        assert not db.in_transaction()
        assert response.lease.read() == data
        response.lease.close()
        assert app.state.factory.kw['bind'].pool.checkedout() == 0


def test_slow_download_releases_pool_and_limits_memory(tmp_path, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    monkeypatch.setenv('LEM_DOWNLOAD_CONCURRENCY','1')
    fixture = setup.__wrapped__(tmp_path, monkeypatch)
    c,h,url,data,app = evidence(fixture)
    entered, release = Event(), Event()
    scope = {'type':'http','asgi':{'version':'3.0','spec_version':'2.4'}, 'method':'GET',
        'scheme':'http','path':url,'raw_path':url.encode(),'query_string':b'', 'http_version':'1.1',
        'server':('testserver',80),'client':('127.0.0.1',1234),
        'headers':[(b'x-lem-client-version', VERSION.encode('ascii')),(b'authorization',h['Authorization'].encode())]}
    async def receive():
        await asyncio.sleep(30)
        return {'type':'http.disconnect'}
    async def send(message):
        if message['type']=='http.response.body':
            entered.set()
            await asyncio.to_thread(release.wait, 15)
    with ThreadPoolExecutor(1) as pool:
        job = pool.submit(asyncio.run, app(scope,receive,send))
        try:
            assert entered.wait(8)
            assert app.state.factory.kw['bind'].pool.checkedout()==0
            assert c.get('/api/contracts',headers=h).status_code==200
            busy = c.get(url,headers=h)
            assert busy.status_code==503 and busy.headers['retry-after']=='2'
        finally:
            release.set()
        job.result(timeout=10)
    assert c.get(url,headers=h).content==data


def test_closed_cache_entry_reloads_original_while_older_lease_can_close(setup):
    client, headers, url, data, app = evidence(setup)
    assert client.get(url, headers=headers).content == data
    cache = app.state.evidence_cache
    key, = cache.entries
    entry = cache.entries[key]
    old_lease = cache.acquire(key, len(data), lambda: pytest.fail("expected a cache hit"))
    entry["source"].close()
    partial = client.get(url, headers={**headers, "Range": "bytes=9-"})
    assert partial.status_code == 206 and partial.content == data[9:]
    old_lease.close()
    assert cache.entries[key] is not entry
    assert cache.entries[key]["readers"] == 0
    assert entry["readers"] == 0


def test_stalled_sender_releases_original_download_slot(tmp_path, monkeypatch):
    import asyncio
    from app.evidence_cache import EvidenceResponse
    from starlette.requests import ClientDisconnect

    monkeypatch.setenv("LEM_DOWNLOAD_CONCURRENCY", "1")
    monkeypatch.setattr(EvidenceResponse, "send_timeout", 0.1)
    client, headers, url, data, app = evidence(setup.__wrapped__(tmp_path, monkeypatch))
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "GET", "scheme": "http", "path": url, "raw_path": url.encode(),
        "query_string": b"", "http_version": "1.1", "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
        "headers": [(b"x-lem-client-version", VERSION.encode()),
                    (b"authorization", headers["Authorization"].encode())],
    }

    async def receive():
        pytest.fail("ASGI 2.4 does not need a disconnect listener")

    async def send(message):
        if message["type"] == "http.response.body":
            await asyncio.Event().wait()

    with pytest.raises(ClientDisconnect):
        asyncio.run(app(scope, receive, send))
    assert all(entry["readers"] == 0 for entry in app.state.evidence_cache.entries.values())
    assert client.get(url, headers=headers).content == data


def test_busy_evidence_upload_does_not_consume_second_body(setup, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app import main

    client, headers, url, data, app = evidence(setup)
    original_inspect = main.inspect_evidence
    entered, release = Event(), Event()

    def hold_inspection(data):
        entered.set()
        assert release.wait(10)
        return original_inspect(data)

    monkeypatch.setattr(main, "inspect_evidence", hold_inspection)
    replacement = url.rsplit("/", 1)[0] + "/" + str(uuid.uuid4())
    multipart = {"data": {"checkpoint": "EST.01", "base_revision": "1"},
                 "files": {"file": ("other.png", data, "image/png")}}

    async def busy_upload():
        messages = []
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "method": "PUT", "scheme": "http", "path": replacement,
            "raw_path": replacement.encode(), "query_string": b"", "http_version": "1.1",
            "server": ("testserver", 80), "client": ("127.0.0.1", 1234),
            "headers": [(b"content-type", b"multipart/form-data; boundary=unused"),
                        (b"x-lem-client-version", VERSION.encode()),
                        (b"authorization", headers["Authorization"].encode())],
        }

        async def receive():
            pytest.fail("busy upload must not call receive")

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)
        return messages

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.put, url, headers=headers, **multipart)
        try:
            assert entered.wait(5)
            start, _ = asyncio.run(busy_upload())
            assert start["status"] == 503
            assert (b"retry-after", b"15") in start["headers"]
        finally:
            release.set()
        assert first.result(timeout=10).is_success
    assert client.put(replacement, headers=headers, **multipart).is_success
