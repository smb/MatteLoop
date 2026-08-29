"""Verified, bounded, atomic acquisition for manifest-bound model artifacts."""

from __future__ import annotations

import errno
import hashlib
import os
import ssl
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Lock
from typing import NoReturn, Protocol, runtime_checkable

from rembggui.core.errors import AppError, ErrorCode
from rembggui.jobs.models.catalog import (
    ExecutionClass,
    ModelArtifact,
    ModelCatalog,
    ModelSpec,
)

_DEFAULT_CHUNK_SIZE = 256 * 1024
_MAX_CHUNK_SIZE = 1024 * 1024
_flight_guard = Lock()
_flight_locks: dict[str, Lock] = {}

type ProgressCallback = Callable[[int, int], None]
type CancellationCheck = Callable[[], bool]


@runtime_checkable
class DownloadResponse(Protocol):
    headers: Mapping[str, str]

    def read(self, size: int) -> bytes: ...

    def close(self) -> None: ...


@runtime_checkable
class DownloadTransport(Protocol):
    def open(self, url: str) -> DownloadResponse: ...


class DownloadHttpError(Exception):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"HTTP status {status}")


class DownloadProxyError(Exception):
    pass


class ModelDownloader:
    """Download exactly one canonical catalog artifact with single-flight locking."""

    def __init__(
        self,
        transport: DownloadTransport,
        *,
        catalog: ModelCatalog | None = None,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
    ) -> None:
        if not isinstance(transport, DownloadTransport):
            raise TypeError("transport must implement the bounded download protocol")
        if type(chunk_size) is not int or not 1 <= chunk_size <= _MAX_CHUNK_SIZE:
            raise ValueError("chunk_size must be between 1 and 1048576 bytes")
        self._transport = transport
        self._catalog = catalog if catalog is not None else ModelCatalog.load_resource()
        self._chunk_size = chunk_size

    def download(
        self,
        spec: ModelSpec,
        destination: Path,
        progress: ProgressCallback,
        cancelled: CancellationCheck,
    ) -> Path:
        trusted = self._trusted_local_spec(spec)
        if not isinstance(destination, Path):
            raise TypeError("destination must be a Path")
        if not callable(progress) or not callable(cancelled):
            raise TypeError("progress and cancelled must be callable")
        artifact = trusted.artifact
        assert artifact is not None
        lexical_target = (
            destination
            / self._catalog.rembg_version
            / trusted.id
            / artifact.runtime_filename
        )
        lock = _flight_lock(lexical_target)
        with lock:
            try:
                target = _prepare_target(
                    destination,
                    self._catalog.rembg_version,
                    trusted.id,
                    artifact.runtime_filename,
                )
            except AppError:
                raise
            except PermissionError as error:
                raise _permission_error(trusted.id, error) from error
            except OSError as error:
                raise _disk_error(trusted.id, error) from error
            return self._download_locked(trusted, artifact, target, progress, cancelled)

    def _trusted_local_spec(self, spec: ModelSpec) -> ModelSpec:
        if type(spec) is not ModelSpec:
            raise _unsafe_error("download request did not contain an exact ModelSpec")
        trusted = self._catalog.get(spec.id)
        if trusted != spec:
            raise _unsafe_error(
                "download spec does not match the active pinned catalog"
            )
        if (
            trusted.execution_class is not ExecutionClass.LOCAL
            or trusted.artifact is None
        ):
            raise _unsafe_error("only local manifest artifacts can be downloaded")
        return trusted

    def _download_locked(
        self,
        spec: ModelSpec,
        artifact: ModelArtifact,
        target: Path,
        progress: ProgressCallback,
        cancelled: CancellationCheck,
    ) -> Path:
        part = target.with_name(f"{target.name}.part")
        _raise_if_cancelled(cancelled, spec.id)
        if target.exists() or target.is_symlink():
            if _verified_file(target, artifact, cancelled, spec.id):
                return target
            _safe_unlink(target, spec.id)
        _remove_stale_part(part, spec.id)
        response: DownloadResponse | None = None
        try:
            response = self._open_response(artifact.url, spec.id)
            _raise_if_cancelled(cancelled, spec.id)
            known_total = _content_length(response.headers, spec.id)
            if known_total is not None and known_total != artifact.size_bytes:
                raise _size_error(
                    spec.id,
                    f"HTTP Content-Length {known_total} does not match manifest size "
                    f"{artifact.size_bytes}",
                )
            if known_total is not None:
                progress(0, known_total)
            digest = hashlib.sha256()
            completed = 0
            try:
                output = _open_part(part)
            except PermissionError as error:
                raise _permission_error(spec.id, error) from error
            except OSError as error:
                raise _disk_error(spec.id, error) from error
            try:
                while True:
                    _raise_if_cancelled(cancelled, spec.id)
                    chunk = self._read_response(response, spec.id)
                    _raise_if_cancelled(cancelled, spec.id)
                    if not chunk:
                        break
                    if len(chunk) > self._chunk_size:
                        raise _network_error(
                            spec.id, "transport returned an over-sized native chunk"
                        )
                    completed += len(chunk)
                    if completed > artifact.size_bytes:
                        raise _size_error(
                            spec.id, "download exceeded the manifest byte size"
                        )
                    try:
                        output.write(chunk)
                    except PermissionError as error:
                        raise _permission_error(spec.id, error) from error
                    except OSError as error:
                        raise _disk_error(spec.id, error) from error
                    digest.update(chunk)
                    if known_total is not None:
                        progress(completed, known_total)
                _raise_if_cancelled(cancelled, spec.id)
                if completed != artifact.size_bytes:
                    raise _size_error(
                        spec.id,
                        f"download ended at {completed} of {artifact.size_bytes} bytes",
                    )
                if digest.hexdigest() != artifact.sha256:
                    raise _checksum_error(spec.id, self._catalog.rembg_version)
                try:
                    output.flush()
                    os.fsync(output.fileno())
                    part_identity = os.fstat(output.fileno())
                except PermissionError as error:
                    raise _permission_error(spec.id, error) from error
                except OSError as error:
                    raise _disk_error(spec.id, error) from error
            finally:
                output.close()
            _raise_if_cancelled(cancelled, spec.id)
            self._close_response(response, spec.id)
            response = None
            _validate_part_identity(part, part_identity, spec.id)
            if target.exists() or target.is_symlink():
                if _verified_file(target, artifact, cancelled, spec.id):
                    _safe_unlink(part, spec.id)
                    return target
                _safe_unlink(target, spec.id)
            try:
                os.replace(part, target)
                _fsync_directory(target.parent)
            except PermissionError as error:
                raise _permission_error(spec.id, error) from error
            except OSError as error:
                raise _disk_error(spec.id, error) from error
            return target
        except BaseException as error:
            try:
                _cleanup_part(part, spec.id)
            except AppError as cleanup_error:
                raise cleanup_error from error
            raise
        finally:
            if response is not None:
                try:
                    response.close()
                except BaseException:
                    pass

    def _open_response(self, url: str, model_id: str) -> DownloadResponse:
        try:
            response = self._transport.open(url)
        except BaseException as error:
            _raise_transport_error(model_id, error)
        if not isinstance(response, DownloadResponse):
            try:
                response.close()
            except BaseException:
                pass
            raise _network_error(model_id, "transport returned an invalid response")
        return response

    def _read_response(self, response: DownloadResponse, model_id: str) -> bytes:
        try:
            chunk = response.read(self._chunk_size)
        except BaseException as error:
            _raise_transport_error(model_id, error)
        if type(chunk) is not bytes:
            raise _network_error(model_id, "transport returned a non-bytes chunk")
        return chunk

    @staticmethod
    def _close_response(response: DownloadResponse, model_id: str) -> None:
        try:
            response.close()
        except BaseException as error:
            _raise_transport_error(model_id, error)


