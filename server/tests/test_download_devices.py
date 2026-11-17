# SPDX-License-Identifier: GPL-3.0-only
"""Real loopback sockets: competing devices, cancellation and digest-safe resumption."""
import hashlib
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import httpx
import pytest
import uvicorn
from sqlalchemy import event, update
from app.models import Photo
from app.evidence_cache import EvidenceCache
from test_workflow import setup
from test_evidence_downloads import evidence


def test_four_devices_share_original_abort_resume_and_release_resources(setup):
    c,h,path,_,app=evidence(setup)
    # This test exercises transport, not image decoding: seed a large opaque original.
    original=os.urandom(16*1024*1024)
    digest=hashlib.sha256(original).hexdigest()
    pid=path.split('/')[-1]
    with app.state.factory() as db:
        db.execute(update(Photo).where(Photo.id==pid).values(data=original,digest=digest))
        db.commit()
    reads=[]
    engine=app.state.factory.kw['bind']
    def observe(connection,cursor,statement,params,context,many):
        if statement.lstrip().lower().startswith('select photos.data '): reads.append(params)
    event.listen(engine,'before_cursor_execute',observe)
    listener=socket.socket(); listener.bind(('127.0.0.1',0))
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='off'))
    thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True)
    thread.start()
    url=f'http://127.0.0.1:{listener.getsockname()[1]}'
    headers={**h,'X-Lem-Client-Version':'0.2.6'}
    barrier=threading.Barrier(4)
    def device(n):
        with httpx.Client(base_url=url,headers=headers,trust_env=False,timeout=20) as client:
            barrier.wait(timeout=10)
            prefix=bytearray()
            with client.stream('GET',path) as response:
                assert response.status_code==200
                tag=response.headers['etag']
                for chunk in response.iter_bytes(65536):
                    prefix.extend(chunk)
                    if n<2 and len(prefix)>=1024*1024:
                        break  # Close the TCP stream while the server still has bytes to send.
            if n<2:
                assert client.get('/api/contracts').status_code==200
                resumed=client.get(path,headers={'Range':f'bytes={len(prefix)}-','If-Range':tag})
                assert resumed.status_code==206
                prefix.extend(resumed.content)
            assert hashlib.sha256(prefix).hexdigest()==digest
            return len(prefix)
    try:
        deadline=time.monotonic()+10
        while not server.started and time.monotonic()<deadline: time.sleep(.02)
        assert server.started
        started=time.monotonic()
        with ThreadPoolExecutor(4) as pool:
            sizes=list(pool.map(device,range(4)))
        elapsed=time.monotonic()-started
        assert sizes==[len(original)]*4
        assert len(reads)==1,reads
        with httpx.Client(base_url=url,headers=headers,trust_env=False) as client:
            assert client.get(path,headers={'Authorization':'Bearer invalid'}).status_code==401
            assert client.get(path,headers={'Range':'bytes=0-31'}).content==original[:32]
        deadline=time.monotonic()+5
        while any(e['readers'] for e in app.state.evidence_cache.entries.values()) and time.monotonic()<deadline:
            time.sleep(.02)
        assert all(e['readers']==0 for e in app.state.evidence_cache.entries.values())
        assert engine.pool.checkedout()==0
        print(f'4 devices, 2 interrupted/resumed: {sum(sizes)/1048576:.0f} MiB in {elapsed:.2f}s; 1 original DB read')
    finally:
        server.should_exit=True; thread.join(timeout=10); listener.close()
        event.remove(engine,'before_cursor_execute',observe)
        assert not thread.is_alive()


def test_cache_failure_retries_and_active_readers_survive_eviction():
    cache=EvidenceCache(budget=8,ttl=600)
    calls=[]
    def load(): calls.append(True); return b'original'
    a=cache.acquire('a',8,load); b=cache.acquire('a',8,load)
    assert len(calls)==1
    other=cache.acquire('b',8,lambda:b'new-data')
    assert a.source.read()==b'original' and b.source.read()==b'original'
    a.close(); b.close()
    assert 'a' not in cache.entries and other.source.read()==b'new-data'
    other.close()
    with pytest.raises(OSError): cache.acquire('bad',10,lambda:(_ for _ in ()).throw(OSError('disk/db failure')))
    assert not cache.loading
    repaired=cache.acquire('bad',4,lambda:b'fine'); assert repaired.source.read()==b'fine'
    repaired.close()
