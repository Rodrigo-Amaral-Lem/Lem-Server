# SPDX-License-Identifier: GPL-3.0-only
"""Bounded private disk cache for immutable originals, shared by concurrent readers.

Every request must authorize before acquiring a lease. Files are never served by
path or URL. Each lease tracks its own byte position.
"""
import asyncio
from collections import OrderedDict
from tempfile import TemporaryFile
from threading import RLock, Lock, Event, BoundedSemaphore
import time
from fastapi.responses import StreamingResponse


class EvidenceCache:
    def __init__(self, budget=512 * 1024 * 1024, ttl=600):
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
            entry['source'].close()
            size -= entry['size']
            del self.entries[key]

    def acquire(self, key, size, load):
        while True:
            with self.lock:
                self._prune()
                entry = self.entries.get(key)
                if entry and entry['source'].closed:
                    # Existing leases retain their original entry while a new
                    # loader repopulates the key from the durable original.
                    del self.entries[key]
                    entry = None
                if entry:
                    entry['readers'] += 1
                    entry['used'] = time.monotonic()
                    self.entries.move_to_end(key)
                    return Lease(self, entry)
                waiting = self.loading.get(key)
                if waiting is None:
                    self.loading[key] = Event()
                    break
            waiting.wait()  # Caller has released its database connection already.
        source = None
        try:
            with self.builds:
                data = load()
                if data is None or len(data) != size:
                    raise FileNotFoundError("Original indisponível")
                # Anonymous on Linux and delete-on-close on Windows. A worker
                # exit releases the storage even without a lifespan shutdown.
                source = TemporaryFile(mode='w+b')
                source.write(data)
                source.flush()
                del data
            with self.lock:
                self._prune(size)
                entry = {'source': source, 'lock': Lock(), 'size': size, 'readers': 1, 'used': time.monotonic()}
                self.entries[key] = entry
                return Lease(self, entry)
        except BaseException:
            if source is not None:
                source.close()
            raise
        finally:
            with self.lock:
                self.loading.pop(key).set()

    def close(self):
        with self.lock:
            for key, entry in list(self.entries.items()):
                if not entry['readers']:
                    entry['source'].close()
                    del self.entries[key]


class Lease:
    def __init__(self, cache, entry):
        self.cache, self.entry = cache, entry
        self.position = 0
        self.closed = False

    def read(self, size=-1):
        with self.entry['lock']:
            source = self.entry['source']
            source.seek(self.position)
            data = source.read(size)
            self.position += len(data)
            return data

    def close(self):
        with self.cache.lock:
            if self.closed:
                return
            self.closed = True
            self.entry['readers'] -= 1
            self.cache._prune()


class EvidenceResponse(StreamingResponse):
    send_timeout = 30

    def __init__(self, lease, start, length, mime, headers, status):
        self.lease = lease
        lease.position = start

        def chunks():
            remaining = length
            while remaining:
                block = lease.read(min(256 * 1024, remaining))
                if not block:
                    raise OSError("Evidência interrompida no armazenamento")
                remaining -= len(block)
                yield block

        super().__init__(chunks(), status_code=status, media_type=mime,
                         headers={**headers, 'Content-Length':str(length)})

    async def __call__(self, scope, receive, send):
        async def bounded_send(message):
            # Once headers are sent the client must resume a timed-out body
            # with a range request. The aborted response releases its slot.
            await asyncio.wait_for(send(message), timeout=self.send_timeout)

        try:
            await super().__call__(scope, receive, bounded_send)
        finally:
            self.lease.close()
