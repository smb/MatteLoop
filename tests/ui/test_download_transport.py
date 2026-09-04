from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

import matteloop.ui.download_transport as download_transport
from matteloop.core.errors import AppError, ErrorCode
from matteloop.jobs.models.catalog import ModelCatalog, ModelSpec
from matteloop.jobs.models.download import (
    DownloadHttpError,
    DownloadResponse,
    DownloadTransport,
    ModelDownloader,
)
from matteloop.ui.preview_controller import _QtNetworkDownloadTransport


@dataclass(frozen=True)
class _HttpResponse:
    body: bytes
    status: int = 200
    chunk_size: int = 2
    include_length: bool = True
    length_header: str = "Content-Length"
    location: str | None = None
    delay: float = 0.0


class _HttpHandler(BaseHTTPRequestHandler):
    response: ClassVar[_HttpResponse]
    protocol_version = "HTTP/1.0"

    def do_GET(self) -> None:
        response = self.response
        self.send_response(response.status)
        if response.location is not None:
            self.send_header("Location", response.location)
        if response.include_length:
            self.send_header(response.length_header, str(len(response.body)))
        self.send_header("Connection", "close")
        self.end_headers()
        for offset in range(0, len(response.body), response.chunk_size):
            try:
                self.wfile.write(response.body[offset : offset + response.chunk_size])
                self.wfile.flush()
            except OSError:
                return
            if response.delay:
                time.sleep(response.delay)

    def log_message(self, *_args: object) -> None:
        return


class _FakeSignal:
    def __init__(self) -> None:
        self._slots: list[Callable[..., None]] = []

    def connect(self, slot: Callable[..., None]) -> None:
        self._slots.append(slot)

    def disconnect(self, slot: Callable[..., None]) -> None:
        self._slots.remove(slot)

    def emit(self) -> None:
        for slot in tuple(self._slots):
            slot()


class _FakeReply:
    def __init__(self, *, available: int = 0, finished: bool = False) -> None:
        self.available = available
        self._finished_state = finished
        self.status = 200
        self.error_name = "NoError"
        self.readyRead = _FakeSignal()
        self.finished = _FakeSignal()
        self.errorOccurred = _FakeSignal()
        self.sslErrors = _FakeSignal()
        self.metaDataChanged = _FakeSignal()

    def bytesAvailable(self) -> int:
        return self.available

    def isFinished(self) -> bool:
        return self.finished_state

    @property
    def finished_state(self) -> bool:
        return bool(self._finished_state)

    @finished_state.setter
    def finished_state(self, value: bool) -> None:
        self._finished_state = value

    def error(self) -> SimpleNamespace:
        return SimpleNamespace(name=self.error_name)

    def read(self, _size: int) -> SimpleNamespace:
        self.available = 0
        return SimpleNamespace(data=lambda: b"x")

    def attribute(self, _attribute: object) -> int:
        return self.status


class _FakeTimer:
    def __init__(self) -> None:
        self.timeout = _FakeSignal()

    def setSingleShot(self, _single_shot: bool) -> None:
        return

    def start(self, _milliseconds: int) -> None:
        return

    def stop(self) -> None:
        return


@pytest.fixture
def reload_download_transport(monkeypatch: pytest.MonkeyPatch):
    variable = "MATTELOOP_DOWNLOAD_TRACE"
    original = os.environ.get(variable)

    def reload_with(value: str | None):
        if value is None:
            monkeypatch.delenv(variable, raising=False)
        else:
            monkeypatch.setenv(variable, value)
        return importlib.reload(download_transport)

    yield reload_with
    if original is None:
        monkeypatch.delenv(variable, raising=False)
    else:
        monkeypatch.setenv(variable, original)
    importlib.reload(download_transport)


def _response_with_fake_wait(
    module: object,
    monkeypatch: pytest.MonkeyPatch,
    reply: _FakeReply,
    event: Callable[[_FakeReply, _FakeTimer], None],
):
    timer_box: list[_FakeTimer] = []

    class FakeEventLoop:
        def quit(self) -> None:
            return

        def exec(self) -> None:
            event(reply, timer_box[0])

    class FakeTimer:
        def __init__(self) -> None:
            timer = _FakeTimer()
            timer_box.append(timer)
            self.timeout = timer.timeout

        def setSingleShot(self, single_shot: bool) -> None:
            timer_box[-1].setSingleShot(single_shot)

        def start(self, milliseconds: int) -> None:
            timer_box[-1].start(milliseconds)

        def stop(self) -> None:
            timer_box[-1].stop()

    monkeypatch.setattr(module, "QEventLoop", FakeEventLoop)
    monkeypatch.setattr(module, "QTimer", FakeTimer)
    response = module._QtNetworkDownloadResponse.__new__(
        module._QtNetworkDownloadResponse
    )
    response._reply = reply
    response._wait_index = 0
    response._empty_wakes = 0
    response._trace_byte_total = 0
    response._next_trace_byte_log = 0
    response._constructed_thread_id = threading.get_ident()
    response._closed = False
    response._tls_error = False
    return response


