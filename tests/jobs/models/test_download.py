from __future__ import annotations

import hashlib
import json
import ntpath
import os
import ssl
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NoReturn

import pytest

from matteloop.core.errors import AppError, ErrorCode
from matteloop.jobs.models.cache_fs import BoundModelDirectory
from matteloop.jobs.models.catalog import ModelCatalog, ModelSpec
from matteloop.jobs.models.download import (
    DownloadHttpError,
    DownloadProxyError,
    ModelDownloader,
)


class FakeResponse:
    def __init__(
        self,
        data: bytes,
        *,
        headers: Mapping[str, str] | None = None,
        failure: BaseException | None = None,
        close_failure: BaseException | None = None,
        on_read: Callable[[int], None] | None = None,
    ) -> None:
        self._data = data
        self._position = 0
        self.headers = dict(headers or {})
        self.failure = failure
        self.close_failure = close_failure
        self.on_read = on_read
        self.closed = False

    def read(self, size: int) -> bytes:
        if self.on_read is not None:
            self.on_read(self._position)
        if self.failure is not None:
            raise self.failure
        chunk = self._data[self._position : self._position + size]
        self._position += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True
        if self.close_failure is not None:
            raise self.close_failure


class FakeTransport:
    def __init__(
        self,
        response: FakeResponse | None = None,
        *,
        failure: BaseException | None = None,
    ) -> None:
        self.response = response
        self.failure = failure
        self.urls: list[str] = []
        self._lock = threading.Lock()

    def open(self, url: str) -> FakeResponse:
        with self._lock:
            self.urls.append(url)
        if self.failure is not None:
            raise self.failure
        assert self.response is not None
        return self.response


def _catalog_for(data: bytes) -> ModelCatalog:
    payload = json.loads(ModelCatalog.resource_path().read_text(encoding="utf-8"))
    models = payload["models"]
    model = next(item for item in models if item["id"] == "u2net")
    model["artifact"]["size_bytes"] = len(data)
    model["artifact"]["sha256"] = hashlib.sha256(data).hexdigest()
    return ModelCatalog.from_bytes(json.dumps(payload).encode())


def _spec(data: bytes) -> tuple[ModelCatalog, ModelSpec]:
    catalog = _catalog_for(data)
    return catalog, catalog.get("u2net")


def _download(
    tmp_path: Path,
    data: bytes,
    *,
    response: FakeResponse | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    progress: Callable[[int, int], None] = lambda _done, _total: None,
) -> tuple[Path, FakeTransport]:
    catalog, spec = _spec(data)
    transport = FakeTransport(response or FakeResponse(data))
    path = ModelDownloader(transport, catalog=catalog, chunk_size=3).download(
        spec, tmp_path, progress, cancelled
    )
    return path, transport


def test_download_streams_to_versioned_part_verifies_and_atomically_promotes(
    tmp_path: Path,
) -> None:
    data = b"verified-model-bytes"
    events: list[tuple[int, int]] = []
    response = FakeResponse(data, headers={"Content-Length": str(len(data))})

    path, transport = _download(
        tmp_path,
        data,
        response=response,
        progress=lambda done, total: events.append((done, total)),
    )

    assert path == tmp_path / "2.0.72" / "u2net" / "u2net.onnx"
    assert path.read_bytes() == data
    assert not path.with_suffix(".onnx.part").exists()
    assert events[0] == (0, len(data))
    assert events[-1] == (len(data), len(data))
    assert transport.urls == [
        "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx"
    ]
    assert response.closed is True


def test_unknown_content_length_emits_no_byte_progress(tmp_path: Path) -> None:
    events: list[tuple[int, int]] = []

    _download(
        tmp_path, b"abcdef", progress=lambda done, total: events.append((done, total))
    )

    assert events == []


@pytest.mark.parametrize("cancel_after_call", [1, 3, 6])
def test_cancellation_before_between_and_after_native_reads_cleans_part(
    tmp_path: Path, cancel_after_call: int
) -> None:
    data = b"0123456789"
    catalog, spec = _spec(data)
    transport = FakeTransport(FakeResponse(data))
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= cancel_after_call

    with pytest.raises(AppError) as exc:
        ModelDownloader(transport, catalog=catalog, chunk_size=3).download(
            spec, tmp_path, lambda _done, _total: None, cancelled
        )
    assert exc.value.code is ErrorCode.JOB_CANCELLED
    assert not list(tmp_path.rglob("*.part"))


