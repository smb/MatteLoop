"""One verified local segmentation session."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Protocol

from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.execution_providers import (
    CPU_EXECUTION_PROVIDER,
    is_allowed_provider,
)
from rembggui.jobs.models.cache_fs import (
    BoundDirectoryCloseError,
    BoundModelDirectory,
    UnsafeCacheError,
)
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
    execution_provider: str = CPU_EXECUTION_PROVIDER
    fallback_notice: str | None = None


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
        self._active_provider: str | None = None
        self._active_requested_provider: str | None = None
        self._active_result: PreparationResult | None = None
        self._cleanup_spec: ModelSpec | None = None
        self._cleanup_ids: frozenset[str] = frozenset()
        self._closed = False

    @property
    def active_id(self) -> str | None:
        with self._lock:
            return self._active_spec.id if self._active_spec is not None else None

    @property
    def active_spec(self) -> ModelSpec | None:
        with self._lock:
            return self._active_spec

    @property
    def active_provider(self) -> str | None:
        with self._lock:
            return self._active_provider

    @property
    def active_requested_provider(self) -> str | None:
        with self._lock:
            return self._active_requested_provider

    @property
    def cleanup_pending_id(self) -> str | None:
        with self._lock:
            return self._cleanup_spec.id if self._cleanup_spec is not None else None

    def prepare(self, model_id: str, extras: dict[str, object]) -> PreparationResult:
        with self._lock:
            self._require_open()
            spec = self._catalog.get(model_id)
            if self._cleanup_spec is not None:
                raise _cleanup_pending_error(self._cleanup_spec.id)
            provider = _execution_provider(extras)
            if provider is None:
                raise _preparation_error(
                    "execution provider must be an allowlisted local provider"
                )
            if spec.execution_class is not ExecutionClass.LOCAL:
                raise _preparation_error("model execution class is not supported")
            if (
                self._active_spec == spec
                and self._active_requested_provider == provider
                and self._active_result is not None
            ):
                return self._active_result
            artifact_path = self._downloader.download(
                spec,
                self._cache_root,
                self._progress,
                self._cancelled,
            )
            launch_payload = self._launch_payload(spec, artifact_path, provider)
            if self._client is None:
                client = self._client_factory(launch_payload)
                try:
                    client.start()
                except BaseException as startup_error:
                    self._client = client
                    self._cleanup_spec = spec
                    self._cleanup_ids = frozenset({spec.id})
                    try:
                        client.close()
                    except BaseException as cleanup_error:
                        raise cleanup_error from startup_error
                    self._clear_all_unlocked()
                    raise
                self._client = client
            else:
                client = self._client
                try:
                    client.replace_model(launch_payload)
                except BaseException as replacement_error:
                    previous_spec = self._active_spec
                    self._clear_active_unlocked()
                    self._cleanup_spec = previous_spec
                    self._cleanup_ids = frozenset(
                        {spec.id}
                        | ({previous_spec.id} if previous_spec is not None else set())
                    )
                    try:
                        client.close()
                    except BaseException as cleanup_error:
                        raise cleanup_error from replacement_error
                    self._clear_all_unlocked()
                    raise
            active_provider = _client_provider(client, provider)
            result = PreparationResult(
                model_id=spec.id,
                execution_class=spec.execution_class,
                local_session_ready=True,
                artifact_path=artifact_path,
                execution_provider=active_provider,
                fallback_notice=_client_notice(client),
            )
            self._active_spec = spec
            self._active_provider = active_provider
            self._active_requested_provider = provider
            self._active_result = result
            return result

    def remove(self, model_id: str) -> bool:
        with self._lock:
            spec = self._catalog.get(model_id)
            if (self._active_spec is not None and self._active_spec.id == spec.id) or (
                spec.id in self._cleanup_ids
            ):
                raise AppError(
                    ErrorCode.MODEL_IN_USE,
                    "model-cache",
                    "error.model.in-use",
                    f"model {spec.id!r} owns the active segmentation session",
                    "close-or-switch-model",
                )
            self._require_open()
            artifact = spec.artifact
            if artifact is None:
                return False
            try:
                bound = BoundModelDirectory.bind(
                    self._cache_root,
                    self._catalog.rembg_version,
                    spec.id,
                    create=False,
                )
            except UnsafeCacheError as error:
                raise _unsafe_cache(str(error)) from error
            except PermissionError as error:
                raise _remove_permission(spec.id, error) from error
            except OSError as error:
                raise _remove_disk(spec.id, error) from error
            if bound is None:
                return False
            try:
                with bound:
                    try:
                        _after_remove_directory_bound(bound)
                        removed = bound.unlink_regular(artifact.runtime_filename)
                        bound.assert_still_named()
                        return removed
                    except UnsafeCacheError as error:
                        raise _unsafe_cache(str(error)) from error
                    except PermissionError as error:
                        raise _remove_permission(spec.id, error) from error
                    except OSError as error:
                        raise _remove_disk(spec.id, error) from error
            except BoundDirectoryCloseError as error:
                close_error = error.close_error
                if isinstance(close_error, PermissionError):
                    mapped = _remove_permission(spec.id, close_error)
                else:
                    mapped = _remove_disk(spec.id, close_error)
                cause = (
                    error.primary_error
                    if error.primary_error is not None
                    else close_error
                )
                raise mapped from cause

    def close(self) -> None:
        with self._lock:
            if self._closed and self._client is None:
                return
            self._closed = True
            self._close_active_unlocked()

    def _launch_payload(
        self, spec: ModelSpec, artifact_path: Path, execution_provider: str
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
            "inference_defaults": spec.inference_defaults.to_primitives(),
            "execution_provider": execution_provider,
        }

    def _close_active_unlocked(self) -> None:
        client = self._client
        if self._cleanup_spec is None:
            self._cleanup_spec = self._active_spec
            self._cleanup_ids = (
                frozenset({self._active_spec.id})
                if self._active_spec is not None
                else frozenset()
            )
        self._clear_active_unlocked()
        if client is not None:
            client.close()
        self._clear_all_unlocked()

    def _clear_active_unlocked(self) -> None:
        self._active_spec = None
        self._active_provider = None
        self._active_requested_provider = None
        self._active_result = None

    def _clear_all_unlocked(self) -> None:
        self._client = None
        self._active_spec = None
        self._active_provider = None
        self._active_requested_provider = None
        self._active_result = None
        self._cleanup_spec = None
        self._cleanup_ids = frozenset()

    def _require_open(self) -> None:
        if self._closed:
            raise AppError(
                ErrorCode.MODEL_MANAGER_CLOSED,
                "model-session",
                "error.model.manager-closed",
                "model session manager has already been closed",
                "restart-application",
            )


def _execution_provider(extras: dict[str, object]) -> str | None:
    if type(extras) is not dict or set(extras) - {"execution_provider"}:
        return None
    provider = extras.get("execution_provider", CPU_EXECUTION_PROVIDER)
    return (
        provider
        if isinstance(provider, str) and is_allowed_provider(provider)
        else None
    )


def _client_provider(client: SessionClient, default: str) -> str:
    provider = getattr(client, "effective_provider", default)
    return provider if is_allowed_provider(provider) else default


def _client_notice(client: SessionClient) -> str | None:
    notice = getattr(client, "startup_notice", None)
    return notice if isinstance(notice, str) and notice else None


def _after_remove_directory_bound(_bound: BoundModelDirectory) -> None:
    """Test seam immediately after removal binds its exact model namespace."""


def _preparation_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.MODEL_PREPARATION_INVALID,
        "model-session",
        "error.model.preparation-invalid",
        detail,
        "choose-approved-model-options",
    )


def _cleanup_pending_error(model_id: str) -> AppError:
    return AppError(
        ErrorCode.SEGMENTATION_CLEANUP_FAILED,
        "model-session",
        "error.segmentation.cleanup-failed",
        f"model {model_id!r} still has a cleanup handle; retry close first",
        "retry-segmentation-cleanup",
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
