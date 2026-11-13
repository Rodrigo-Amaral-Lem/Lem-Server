from app.main import TEMPLATE
"""Evidence completeness and resource admission under overlapping requests."""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import importlib
import uuid
import pytest
from app.exports import MissingEvidence, write_bundle
from app.images import inspect_image
from app.pdf_report import build_pdf
from test_workflow import setup, upload, answers
from test_bug_regressions import _report, _jpeg


def test_missing_original_aborts_archive_and_pdf():
    photo = SimpleNamespace(
        id="gone", checkpoint="EST.01", mime="image/jpeg", filename="missing.jpg", data=None
    )
    with pytest.raises(MissingEvidence):
        write_bundle(BytesIO(), {"photos": [{"id": "gone"}]}, [photo], b"%PDF-test")
    with pytest.raises(MissingEvidence):
        build_pdf(_report(), [photo])


def test_database_failure_is_not_mislabeled_as_image_damage():
    class Broken:
        checkpoint, filename = "EST.01", "original.jpg"

        @property
        def data(self):
            raise RuntimeError("database failure")

    with pytest.raises(RuntimeError, match="database failure"):
        build_pdf(_report(), [Broken()])


def test_memory_failure_is_not_invalid_input(monkeypatch):
    def fail(*args, **kwargs):
        raise MemoryError("resource exhausted")

    monkeypatch.setattr("app.images.Image.open", fail)
    with pytest.raises(MemoryError):
        inspect_image(b"test")


def make_report(client, headers, contract, closed=False):
    upload(client, headers, contract["id"], True)
    charger = client.get(f'/api/contracts/{contract["id"]}/chargers', headers=headers).json()[0]
    rid = str(uuid.uuid4())
    result = client.put(
        f"/api/reports/{rid}",
        headers=headers,
        json={
            "checklist_version": TEMPLATE["version"], "charger_id": charger["id"],
            "base_revision": 0,
            "status": "closed" if closed else "draft",
            "payload": {"answers": answers()},
        },
    )
    assert result.status_code == 200, result.text
    return rid


@pytest.mark.parametrize("operation", ["export", "image"])
def test_busy_admission_does_not_acquire_another_connection(tmp_path, monkeypatch, operation):
    monkeypatch.setenv("LEM_EXPORT_CONCURRENCY", "1")
    monkeypatch.setenv("LEM_IMAGE_CONCURRENCY", "1")
    client, headers, contract, app = setup.__wrapped__(tmp_path, monkeypatch)
    rid = make_report(client, headers, contract, closed=operation == "export")
    entered, release = Event(), Event()
    engine = app.state.factory.kw["bind"]

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return b"%PDF-test" if operation == "export" else "image/jpeg"

    if operation == "export":
        monkeypatch.setattr("app.pdf_report.build_pdf", slow)

        def request():
            return client.get(f"/api/reports/{rid}/pdf", headers=headers)

    else:
        monkeypatch.setattr("app.main.inspect_evidence", slow)
        original = _jpeg((8, 8))

        def request():
            return client.put(
                f"/api/reports/{rid}/photos/{uuid.uuid4()}",
                headers=headers,
                data={"checkpoint": "EST.01", "base_revision": "1"},
                files={"file": ("photo.jpg", original)},
            )

    with client, ThreadPoolExecutor(1) as pool:
        active = pool.submit(request)
        try:
            assert entered.wait(5)
            busy = request()
            assert busy.status_code == 503 and busy.headers["retry-after"] == "15"
            assert engine.pool.checkedout() == 1
        finally:
            release.set()
        assert active.result(timeout=10).status_code == 200
    assert engine.pool.checkedout() == 0


def test_generated_archive_streams_without_database_connection(setup, monkeypatch):
    client, headers, contract, app = setup
    rid = make_report(client, headers, contract, closed=True)
    main = importlib.import_module("app.main")
    original = main.file_chunks
    checked = []

    def check(source):
        for chunk in original(source):
            checked.append(app.state.factory.kw["bind"].pool.checkedout())
            yield chunk

    monkeypatch.setattr(main, "file_chunks", check)
    result = client.get(f"/api/reports/{rid}/bundle", headers=headers)
    assert result.status_code == 200 and result.content.startswith(b"PK")
    assert checked and set(checked) == {0}


def test_spool_closes_when_stream_fails_before_first_chunk(setup, monkeypatch):
    client, headers, contract, app = setup
    rid = make_report(client, headers, contract, closed=True)
    main = importlib.import_module("app.main")
    factory = main.SpooledTemporaryFile
    files = []

    def capture(*args, **kwargs):
        source = factory(*args, **kwargs)
        files.append(source)
        return source

    async def disconnect(*args, **kwargs):
        raise OSError("disconnected before headers")

    monkeypatch.setattr(main, "SpooledTemporaryFile", capture)
    monkeypatch.setattr(main.StreamingResponse, "stream_response", disconnect)
    with pytest.raises(Exception):
        client.get(f"/api/reports/{rid}/bundle", headers=headers)
    assert files and all(source.closed for source in files)
    assert app.state.factory.kw["bind"].pool.checkedout() == 0