def test_baseexception_from_native_read_closes_response_and_cleans_part(
    tmp_path: Path,
) -> None:
    data = b"bytes"
    catalog, spec = _spec(data)
    response = FakeResponse(data, failure=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        ModelDownloader(FakeTransport(response), catalog=catalog).download(
            spec, tmp_path, lambda _done, _total: None, lambda: False
        )
    assert response.closed is True
    assert not list(tmp_path.rglob("*.part"))


def test_transport_close_failure_is_structured_before_atomic_promotion(
    tmp_path: Path,
) -> None:
    data = b"bytes"
    catalog, spec = _spec(data)
    response = FakeResponse(data, close_failure=OSError("close failed"))

    with pytest.raises(AppError) as exc:
        ModelDownloader(FakeTransport(response), catalog=catalog).download(
            spec, tmp_path, lambda _done, _total: None, lambda: False
        )

    assert exc.value.code is ErrorCode.MODEL_DOWNLOAD_NETWORK
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.rglob("*.onnx"))


def test_checksum_mismatch_never_promotes_part_file(tmp_path: Path) -> None:
    expected = b"expected"
    catalog, spec = _spec(expected)

    with pytest.raises(AppError) as exc:
        ModelDownloader(
            FakeTransport(FakeResponse(b"tampered")), catalog=catalog
        ).download(spec, tmp_path, lambda _done, _total: None, lambda: False)
    assert exc.value.code is ErrorCode.MODEL_CHECKSUM_MISMATCH
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.rglob("*.onnx"))


def test_part_cleanup_failure_is_not_silently_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = b"expected"
    catalog, spec = _spec(expected)
    real_unlink = BoundModelDirectory.unlink_regular

    def deny_part_unlink(bound: BoundModelDirectory, filename: str) -> bool:
        if filename.endswith(".part"):
            raise PermissionError("part is locked")
        return real_unlink(bound, filename)

    monkeypatch.setattr(BoundModelDirectory, "unlink_regular", deny_part_unlink)

    with pytest.raises(AppError) as exc:
        ModelDownloader(
            FakeTransport(FakeResponse(b"tampered")), catalog=catalog
        ).download(spec, tmp_path, lambda _done, _total: None, lambda: False)

    assert exc.value.code is ErrorCode.MODEL_DOWNLOAD_PERMISSION
    assert list(tmp_path.rglob("*.part"))


@pytest.mark.parametrize(
    ("payload", "content_length"),
    [(b"short", None), (b"expected-plus-overflow", None), (b"expected", "999")],
)
def test_size_and_content_length_mismatch_never_promote(
    tmp_path: Path, payload: bytes, content_length: str | None
) -> None:
    expected = b"expected"
    catalog, spec = _spec(expected)
    headers = {} if content_length is None else {"Content-Length": content_length}

    with pytest.raises(AppError) as exc:
        ModelDownloader(
            FakeTransport(FakeResponse(payload, headers=headers)), catalog=catalog
        ).download(spec, tmp_path, lambda _done, _total: None, lambda: False)
    assert exc.value.code is ErrorCode.MODEL_DOWNLOAD_SIZE_MISMATCH
    assert not list(tmp_path.rglob("*.part"))


def test_verified_offline_cache_is_reused_and_invalid_cache_is_not(
    tmp_path: Path,
) -> None:
    data = b"cached"
    catalog, spec = _spec(data)
    path = tmp_path / "2.0.72" / "u2net" / "u2net.onnx"
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    offline = FakeTransport(failure=OSError("offline"))

    assert (
        ModelDownloader(offline, catalog=catalog).download(
            spec, tmp_path, lambda _done, _total: None, lambda: False
        )
        == path
    )
    assert offline.urls == []

    path.write_bytes(b"invalid")
    with pytest.raises(AppError) as exc:
        ModelDownloader(offline, catalog=catalog).download(
            spec, tmp_path, lambda _done, _total: None, lambda: False
        )
    assert exc.value.code is ErrorCode.MODEL_DOWNLOAD_NETWORK
    assert not path.exists()


