"""Background worker for normal renders and source-free cut rebuilds."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QObject, Signal, Slot

from matteloop.core.errors import AppError, ErrorCode
from matteloop.core.specs import RenderRequest
from matteloop.core.state import (
    ArtifactResult,
    CancelAcknowledged,
    RenderFailed,
    RenderSucceeded,
)
from matteloop.jobs.context import JobContext, JobTerminalState, ProgressEvent
from matteloop.jobs.render import RenderArtifact
from matteloop.jobs.workspace import CutWorkspace

_LOGGER = logging.getLogger(__name__)


class RenderRuntime(Protocol):
    def render(self, request: RenderRequest, context: JobContext) -> RenderArtifact: ...


class RenderWorker(QObject):
    notification = Signal(object)
    finished = Signal(str)
    provider_ready = Signal(str)
    provider_notice = Signal(str)

    def __init__(
        self,
        job_id: str,
        source_id: str,
        request_id: str,
        request: RenderRequest,
        runtime: RenderRuntime,
        context: JobContext,
        rebuild_workspace: CutWorkspace | None = None,
    ) -> None:
        super().__init__()
        self._job_id = job_id
        self._source_id = source_id
        self._request_id = request_id
        self._request = request
        self._runtime = runtime
        self._context = context
        self._rebuild_workspace = rebuild_workspace

    def emit_progress(self, event: ProgressEvent) -> None:
        self.notification.emit(event)

    @Slot()
    def run(self) -> None:
        started_ns = time.monotonic_ns()
        try:
            provider_emitted = self._prepare_for_render()
            if self._rebuild_workspace is None:
                artifact = self._runtime.render(self._request, self._context)
            else:
                rebuild = getattr(self._runtime, "rebuild", None)
                if not callable(rebuild):
                    raise RuntimeError("render runtime does not support Rebuild")
                artifact = rebuild(
                    self._request, self._rebuild_workspace, self._context
                )
            if not provider_emitted:
                self._emit_provider_details()
            if self._context.terminal_state is JobTerminalState.RUNNING:
                self._context.commit_if_not_cancelled(lambda: None)
            elif self._context.terminal_state is JobTerminalState.CANCEL_PENDING:
                self._context.checkpoint("render")
            output_path = artifact.output_path
            if not isinstance(output_path, Path):
                raise TypeError("render result did not contain an output path")
            provider = getattr(self._runtime, "active_provider", None)
            if not isinstance(provider, str) or not provider:
                provider = self._request.segmentation.execution_provider
            self.notification.emit(
                RenderSucceeded(
                    self._job_id,
                    _artifact_result(
                        self._source_id,
                        self._request_id,
                        self._request,
                        artifact,
                        provider,
                        max(0, (time.monotonic_ns() - started_ns) // 1_000_000),
                    ),
                )
            )
        except BaseException as error:
            self._notify_failure_or_cancel(error)
        finally:
            if self._context.terminal_state is JobTerminalState.RUNNING:
                self._context.fail()
            self.finished.emit(self._job_id)

    def _prepare_for_render(self) -> bool:
        if self._rebuild_workspace is not None:
            return False
        prepare = getattr(self._runtime, "prepare", None)
        if not callable(prepare):
            return False
        prepare(
            self._request.segmentation.model_id,
            {"execution_provider": self._request.segmentation.execution_provider},
            self._context,
        )
        self._emit_provider_details()
        return True

    def _emit_provider_details(self) -> None:
        provider = getattr(
            self._runtime,
            "active_provider",
            self._request.segmentation.execution_provider,
        )
        if not isinstance(provider, str):
            provider = self._request.segmentation.execution_provider
        self.provider_ready.emit(provider)
        notice = getattr(self._runtime, "fallback_notice", None)
        if isinstance(notice, str) and notice:
            self.provider_notice.emit(notice)

    def _notify_failure_or_cancel(self, error: BaseException) -> None:
        if self._context.cancellation.requested:
            if self._context.terminal_state is JobTerminalState.CANCEL_PENDING:
                try:
                    self._context.checkpoint("render")
                except AppError as cancellation:
                    if cancellation.code is not ErrorCode.JOB_CANCELLED:
                        raise
            if self._context.cancellation.acknowledged:
                self.notification.emit(CancelAcknowledged(self._job_id))
                return
        self.notification.emit(
            RenderFailed(
                self._job_id,
                self._source_id,
                self._request_id,
                _render_error(error, self._job_id),
            )
        )


def _render_error(error: BaseException, job_id: str) -> AppError:
    if isinstance(error, AppError):
        # exc_info: the message alone says what failed, the traceback says
        # where, which is what a report from a frozen build needs.
        _LOGGER.error(
            "render job %s failed: %s: %s",
            job_id,
            error.code.value,
            error.technical_detail,
            exc_info=error,
        )
        return error
    _LOGGER.exception("render job %s raised %s", job_id, type(error).__name__)
    return AppError(
        ErrorCode.INVALID_RENDER_REQUEST,
        "render",
        "error.render.failed",
        f"render failed: {error}",
        "retry-render",
        job_id,
    )


def _artifact_result(
    source_id: str,
    request_id: str,
    request: RenderRequest,
    artifact: RenderArtifact,
    provider: str,
    job_duration_ms: int,
) -> ArtifactResult:
    """Copy only the render summary needed after the worker has finished."""
    return ArtifactResult(
        source_id,
        request_id,
        artifact.output_path,
        frame_count=_int_field(getattr(artifact, "frame_count", None)),
        width=_int_field(getattr(artifact, "width", None)),
        height=_int_field(getattr(artifact, "height", None)),
        file_size=_int_field(getattr(artifact, "file_size", None)),
        duration_ms=_int_field(getattr(artifact, "duration_ms", None)),
        output_fps=request.sampling.fps,
        model_id=request.segmentation.model_id,
        execution_provider=provider,
        cuts_reused=_bool_field(
            getattr(artifact, "rebuilt", None),
            default=False,
        ),
        job_duration_ms=job_duration_ms,
    )


def _int_field(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _bool_field(value: object, *, default: bool) -> bool:
    return value if isinstance(value, bool) else default
