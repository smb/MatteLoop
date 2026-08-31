"""Production preview runtime and model-session preparation."""

from __future__ import annotations

import time
from fractions import Fraction
from pathlib import Path
from typing import Protocol

import numpy as np

from matteloop.core.errors import AppError, ErrorCode
from matteloop.core.execution_providers import (
    CPU_EXECUTION_PROVIDER,
    ProviderOption,
    provider_options_from_runtime,
)
from matteloop.core.parameters import V1_MODEL_IDS
from matteloop.core.specs import RenderRequest
from matteloop.jobs.context import JobContext
from matteloop.jobs.models.catalog import ModelCatalog
from matteloop.jobs.models.download import ModelDownloader
from matteloop.jobs.models.session import ModelSessionManager
from matteloop.jobs.protocol import SegmentRequest
from matteloop.jobs.render import (
    FilesystemWorkspacePort,
    LocalSourcePort,
    PreparedSegmentation,
    PreviewService,
    RenderArtifact,
    SystemClock,
    find_matching_cut_workspace,
)
from matteloop.jobs.render import PreviewResult as RenderPreviewResult
from matteloop.jobs.segmentation_host import SegmentationClient
from matteloop.jobs.workspace import CutWorkspace
from matteloop.paths import model_cache_root
from matteloop.ui.download_transport import (
    QtNetworkDownloadTransport as _QtNetworkDownloadTransport,
)
from matteloop.ui.source_presentation import (
    DownloadRateEstimator,
    format_model_download_detail,
)


class PreviewRuntime(Protocol):
    """Prepare one model session and execute one render-pipeline preview."""

    def prepare(
        self, model_id: str, extras: dict[str, object], context: JobContext
    ) -> PreparedSegmentation: ...

    def preview(
        self, request: RenderRequest, playhead: Fraction, context: JobContext
    ) -> RenderPreviewResult: ...

    def render(self, request: RenderRequest, context: JobContext) -> RenderArtifact: ...

    def close(self) -> None: ...


class _SessionHolder:
    def __init__(self) -> None:
        self.client: SegmentationClient | None = None
        self.context: JobContext | None = None

    def create(self, payload: dict[str, object]) -> SegmentationClient:
        if self.context is not None:
            self.context.progress(
                "Preparing model",
                0,
                detail="Starting segmentation session",
            )
        self.client = SegmentationClient(payload)
        return self.client


class _NoInferencePort:
    """Satisfy the frozen service contract for a source-free Rebuild."""

    def segment(self, frame: np.ndarray, request: SegmentRequest) -> np.ndarray:
        del frame, request
        raise RuntimeError("Rebuild attempted segmentation")