@pytest.mark.parametrize("reuse_cached", [False, True])
def test_bound_directory_close_oserror_is_structured_for_download_and_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reuse_cached: bool,
) -> None:
    data = b"bound-close"
    catalog, spec = _spec(data)
    target = tmp_path / "2.0.72" / "u2net" / "u2net.onnx"
    if reuse_cached:
        target.parent.mkdir(parents=True)
        target.write_bytes(data)
    real_close = BoundModelDirectory.close

    def close_then_fail(bound: BoundModelDirectory) -> None:
        real_close(bound)
        raise OSError("synthetic CloseHandle failure")

    monkeypatch.setattr(BoundModelDirectory, "close", close_then_fail)

    with pytest.raises(AppError) as exc:
        ModelDownloader(FakeTransport(FakeResponse(data)), catalog=catalog).download(
            spec, tmp_path, lambda _done, _total: None, lambda: False
        )

    assert exc.value.code is ErrorCode.MODEL_DOWNLOAD_DISK
    assert "CloseHandle" in exc.value.technical_detail
    assert target.read_bytes() == data


def test_bound_close_cleanup_failure_preserves_primary_download_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = b"expected"
    catalog, spec = _spec(expected)
    real_close = BoundModelDirectory.close

    def close_then_fail(bound: BoundModelDirectory) -> None:
        real_close(bound)
        raise OSError("synthetic CloseHandle cleanup failure")

    monkeypatch.setattr(BoundModelDirectory, "close", close_then_fail)

    with pytest.raises(AppError) as exc:
        ModelDownloader(
            FakeTransport(FakeResponse(b"tampered")), catalog=catalog
        ).download(spec, tmp_path, lambda _done, _total: None, lambda: False)

    assert exc.value.code is ErrorCode.MODEL_DOWNLOAD_DISK
    assert "CloseHandle" in exc.value.technical_detail
    primary = exc.value.__cause__
    assert isinstance(primary, AppError)
    assert primary.code is ErrorCode.MODEL_CHECKSUM_MISMATCH
    assert not list(tmp_path.rglob("*.part"))


def test_different_manifest_version_never_reuses_old_namespace(tmp_path: Path) -> None:
    data = b"same-bytes"
    old_path = tmp_path / "2.0.71" / "u2net" / "u2net.onnx"
    old_path.parent.mkdir(parents=True)
    old_path.write_bytes(data)
    current_catalog, current_spec = _spec(data)
    response = FakeResponse(data)
    transport = FakeTransport(response)

    path = ModelDownloader(transport, catalog=current_catalog).download(
        current_spec, tmp_path, lambda _done, _total: None, lambda: False
    )

    assert path.parent.parent.name == "2.0.72"
    assert transport.urls
    assert old_path.read_bytes() == data


def test_symlinked_namespace_is_rejected_without_writing_outside_cache(
    tmp_path: Path,
) -> None:
    data = b"model"
    catalog, spec = _spec(data)
    outside = tmp_path / "outside"
    outside.mkdir()
    version = tmp_path / "cache" / "2.0.72"
    version.parent.mkdir()
    version.symlink_to(outside, target_is_directory=True)

    with pytest.raises(AppError) as exc:
        ModelDownloader(FakeTransport(FakeResponse(data)), catalog=catalog).download(
            spec, tmp_path / "cache", lambda _done, _total: None, lambda: False
        )
    assert exc.value.code is ErrorCode.MODEL_CACHE_UNSAFE
    assert list(outside.iterdir()) == []


def test_single_flight_collision_opens_transport_once(tmp_path: Path) -> None:
    data = b"thread-safe-model"
    catalog, spec = _spec(data)
    first_read = threading.Event()
    release = threading.Event()

    def pause_once(position: int) -> None:
        if position == 0 and not first_read.is_set():
            first_read.set()
            assert release.wait(3)

    transport = FakeTransport(FakeResponse(data, on_read=pause_once))
    downloader = ModelDownloader(transport, catalog=catalog, chunk_size=4)
    paths: list[Path] = []

    def run() -> None:
        paths.append(
            downloader.download(
                spec, tmp_path, lambda _done, _total: None, lambda: False
            )
        )

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    assert first_read.wait(3)
    second.start()
    release.set()
    first.join(3)
    second.join(3)

    assert len(paths) == 2
    assert paths[0] == paths[1]
    assert len(transport.urls) == 1