def _prepare_target(root: Path, version: str, model_id: str, filename: str) -> Path:
    _ensure_directory(root)
    root_resolved = root.resolve(strict=True)
    if root.is_symlink():
        raise _unsafe_error("model cache root cannot be a symbolic link")
    version_dir = root / version
    _ensure_directory(version_dir)
    model_dir = version_dir / model_id
    _ensure_directory(model_dir)
    target = model_dir / filename
    resolved = target.resolve(strict=False)
    if not resolved.is_relative_to(root_resolved):
        raise _unsafe_error("model artifact target escapes the cache root")
    for directory in (version_dir, model_dir):
        if directory.is_symlink():
            raise _unsafe_error("model cache namespace contains an unsafe directory")
        if not directory.resolve(strict=True).is_relative_to(root_resolved):
            raise _unsafe_error("model cache namespace escapes the cache root")
    return target


def _ensure_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise _unsafe_error(
                f"cache component {path.name!r} is not a real directory"
            )
        return
    path.mkdir(mode=0o700)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _unsafe_error(f"cache component {path.name!r} is not a real directory")


def _flight_lock(target: Path) -> Lock:
    key = os.path.normcase(str(target.resolve(strict=False)))
    with _flight_guard:
        lock = _flight_locks.get(key)
        if lock is None:
            lock = Lock()
            _flight_locks[key] = lock
        return lock


