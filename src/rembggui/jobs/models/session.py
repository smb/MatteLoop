"""One verified local segmentation session and capability-only special models."""

from __future__ import annotations

import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Protocol

from rembggui.core.errors import AppError, ErrorCode
from rembggui.jobs.models.catalog import ExecutionClass, ModelCatalog, ModelSpec
from rembggui.jobs.models.download import CancellationCheck, ProgressCallback


class Downloader(Protocol):
    def download(
        self,
        spec: ModelSpec,
        destination: Path,
        progress: ProgressCallback,
        cancelled: CancellationCheck,
    ) -> Path: ...


class SessionClient(Protocol):
    def start(self) -> None: ...

    def replace_model(self, model_spec: object) -> None: ...

    def close(self) -> None: ...


type ClientFactory = Callable[[dict[str, object]], SessionClient]


@dataclass(frozen=True, slots=True)
class PreparationResult:
    model_id: str
    execution_class: ExecutionClass
    local_session_ready: bool
    artifact_path: Path | None


class ModelSessionManager:
    """Centralize acquisition, safe child projection, replacement, and removal."""

    def __init__(
        self,
        *,
        catalog: ModelCatalog,
        downloader: Downloader,
        client_factory: ClientFactory,
        cache_root: Path,
        progress: ProgressCallback,
        cancelled: CancellationCheck,
    ) -> None:
        if type(catalog) is not ModelCatalog:
            raise TypeError("catalog must be an exact ModelCatalog")
        if not isinstance(cache_root, Path):
            raise TypeError("cache_root must be a Path")
        if (
            not callable(progress)
            or not callable(cancelled)
            or not callable(client_factory)
        ):
            raise TypeError("model manager dependencies must be callable")
        self._catalog = catalog
        self._downloader = downloader
        self._client_factory = client_factory
        self._cache_root = cache_root
        self._progress = progress
        self._cancelled = cancelled
        self._lock = RLock()
        self._client: SessionClient | None = None
        self._active_spec: ModelSpec | None = None
        self._active_result: PreparationResult | None = None
        self._closed = False

    @property
    def active_id(self) -> str | None:
        with self._lock:
            return self._active_spec.id if self._active_spec is not None else None

    @property
    def active_spec(self) -> ModelSpec | None:
        with self._lock:
            return self._active_spec

    def prepare(self, model_id: str, extras: dict[str, object]) -> PreparationResult:
        with self._lock:
            self._require_open()
            if type(extras) is not dict or extras:
                raise _preparation_error(
                    "Task 9 accepts no model paths, custom options, prompts, or tokens"
                )
            spec = self._catalog.get(model_id)
            if spec.execution_class is not ExecutionClass.LOCAL:
                self._close_active_unlocked()
                return PreparationResult(
                    model_id=spec.id,
                    execution_class=spec.execution_class,
                    local_session_ready=False,
                    artifact_path=None,
                )
            if self._active_spec == spec and self._active_result is not None:
                return self._active_result
            artifact_path = self._downloader.download(
                spec,
                self._cache_root,
                self._progress,
                self._cancelled,
            )
            launch_payload = self._launch_payload(spec, artifact_path)
            result = PreparationResult(
                model_id=spec.id,
                execution_class=spec.execution_class,
                local_session_ready=True,
                artifact_path=artifact_path,
            )
            if self._client is None:
                client = self._client_factory(launch_payload)
                try:
                    client.start()
                except BaseException:
                    try:
                        client.close()
                    except BaseException:
                        pass
                    self._clear_active_unlocked()
                    raise
                self._client = client
            else:
                client = self._client
                try:
                    client.replace_model(launch_payload)
                except BaseException:
                    try:
                        client.close()
                    except BaseException:
                        pass
                    self._clear_active_unlocked()
                    raise
            self._active_spec = spec
            self._active_result = result
            return result

    def remove(self, model_id: str) -> bool:
        with self._lock:
            self._require_open()
            spec = self._catalog.get(model_id)
            if self._active_spec is not None and self._active_spec.id == spec.id:
                raise AppError(
                    ErrorCode.MODEL_IN_USE,
                    "model-cache",
                    "error.model.in-use",
                    f"model {spec.id!r} owns the active segmentation session",
                    "close-or-switch-model",
                )
            artifact = spec.artifact
            if artifact is None:
                return False
            target = (
                self._cache_root
                / self._catalog.rembg_version
                / spec.id
                / artifact.runtime_filename
            )
            _validate_remove_target(self._cache_root, target)
            try:
                info = target.lstat()
            except FileNotFoundError:
                return False
            except PermissionError as error:
                raise _remove_permission(spec.id, error) from error
            except OSError as error:
                raise _remove_disk(spec.id, error) from error
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise _unsafe_cache("model removal target is not a real regular file")
            try:
                target.unlink()
                try:
                    target.parent.rmdir()
                except OSError:
                    pass
            except PermissionError as error:
                raise _remove_permission(spec.id, error) from error
            except OSError as error:
                raise _remove_disk(spec.id, error) from error
            return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._close_active_unlocked()

    def _launch_payload(
        self, spec: ModelSpec, artifact_path: Path
    ) -> dict[str, object]:
        artifact = spec.artifact
        assert artifact is not None
        expected = (
            self._cache_root
            / self._catalog.rembg_version
            / spec.id
            / artifact.runtime_filename
        )
        if artifact_path != expected:
            raise _preparation_error(
                "downloader returned a path outside the manifest-bound cache namespace"
            )
        return {
            "schema_version": 1,
            "model_id": spec.id,
            "upstream_id": spec.upstream_id,
            "rembg_version": self._catalog.rembg_version,
            "model_home": str(artifact_path.parent),
            "runtime_filename": artifact.runtime_filename,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
        }

    def _close_active_unlocked(self) -> None:
        client = self._client
        self._clear_active_unlocked()
        if client is not None:
            client.close()

    def _clear_active_unlocked(self) -> None:
        self._client = None
        self._active_spec = None
        self._active_result = None

    def _require_open(self) -> None:
        if self._closed:
            raise AppError(
                ErrorCode.MODEL_MANAGER_CLOSED,
                "model-session",
                "error.model.manager-closed",
                "model session manager has already been closed",
                "restart-application",
            )