def test_single_flight_key_never_calls_path_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import matteloop.jobs.models.download as download_module

    target = tmp_path / "cache" / "2.0.72" / "u2net" / "u2net.onnx"

    def forbid_resolve(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("single-flight key must not resolve the filesystem")

    monkeypatch.setattr(Path, "resolve", forbid_resolve)

    assert download_module._flight_lock(target) is download_module._flight_lock(target)


def test_single_flight_lock_identity_survives_symlink_namespace_swap(
    tmp_path: Path,
) -> None:
    import matteloop.jobs.models.download as download_module

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    cache_link = tmp_path / "cache-link"
    cache_link.symlink_to(first_root, target_is_directory=True)
    target = cache_link / "2.0.72" / "u2net" / "u2net.onnx"

    before = download_module._flight_lock(target)
    cache_link.unlink()
    cache_link.symlink_to(second_root, target_is_directory=True)
    after = download_module._flight_lock(target)

    assert before is after


def test_single_flight_key_normalizes_lexical_dotdot_and_windows_case(
    tmp_path: Path,
) -> None:
    import matteloop.jobs.models.download as download_module

    direct = tmp_path / "cache" / "2.0.72" / "u2net" / "u2net.onnx"
    equivalent = (
        tmp_path / "cache" / "ignored" / ".." / "2.0.72" / "u2net" / "u2net.onnx"
    )

    assert download_module._flight_lock(direct) is download_module._flight_lock(
        equivalent
    )
    assert download_module._lexical_lock_key(  # type: ignore[attr-defined]
        Path("C:/CACHE/ignored/../U2NET.ONNX"), path_api=ntpath
    ) == ntpath.normcase(ntpath.abspath("C:/CACHE/ignored/../U2NET.ONNX"))


def test_success_fsyncs_file_and_parent_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import matteloop.jobs.models.download as download_module

    calls: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(download_module.os, "fsync", recording_fsync)
    _download(tmp_path, b"durable")

    assert len(calls) >= 2


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (DownloadHttpError(503), ErrorCode.MODEL_DOWNLOAD_HTTP),
        (ssl.SSLError("certificate"), ErrorCode.MODEL_DOWNLOAD_TLS),
        (DownloadProxyError("proxy auth"), ErrorCode.MODEL_DOWNLOAD_PROXY),
        (OSError("network unreachable"), ErrorCode.MODEL_DOWNLOAD_NETWORK),
    ],
)
def test_transport_failures_map_to_structured_errors(
    tmp_path: Path, failure: BaseException, code: ErrorCode
) -> None:
    data = b"payload"
    catalog, spec = _spec(data)

    with pytest.raises(AppError) as exc:
        ModelDownloader(FakeTransport(failure=failure), catalog=catalog).download(
            spec, tmp_path, lambda _done, _total: None, lambda: False
        )
    assert exc.value.code is code
    assert not list(tmp_path.rglob("*.part"))


def test_permission_and_disk_write_failures_are_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import matteloop.jobs.models.download as download_module

    data = b"payload"
    catalog, spec = _spec(data)

    def denied(*_args: object, **_kwargs: object) -> NoReturn:
        raise PermissionError("denied")

    monkeypatch.setattr(download_module, "_open_part", denied)
    with pytest.raises(AppError) as exc:
        ModelDownloader(FakeTransport(FakeResponse(data)), catalog=catalog).download(
            spec, tmp_path, lambda _done, _total: None, lambda: False
        )
    assert exc.value.code is ErrorCode.MODEL_DOWNLOAD_PERMISSION