def _open_part(path: Path):  # type: ignore[no-untyped-def]
    return path.open("xb")


def _remove_stale_part(part: Path, model_id: str) -> None:
    if not part.exists() and not part.is_symlink():
        return
    _safe_unlink(part, model_id)


def _validate_part_identity(
    part: Path, expected: os.stat_result, model_id: str
) -> None:
    try:
        current = part.lstat()
    except PermissionError as error:
        raise _permission_error(model_id, error) from error
    except OSError as error:
        raise _disk_error(model_id, error) from error
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or current.st_dev != expected.st_dev
        or current.st_ino != expected.st_ino
        or current.st_size != expected.st_size
    ):
        raise _unsafe_error("verified model part changed before atomic promotion")


def _safe_unlink(path: Path, model_id: str) -> None:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise _unsafe_error(
                f"model cache target {path.name!r} is not a regular file"
            )
        path.unlink()
    except FileNotFoundError:
        return
    except AppError:
        raise
    except PermissionError as error:
        raise _permission_error(model_id, error) from error
    except OSError as error:
        raise _disk_error(model_id, error) from error


def _cleanup_part(part: Path, model_id: str) -> None:
    _safe_unlink(part, model_id)


def _verified_file(
    path: Path,
    artifact: ModelArtifact,
    cancelled: CancellationCheck,
    model_id: str,
) -> bool:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise _disk_error(model_id, error) from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise _unsafe_error("cached model is not a real regular file")
    if before.st_size != artifact.size_bytes:
        return False
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except PermissionError as error:
        raise _permission_error(model_id, error) from error
    except OSError as error:
        raise _disk_error(model_id, error) from error
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != artifact.size_bytes
        ):
            return False
        while True:
            _raise_if_cancelled(cancelled, model_id)
            chunk = os.read(descriptor, _DEFAULT_CHUNK_SIZE)
            _raise_if_cancelled(cancelled, model_id)
            if not chunk:
                break
            digest.update(chunk)
    except PermissionError as error:
        raise _permission_error(model_id, error) from error
    except OSError as error:
        raise _disk_error(model_id, error) from error
    finally:
        os.close(descriptor)
    try:
        after = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise _disk_error(model_id, error) from error
    if (
        after.st_dev != opened.st_dev
        or after.st_ino != opened.st_ino
        or after.st_size != artifact.size_bytes
    ):
        return False
    return digest.hexdigest() == artifact.sha256


