from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from rembggui.core.errors import AppError, ErrorCode
from rembggui.jobs.models.catalog import ModelCatalog, ModelSpec
from rembggui.jobs.models.download import (
    DownloadHttpError,
    DownloadResponse,
    DownloadTransport,
    ModelDownloader,
)
from rembggui.ui.preview_controller import _QtNetworkDownloadTransport


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
