"""Qt Network adapter for the bounded model-download protocol."""

from __future__ import annotations

import logging
import os
import ssl
import threading
import time
from collections.abc import Mapping
from urllib.parse import urlsplit

from PySide6.QtCore import QEventLoop, QTimer, QUrl
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkProxyFactory,
    QNetworkRequest,
)

from matteloop.jobs.models.download import DownloadHttpError, DownloadProxyError

_NETWORK_TIMEOUT_MS = 60_000
_DOWNLOAD_TRACE = os.environ.get("MATTELOOP_DOWNLOAD_TRACE") == "1"
_DOWNLOAD_TRACE_BYTE_INTERVAL = 8 << 20
_LOGGER = logging.getLogger(__name__)
_ONNXRUNTIME_TRACE_LOGGED = False
_PROXY_ERROR_NAMES = frozenset(
    {
        "ProxyConnectionRefusedError",
        "ProxyConnectionClosedError",
        "ProxyNotFoundError",
        "ProxyTimeoutError",
        "ProxyAuthenticationRequiredError",
        "UnknownProxyError",
    }
)


class QtNetworkDownloadTransport:
    """Open model responses with Qt's platform trust store and proxy settings."""

    def open(self, url: str) -> _QtNetworkDownloadResponse:
        return _QtNetworkDownloadResponse(url)