def _ready_read_event(reply: _FakeReply, _timer: _FakeTimer) -> None:
    reply.readyRead.emit()


@pytest.fixture
def http_server() -> Callable[[_HttpResponse], str]:
    servers: list[ThreadingHTTPServer] = []
    threads: list[threading.Thread] = []

    def start(response: _HttpResponse) -> str:
        handler = type("FixtureHttpHandler", (_HttpHandler,), {"response": response})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        threads.append(thread)
        return f"http://127.0.0.1:{server.server_port}/model"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()
    for thread in threads:
        thread.join(timeout=2)


def _transport() -> DownloadTransport:
    return _QtNetworkDownloadTransport()


def _catalog_for(data: bytes) -> tuple[ModelCatalog, ModelSpec]:
    payload = json.loads(ModelCatalog.resource_path().read_text(encoding="utf-8"))
    model = next(item for item in payload["models"] if item["id"] == "u2net")
    model["artifact"]["size_bytes"] = len(data)
    model["artifact"]["sha256"] = hashlib.sha256(data).hexdigest()
    catalog = ModelCatalog.from_bytes(json.dumps(payload).encode())
    return catalog, catalog.get("u2net")


class _LocalUrlTransport:
    def __init__(self, url: str) -> None:
        self._transport = _transport()
        self._url = url

    def open(self, _url: str) -> DownloadResponse:
        return self._transport.open(self._url)


def test_qt_transport_reads_chunked_http_body_in_bounded_chunks(
    http_server: Callable[[_HttpResponse], str], qtbot
) -> None:
    del qtbot
    body = b"chunked model response"
    response = _transport().open(http_server(_HttpResponse(body, chunk_size=3)))
    try:
        chunks: list[bytes] = []
        while chunk := response.read(4):
            chunks.append(chunk)
    finally:
        response.close()

    assert b"".join(chunks) == body
    assert all(0 < len(chunk) <= 4 for chunk in chunks)


def test_qt_transport_exposes_content_length_header(
    http_server: Callable[[_HttpResponse], str], qtbot
) -> None:
    del qtbot
    body = b"known-length model response"
    response = _transport().open(
        http_server(_HttpResponse(body, length_header="content-length"))
    )
    try:
        assert response.headers["Content-Length"] == str(len(body))
    finally:
        response.close()


def test_qt_transport_reports_non_success_http_status(
    http_server: Callable[[_HttpResponse], str], qtbot
) -> None:
    del qtbot
    with pytest.raises(DownloadHttpError) as exc:
        _transport().open(http_server(_HttpResponse(b"unavailable", status=503)))

    assert exc.value.status == 503


def test_model_downloader_cancels_qt_transport_between_chunks(
    tmp_path: Path,
    http_server: Callable[[_HttpResponse], str],
    qtbot,
) -> None:
    del qtbot
    data = b"cancel after the first downloaded chunk"
    url = http_server(_HttpResponse(data, chunk_size=2, delay=0.01))
    catalog, spec = _catalog_for(data)
    progress: list[tuple[int, int]] = []
    cancellation_requested = False

    def progress_callback(completed: int, total: int) -> None:
        nonlocal cancellation_requested
        progress.append((completed, total))
        if completed:
            cancellation_requested = True

    with pytest.raises(AppError) as exc:
        ModelDownloader(
            _LocalUrlTransport(url), catalog=catalog, chunk_size=4
        ).download(
            spec,
            tmp_path,
            progress_callback,
            lambda: cancellation_requested,
        )

    assert exc.value.code is ErrorCode.JOB_CANCELLED
    assert progress[0] == (0, len(data))
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.rglob("*.onnx"))