def test_output_close_oserror_is_structured_and_part_cleanup_remains_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import matteloop.jobs.models.download as download_module

    data = b"payload"
    catalog, spec = _spec(data)
    real_open = download_module._open_part

    class CloseFailingOutput:
        def __init__(self, output: object) -> None:
            self._output = output

        def __getattr__(self, name: str) -> object:
            return getattr(self._output, name)

        def close(self) -> None:
            self._output.close()  # type: ignore[attr-defined]
            raise OSError("synthetic output close failure")

    def close_failing(bound: BoundModelDirectory, filename: str) -> CloseFailingOutput:
        return CloseFailingOutput(real_open(bound, filename))

    monkeypatch.setattr(download_module, "_open_part", close_failing)

    with pytest.raises(AppError) as exc:
        ModelDownloader(FakeTransport(FakeResponse(data)), catalog=catalog).download(
            spec, tmp_path, lambda _done, _total: None, lambda: False
        )

    assert exc.value.code is ErrorCode.MODEL_DOWNLOAD_DISK
    assert "close" in exc.value.technical_detail
    assert not list(tmp_path.rglob("*.part"))


def _swap_bound_parent(bound: BoundModelDirectory, outside: Path) -> tuple[bool, Path]:
    held = bound.path.with_name(f"{bound.path.name}-held")
    try:
        bound.path.rename(held)
        bound.path.symlink_to(outside, target_is_directory=True)
    except OSError:
        if held.exists() and not bound.path.exists():
            held.rename(bound.path)
        return False, held
    return True, held


def test_parent_swap_cannot_redirect_cached_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import matteloop.jobs.models.download as download_module

    data = b"cached"
    catalog, spec = _spec(data)
    cached = tmp_path / "2.0.72" / "u2net" / "u2net.onnx"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(data)
    outside = tmp_path / "outside-reuse"
    outside.mkdir()
    outside_file = outside / "u2net.onnx"
    outside_file.write_bytes(b"outside")
    swapped = False

    def swap(bound: BoundModelDirectory) -> None:
        nonlocal swapped
        swapped, _held = _swap_bound_parent(bound, outside)

    monkeypatch.setattr(download_module, "_after_model_directory_bound", swap)

    try:
        result = ModelDownloader(
            FakeTransport(failure=OSError("offline")), catalog=catalog
        ).download(spec, tmp_path, lambda _done, _total: None, lambda: False)
    except AppError as error:
        assert swapped is True
        assert error.code is ErrorCode.MODEL_CACHE_UNSAFE
    else:
        assert swapped is False
        assert result.read_bytes() == data
    assert outside_file.read_bytes() == b"outside"


def test_parent_swap_cannot_redirect_part_promotion_or_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import matteloop.jobs.models.download as download_module

    data = b"verified"
    catalog, spec = _spec(data)
    outside = tmp_path / "outside-promotion"
    outside.mkdir()
    outside_part = outside / "u2net.onnx.part"
    outside_part.write_bytes(b"outside-part")
    outside_final = outside / "u2net.onnx"
    outside_final.write_bytes(b"outside-final")
    swapped = False

    def swap(bound: BoundModelDirectory) -> None:
        nonlocal swapped
        swapped, _held = _swap_bound_parent(bound, outside)

    monkeypatch.setattr(download_module, "_before_model_promotion", swap)

    try:
        ModelDownloader(FakeTransport(FakeResponse(data)), catalog=catalog).download(
            spec, tmp_path, lambda _done, _total: None, lambda: False
        )
    except AppError as error:
        assert swapped is True
        assert error.code is ErrorCode.MODEL_CACHE_UNSAFE
    else:
        assert swapped is False
    assert outside_part.read_bytes() == b"outside-part"
    assert outside_final.read_bytes() == b"outside-final"


def test_parent_swap_cannot_redirect_failed_download_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import matteloop.jobs.models.download as download_module

    expected = b"expected"
    catalog, spec = _spec(expected)
    outside = tmp_path / "outside-cleanup"
    outside.mkdir()
    outside_part = outside / "u2net.onnx.part"
    outside_part.write_bytes(b"outside-part")
    swapped = False

    def swap(bound: BoundModelDirectory) -> None:
        nonlocal swapped
        swapped, _held = _swap_bound_parent(bound, outside)

    monkeypatch.setattr(download_module, "_after_model_directory_bound", swap)

    with pytest.raises(AppError) as exc:
        ModelDownloader(
            FakeTransport(FakeResponse(b"tampered")), catalog=catalog
        ).download(spec, tmp_path, lambda _done, _total: None, lambda: False)

    assert swapped is (os.name != "nt")
    assert exc.value.code is ErrorCode.MODEL_CHECKSUM_MISMATCH
    assert outside_part.read_bytes() == b"outside-part"