class _QtNetworkDownloadResponse:
    def __init__(self, url: str) -> None:
        if _DOWNLOAD_TRACE:
            self._constructed_thread_id = threading.get_ident()
            self._wait_index = 0
            self._empty_wakes = 0
            self._trace_byte_total = 0
            self._next_trace_byte_log = 0
        QNetworkProxyFactory.setUseSystemConfiguration(True)
        self._manager = QNetworkAccessManager()
        request = QNetworkRequest(QUrl(url))
        # Weight URLs redirect (GitHub releases -> objects.githubusercontent.com).
        # NoLessSafe follows them without ever downgrading HTTPS to HTTP.
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        self._reply = self._manager.get(request)
        if _DOWNLOAD_TRACE:
            _log_onnxruntime_trace_once()
        self._tls_error = False
        self._closed = False
        self.headers: Mapping[str, str] = {}
        self._reply.sslErrors.connect(self._mark_tls_error)
        try:
            self._wait_for_event(include_metadata=True)
            while self._is_redirect() and not self._reply.isFinished():
                self._wait_for_event(include_metadata=True)
            if _DOWNLOAD_TRACE:
                self._trace_response(url)
            self._raise_http_or_transport_error()
            self.headers = _response_headers(self._reply)
        except BaseException:
            self.close()
            raise

    def read(self, size: int) -> bytes:
        if self._closed or size <= 0:
            return b""
        while True:
            self._raise_reply_error()
            available = self._reply.bytesAvailable()
            if available:
                chunk = bytes(self._reply.read(min(size, available)).data())
                if chunk or self._reply.isFinished():
                    if chunk and _DOWNLOAD_TRACE:
                        self._trace_read(len(chunk))
                        self._empty_wakes = 0
                    return chunk
            elif self._reply.isFinished():
                return b""
            self._wait_for_event(include_metadata=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._reply.isFinished():
            self._reply.abort()

    def _mark_tls_error(self, _errors: object) -> None:
        self._tls_error = True

    def _is_redirect(self) -> bool:
        status = self._http_status()
        return status is not None and 300 <= status < 400

    def _wait_for_event(self, *, include_metadata: bool) -> None:
        if _DOWNLOAD_TRACE:
            trace_started = time.monotonic()
            self._wait_index += 1
            wait_index = self._wait_index
        else:
            trace_started = 0.0
            wait_index = 0
        if self._reply.isFinished():
            if _DOWNLOAD_TRACE:
                self._trace_wait(wait_index, include_metadata, trace_started, False)
            return
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        signaled = False

        def wake(*_args: object) -> None:
            nonlocal signaled
            signaled = True
            loop.quit()

        self._reply.readyRead.connect(wake)
        self._reply.finished.connect(wake)
        self._reply.errorOccurred.connect(wake)
        self._reply.sslErrors.connect(wake)
        if include_metadata:
            self._reply.metaDataChanged.connect(wake)
        timer.timeout.connect(loop.quit)
        try:
            timer.start(_NETWORK_TIMEOUT_MS)
            loop.exec()
        finally:
            timer.stop()
            self._reply.readyRead.disconnect(wake)
            self._reply.finished.disconnect(wake)
            self._reply.errorOccurred.disconnect(wake)
            self._reply.sslErrors.disconnect(wake)
            if include_metadata:
                self._reply.metaDataChanged.disconnect(wake)
        if _DOWNLOAD_TRACE:
            self._trace_wait(wait_index, include_metadata, trace_started, signaled)
        if not signaled:
            if _DOWNLOAD_TRACE:
                _LOGGER.info(
                    "download guard timer expired wait_index=%d timeout_ms=%d",
                    wait_index,
                    _NETWORK_TIMEOUT_MS,
                )
            raise TimeoutError("Qt Network response timed out")

    def _trace_response(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or parsed.netloc or "<unknown>"
            path = parsed.path or "/"
        except ValueError:
            host = path = "<unavailable>"
        _LOGGER.info(
            "download response host=%s path=%s status=%s thread_id=%d",
            host,
            path,
            self._http_status(),
            self._constructed_thread_id,
        )

    def _trace_wait(
        self,
        wait_index: int,
        include_metadata: bool,
        started: float,
        signaled: bool,
    ) -> None:
        available = self._reply.bytesAvailable()
        _LOGGER.info(
            "download wait index=%d metadata=%s bytes_available=%d finished=%s "
            "error=%s elapsed_ms=%.1f",
            wait_index,
            include_metadata,
            available,
            self._reply.isFinished(),
            self._reply_error_name(),
            (time.monotonic() - started) * 1000,
        )
        if signaled and available == 0:
            self._empty_wakes += 1
            _LOGGER.info(
                "download empty wake wait_index=%d consecutive=%d",
                wait_index,
                self._empty_wakes,
            )

    def _trace_read(self, byte_count: int) -> None:
        self._trace_byte_total += byte_count
        if self._trace_byte_total < self._next_trace_byte_log:
            return
        _LOGGER.info("download read bytes_total=%d", self._trace_byte_total)
        self._next_trace_byte_log = (
            self._trace_byte_total + _DOWNLOAD_TRACE_BYTE_INTERVAL
        )

    def _raise_http_or_transport_error(self) -> None:
        status = self._http_status()
        error_name = self._reply_error_name()
        if self._tls_error or error_name == "SslHandshakeFailedError":
            self._raise_reply_error()
        if error_name in _PROXY_ERROR_NAMES:
            self._raise_reply_error()
        if status is not None and status != 200:
            raise DownloadHttpError(status)
        self._raise_reply_error()
        if status is None:
            raise OSError("Qt Network response did not expose an HTTP status")

    def _raise_reply_error(self) -> None:
        error_name = self._reply_error_name()
        if self._tls_error:
            detail = self._reply.errorString() or "TLS handshake failed"
            raise ssl.SSLError(detail)
        if not error_name or error_name == "NoError":
            return
        detail = self._reply.errorString() or error_name
        if error_name == "SslHandshakeFailedError":
            raise ssl.SSLError(detail)
        if error_name in _PROXY_ERROR_NAMES:
            raise DownloadProxyError(detail)
        raise OSError(detail)

    def _reply_error_name(self) -> str:
        return getattr(self._reply.error(), "name", "")

    def _http_status(self) -> int | None:
        status = self._reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute
        )
        return status if type(status) is int else None


def _response_headers(reply: object) -> dict[str, str]:
    # PySide6 6.10 takes rawHeader(str); passing the QByteArray name raises TypeError.
    raw = getattr(reply, "rawHeader")
    names = getattr(reply, "rawHeaderList")()
    headers = {
        key: raw(key).data().decode("latin-1")
        for key in (name.data().decode("latin-1") for name in names)
    }
    content_length_key = next(
        (key for key in headers if key.lower() == "content-length"), None
    )
    if content_length_key is not None:
        headers["Content-Length"] = headers[content_length_key]
        if content_length_key != "Content-Length":
            del headers[content_length_key]
    else:
        content_length = getattr(reply, "header")(
            QNetworkRequest.KnownHeaders.ContentLengthHeader
        )
        if content_length is not None:
            headers["Content-Length"] = str(content_length)
    return headers


def _log_onnxruntime_trace_once() -> None:
    global _ONNXRUNTIME_TRACE_LOGGED
    if not _DOWNLOAD_TRACE or _ONNXRUNTIME_TRACE_LOGGED:
        return
    _ONNXRUNTIME_TRACE_LOGGED = True
    try:
        from matteloop.app import _onnxruntime_distribution

        distribution, version = _onnxruntime_distribution()
    except Exception as error:
        _LOGGER.info(
            "download onnxruntime distribution unavailable error=%s",
            str(error) or type(error).__name__,
        )
        return
    _LOGGER.info(
        "download onnxruntime distribution=%s version=%s", distribution, version
    )