def test_qt_transport_follows_a_redirect_to_the_final_body(
    http_server: Callable[[_HttpResponse], str],
) -> None:
    """Weight URLs redirect; the first 3xx must not be reported as the status."""
    data = b"redirected-model-bytes"
    final_url = http_server(_HttpResponse(body=data))
    redirect_url = http_server(
        _HttpResponse(body=b"", status=302, location=final_url, include_length=False)
    )

    response = _transport().open(redirect_url)
    try:
        assert response.headers["Content-Length"] == str(len(data))
        chunks = []
        while chunk := response.read(8):
            chunks.append(chunk)
        assert b"".join(chunks) == data
    finally:
        response.close()


def test_download_trace_is_silent_when_the_environment_variable_is_unset(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reload_download_transport,
) -> None:
    module = reload_download_transport(None)
    reply = _FakeReply(available=1)
    response = _response_with_fake_wait(module, monkeypatch, reply, _ready_read_event)

    with caplog.at_level(logging.INFO, logger=module.__name__):
        response._wait_for_event(include_metadata=False)

    assert not [record for record in caplog.records if record.name == module.__name__]


def test_download_trace_records_each_wait_with_reply_state(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reload_download_transport,
) -> None:
    module = reload_download_transport("1")
    reply = _FakeReply(available=7)
    response = _response_with_fake_wait(module, monkeypatch, reply, _ready_read_event)

    with caplog.at_level(logging.INFO, logger=module.__name__):
        response._wait_for_event(include_metadata=True)

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == module.__name__
    ]
    wait_message = next(
        message for message in messages if message.startswith("download wait ")
    )
    assert "index=1" in wait_message
    assert "metadata=True" in wait_message
    assert "bytes_available=7" in wait_message
    assert "finished=False" in wait_message
    assert "error=NoError" in wait_message
    assert "elapsed_ms=" in wait_message


def test_download_trace_counts_empty_wakes_until_bytes_are_read(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reload_download_transport,
) -> None:
    module = reload_download_transport("1")
    reply = _FakeReply()
    response = _response_with_fake_wait(module, monkeypatch, reply, _ready_read_event)

    with caplog.at_level(logging.INFO, logger=module.__name__):
        response._wait_for_event(include_metadata=False)
        response._wait_for_event(include_metadata=False)
        response._wait_for_event(include_metadata=False)
        reply.available = 1
        assert response.read(1) == b"x"
        response._wait_for_event(include_metadata=False)

    empty_messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("download empty wake ")
    ]
    assert ["consecutive=1", "consecutive=2", "consecutive=3", "consecutive=1"] == [
        message.rsplit(" ", 1)[-1] for message in empty_messages
    ]


def test_download_trace_reports_guard_timer_timeout(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reload_download_transport,
) -> None:
    module = reload_download_transport("1")
    response = _response_with_fake_wait(
        module,
        monkeypatch,
        _FakeReply(),
        lambda _reply, timer: timer.timeout.emit(),
    )

    with (
        caplog.at_level(logging.INFO, logger=module.__name__),
        pytest.raises(TimeoutError, match="timed out"),
    ):
        response._wait_for_event(include_metadata=False)

    assert any(
        "download guard timer expired wait_index=1" in record.getMessage()
        for record in caplog.records
    )


def test_download_trace_logs_response_location_and_thread(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reload_download_transport,
) -> None:
    module = reload_download_transport("1")
    response = module._QtNetworkDownloadResponse.__new__(
        module._QtNetworkDownloadResponse
    )
    response._reply = _FakeReply()
    response._constructed_thread_id = threading.get_ident()

    with caplog.at_level(logging.INFO, logger=module.__name__):
        response._trace_response("https://example.test/models/model.onnx?token=secret")

    message = caplog.records[-1].getMessage()
    assert "host=example.test" in message
    assert "path=/models/model.onnx" in message
    assert "status=200" in message
    assert f"thread_id={threading.get_ident()}" in message
    assert "secret" not in message


def test_download_trace_logs_onnxruntime_distribution_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reload_download_transport,
) -> None:
    module = reload_download_transport("1")
    import matteloop.app as app

    monkeypatch.setattr(
        app, "_onnxruntime_distribution", lambda: ("onnxruntime", "1.2.3")
    )

    with caplog.at_level(logging.INFO, logger=module.__name__):
        module._log_onnxruntime_trace_once()
        module._log_onnxruntime_trace_once()

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("download onnxruntime distribution=")
    ]
    assert messages == ["download onnxruntime distribution=onnxruntime version=1.2.3"]