class ProductionPreviewRuntime:
    """Wire the pinned catalog, session manager, render ports, and real child."""

    def __init__(self, *, cache_root: Path | None = None) -> None:
        self.catalog = ModelCatalog.load_resource()
        self.cache_root = (
            model_cache_root()
            if cache_root is None
            else cache_root
        )
        self._context: JobContext | None = None
        self._prepared: PreparedSegmentation | None = None
        self._prepared_provider = CPU_EXECUTION_PROVIDER
        self._prepared_requested_provider = CPU_EXECUTION_PROVIDER
        self._fallback_notice: str | None = None
        self._download_model_name = ""
        self._download_rate = DownloadRateEstimator()
        self._sessions = _SessionHolder()
        self._manager = ModelSessionManager(
            catalog=self.catalog,
            downloader=ModelDownloader(
                _QtNetworkDownloadTransport(), catalog=self.catalog
            ),
            client_factory=self._sessions.create,
            cache_root=self.cache_root,
            progress=self._download_progress,
            cancelled=self._is_cancelled,
        )

    @property
    def default_model_id(self) -> str:
        return self.catalog.default_id

    @property
    def model_manager(self) -> ModelSessionManager:
        """Expose the session owner to the cache-management dialog."""
        return self._manager

    @property
    def model_options(self) -> tuple[tuple[str, bool], ...]:
        """Expose the V1 picker IDs with truthful local-cache availability."""
        return tuple(
            (
                model_id,
                self._model_cached(model_id),
            )
            for model_id in V1_MODEL_IDS
        )

    @property
    def provider_options(self) -> tuple[ProviderOption, ...]:
        return provider_options_from_runtime(model_id=self.default_model_id)

    @property
    def active_provider(self) -> str:
        return self._prepared_provider

    @property
    def fallback_notice(self) -> str | None:
        return self._fallback_notice

    def _model_cached(self, model_id: str) -> bool:
        artifact = self.catalog.get(model_id).artifact
        return artifact is not None and self._cached(
            model_id, artifact.runtime_filename
        )

    def prepare(
        self, model_id: str, extras: dict[str, object], context: JobContext
    ) -> PreparedSegmentation:
        self._context = context
        self._sessions.context = context
        self._fallback_notice = None
        spec = self.catalog.get(model_id)
        requested_provider = str(
            extras.get("execution_provider", CPU_EXECUTION_PROVIDER)
        )
        if (
            self._manager.active_id != model_id
            or self._manager.active_requested_provider != requested_provider
        ):
            filename = spec.artifact.runtime_filename if spec.artifact else ""
            if self._cached(spec.id, filename):
                context.progress(
                    "Preparing model",
                    0,
                    detail="Using cached model weights",
                )
            else:
                self._download_model_name = spec.display_name
                self._download_rate = DownloadRateEstimator()
                context.progress(
                    "Downloading model",
                    0,
                    detail=format_model_download_detail(spec.display_name),
                )
        else:
            context.progress("Preparing model", 0, detail="Reusing prepared session")
        result = self._manager.prepare(
            model_id,
            {"execution_provider": requested_provider},
        )
        client = self._sessions.client
        artifact = spec.artifact
        if client is None or artifact is None or not result.local_session_ready:
            raise AppError(
                ErrorCode.MODEL_PREPARATION_INVALID,
                "model-session",
                "error.model.preparation-invalid",
                "model preparation did not produce a local session",
                "retry-preview",
                context.job_id,
            )
        self._prepared = PreparedSegmentation(
            client,
            result.model_id,
            artifact.sha256,
            self.catalog.rembg_version,
            frozenset(spec.edge_modes),
        )
        self._prepared_provider = result.execution_provider
        self._prepared_requested_provider = requested_provider
        self._fallback_notice = result.fallback_notice
        if result.fallback_notice is not None:
            context.progress("Preparing model", 0, detail=result.fallback_notice)
        return self._prepared

    def preview(
        self, request: RenderRequest, playhead: Fraction, context: JobContext
    ) -> RenderPreviewResult:
        prepared = self._prepared
        if prepared is None:
            raise AppError(
                ErrorCode.MODEL_PREPARATION_INVALID,
                "preview",
                "error.model.preparation-invalid",
                "preview started without a prepared model session",
                "retry-preview",
                context.job_id,
            )
        return PreviewService(
            source=LocalSourcePort(),
            segmentation=prepared,
            workspace=FilesystemWorkspacePort(),
            clock=SystemClock(),
        ).preview(request, playhead, context)

    def render(self, request: RenderRequest, context: JobContext) -> RenderArtifact:
        prepared = self._prepared
        if (
            prepared is None
            or prepared.model_id != request.segmentation.model_id
            or self._prepared_requested_provider
            != request.segmentation.execution_provider
        ):
            prepared = self.prepare(
                request.segmentation.model_id,
                {"execution_provider": request.segmentation.execution_provider},
                context,
            )
        from matteloop.ui.render_pipeline import render_prepared

        return render_prepared(prepared, request, context)

    def find_matching_workspace(
        self, request: RenderRequest, context: JobContext
    ) -> CutWorkspace | None:
        artifact = self.catalog.get(request.segmentation.model_id).artifact
        if artifact is None:
            return None
        return find_matching_cut_workspace(
            LocalSourcePort(),
            FilesystemWorkspacePort(),
            request,
            model_weight_sha256=artifact.sha256,
            rembg_version=self.catalog.rembg_version,
            context=context,
        )

    def rebuild(
        self,
        request: RenderRequest,
        cut_workspace: CutWorkspace,
        context: JobContext,
    ) -> RenderArtifact:
        prepared = self._prepared
        if (
            prepared is None
            or prepared.model_id != request.segmentation.model_id
        ):
            spec = self.catalog.get(request.segmentation.model_id)
            artifact = spec.artifact
            if artifact is None:
                raise AppError(
                    ErrorCode.MODEL_PREPARATION_INVALID,
                    "rebuild",
                    "error.model.preparation-invalid",
                    "selected model has no downloadable artifact",
                    "retry-render",
                    context.job_id,
                )
            prepared = PreparedSegmentation(
                _NoInferencePort(),
                spec.id,
                artifact.sha256,
                self.catalog.rembg_version,
                frozenset(spec.edge_modes),
            )
        from matteloop.ui.render_pipeline import render_prepared

        return render_prepared(
            prepared, request, context, cut_workspace=cut_workspace
        )

    def close(self) -> None:
        self._manager.close()
        self._prepared = None
        self._prepared_provider = CPU_EXECUTION_PROVIDER
        self._prepared_requested_provider = CPU_EXECUTION_PROVIDER
        self._fallback_notice = None

    def cancel(self, job_id: str) -> None:
        client = self._sessions.client
        if client is not None:
            client.cancel(job_id)

    def _download_progress(self, completed: int, total: int) -> None:
        if self._context is None:
            return
        speed = self._download_rate.update(completed, time.monotonic())
        self._context.progress(
            "Downloading model",
            completed,
            total=total,
            detail=format_model_download_detail(
                self._download_model_name, completed, total, speed
            ),
        )

    def _is_cancelled(self) -> bool:
        return self._context is not None and self._context.cancellation.requested

    def _cached(self, model_id: str, filename: str) -> bool:
        return (
            bool(filename)
            and (
                self.cache_root
                / self.catalog.rembg_version
                / model_id
                / filename
            ).is_file()
        )