def _content_length(headers: Mapping[str, str], model_id: str) -> int | None:
    if not isinstance(headers, Mapping):
        raise _network_error(model_id, "response headers are not a mapping")
    values = [
        value
        for key, value in headers.items()
        if type(key) is str and key.lower() == "content-length"
    ]
    if not values:
        return None
    if len(values) != 1 or type(values[0]) is not str or not values[0].isdigit():
        raise _network_error(model_id, "HTTP Content-Length is malformed")
    total = int(values[0])
    if total <= 0:
        raise _network_error(model_id, "HTTP Content-Length is not positive")
    return total


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(directory, flags)
    except OSError as error:
        if error.errno in {errno.EINVAL, errno.ENOTSUP, errno.EBADF, errno.EACCES}:
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EBADF}:
                raise
    finally:
        os.close(descriptor)


def _raise_if_cancelled(cancelled: CancellationCheck, model_id: str) -> None:
    if cancelled():
        raise AppError(
            ErrorCode.JOB_CANCELLED,
            "model-download",
            "error.job.cancelled",
            f"model download for {model_id!r} was cancelled",
            "retry-model-download",
        )


def _raise_transport_error(model_id: str, error: BaseException) -> NoReturn:
    if isinstance(error, AppError):
        raise error
    if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
        raise error
    if isinstance(error, DownloadHttpError):
        raise AppError(
            ErrorCode.MODEL_DOWNLOAD_HTTP,
            "model-download",
            "error.model.download-http",
            f"model {model_id!r} server returned HTTP {error.status}",
            "retry-model-download",
        ) from error
    if isinstance(error, ssl.SSLError):
        raise AppError(
            ErrorCode.MODEL_DOWNLOAD_TLS,
            "model-download",
            "error.model.download-tls",
            f"TLS verification failed for model {model_id!r}: {type(error).__name__}",
            "check-system-trust-and-retry",
        ) from error
    if isinstance(error, DownloadProxyError):
        raise AppError(
            ErrorCode.MODEL_DOWNLOAD_PROXY,
            "model-download",
            "error.model.download-proxy",
            f"system proxy failed for model {model_id!r}",
            "check-system-proxy-and-retry",
        ) from error
    raise _network_error(
        model_id, f"transport failed: {type(error).__name__}: {error}"
    ) from error


def _unsafe_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.MODEL_CACHE_UNSAFE,
        "model-cache",
        "error.model.cache-unsafe",
        detail,
        "choose-safe-cache-location",
    )


def _size_error(model_id: str, detail: str) -> AppError:
    return AppError(
        ErrorCode.MODEL_DOWNLOAD_SIZE_MISMATCH,
        "model-download",
        "error.model.download-size-mismatch",
        f"model {model_id!r}: {detail}",
        "retry-model-download",
    )


def _checksum_error(model_id: str, version: str) -> AppError:
    return AppError(
        ErrorCode.MODEL_CHECKSUM_MISMATCH,
        "model-download",
        "error.model.checksum-mismatch",
        f"model {model_id!r} for rembg {version} failed SHA-256 verification",
        "retry-model-download",
    )


def _network_error(model_id: str, detail: str) -> AppError:
    return AppError(
        ErrorCode.MODEL_DOWNLOAD_NETWORK,
        "model-download",
        "error.model.download-network",
        f"model {model_id!r}: {detail}",
        "check-network-and-retry",
    )


def _permission_error(model_id: str, error: BaseException) -> AppError:
    return AppError(
        ErrorCode.MODEL_DOWNLOAD_PERMISSION,
        "model-download",
        "error.model.download-permission",
        f"model {model_id!r} cache permission failed: {type(error).__name__}",
        "choose-writable-cache-location",
    )


def _disk_error(model_id: str, error: BaseException) -> AppError:
    return AppError(
        ErrorCode.MODEL_DOWNLOAD_DISK,
        "model-download",
        "error.model.download-disk",
        f"model {model_id!r} cache write failed: {type(error).__name__}: {error}",
        "free-disk-space-and-retry",
    )