def _validate_remove_target(root: Path, target: Path) -> None:
    if not root.exists() and not root.is_symlink():
        return
    try:
        root_info = root.lstat()
    except OSError as error:
        raise _unsafe_cache(
            f"model cache root cannot be inspected: {type(error).__name__}"
        ) from error
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise _unsafe_cache("model cache root is not a real directory")
    root_resolved = root.resolve(strict=True)
    if not target.resolve(strict=False).is_relative_to(root_resolved):
        raise _unsafe_cache("model removal target escapes the cache root")
    current = target.parent
    while current != root:
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _unsafe_cache(
                    "model cache namespace contains an unsafe component"
                )
            if not current.resolve(strict=True).is_relative_to(root_resolved):
                raise _unsafe_cache("model cache namespace escapes the cache root")
        current = current.parent


def _preparation_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.MODEL_PREPARATION_INVALID,
        "model-session",
        "error.model.preparation-invalid",
        detail,
        "choose-approved-model-options",
    )


def _unsafe_cache(detail: str) -> AppError:
    return AppError(
        ErrorCode.MODEL_CACHE_UNSAFE,
        "model-cache",
        "error.model.cache-unsafe",
        detail,
        "choose-safe-cache-location",
    )


def _remove_permission(model_id: str, error: BaseException) -> AppError:
    return AppError(
        ErrorCode.MODEL_DOWNLOAD_PERMISSION,
        "model-cache",
        "error.model.remove-permission",
        f"could not remove model {model_id!r}: {type(error).__name__}",
        "close-model-users-and-retry",
    )


def _remove_disk(model_id: str, error: BaseException) -> AppError:
    return AppError(
        ErrorCode.MODEL_DOWNLOAD_DISK,
        "model-cache",
        "error.model.remove-disk",
        f"could not remove model {model_id!r}: {type(error).__name__}: {error}",
        "retry-model-removal",
    )
