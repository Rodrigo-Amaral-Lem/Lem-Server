# SPDX-License-Identifier: GPL-3.0-only
"""Bounded private disk cache for immutable originals, shared by concurrent readers.

Every request must authorize before acquiring a lease. Files are never served by
path/URL; independent open handles keep cancellation and byte ranges isolated.
"""
from collections import OrderedDict
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock, Event, BoundedSemaphore
import time
import uuid
from fastapi.responses import StreamingResponse


class EvidenceCache:
    def __init__(self, budget=512 * 1024 * 1024, ttl=600):
        self.directory = TemporaryDirectory(prefix="lem-evidence-")
        self.budget, self.ttl = budget, ttl
        self.lock = RLock()
        self.builds = BoundedSemaphore(2)
        self.entries = OrderedDict()
        self.loading = {}

    def _prune(self, needed=0):
        size = sum(e['size'] for e in self.entries.values())
        for key, entry in list(self.entries.items()):
            if entry['readers'] or (size + needed <= self.budget and
                    len(self.entries) + bool(needed) <= 2048 and time.monotonic()-entry['used'] < self.ttl):
                continue
            entry['path'].unlink(missing_ok=True)
            size -= entry['size']
            del self.entries[key]

    def acquire(self, key, size, load):
        while True:
            with self.lock:
                self._prune()
                entry = self.entries.get(key)
                if entry:
                    source = entry['path'].open('rb')
                    entry['readers'] += 1
                    entry['used'] = time.monotonic()
                    self.entries.move_to_end(key)
                    return Lease(self, key, source)
                waiting = self.loading.get(key)
                if waiting is None:
                    self.loading[key] = Event()
                    break
            waiting.wait()  # Caller has released its database connection already.
        path = Path(self.directory.name) / uuid.uuid4().hex
        try:
            with self.builds:
                data = load()
                if data is None or len(data) != size:
                    raise FileNotFoundError("Original indisponível")
                path.write_bytes(data)
                del data
            with self.lock:
                self._prune(size)
                source = path.open('rb')
                self.entries[key] = {'path':path, 'size':size, 'readers':1, 'used':time.monotonic()}
                return Lease(self, key, source)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            with self.lock:
                self.loading.pop(key).set()


class Lease:
    def __init__(self, cache, key, source):
        self.cache, self.key, self.source = cache, key, source
        self.closed = False

    def close(self):
        with self.cache.lock:
            if self.closed:
                return
            self.closed = True
            self.source.close()
            self.cache.entries[self.key]['readers'] -= 1
            self.cache._prune()


class EvidenceResponse(StreamingResponse):
    def __init__(self, lease, start, length, mime, headers, status):
        self.lease = lease
        lease.source.seek(start)

        def chunks():
            remaining = length
            while remaining:
                block = lease.source.read(min(256 * 1024, remaining))
                if not block:
                    raise OSError("Evidência interrompida no armazenamento")
                remaining -= len(block)
                yield block

        super().__init__(chunks(), status_code=status, media_type=mime,
                         headers={**headers, 'Content-Length':str(length)})

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.lease.close()
