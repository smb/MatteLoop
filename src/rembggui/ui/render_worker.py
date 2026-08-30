"""Background worker for normal renders and source-free cut rebuilds."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QObject, Signal, Slot

from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.specs import RenderRequest
from rembggui.core.state import (
    ArtifactResult,
    CancelAcknowledged,
    RenderFailed,
    RenderSucceeded,
)
from rembggui.jobs.context import JobContext, JobTerminalState, ProgressEvent
from rembggui.jobs.render import RenderArtifact
from rembggui.jobs.workspace import CutWorkspace


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
        try:
            if self._rebuild_workspace is None:
                artifact = self._runtime.render(self._request, self._context)
            else:
                rebuild = getattr(self._runtime, "rebuild", None)
                if not callable(rebuild):
                    raise RuntimeError("render runtime does not support Rebuild")
                artifact = rebuild(
                    self._request, self._rebuild_workspace, self._context
                )
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
            if self._context.terminal_state is JobTerminalState.RUNNING:
                self._context.commit_if_not_cancelled(lambda: None)
            elif self._context.terminal_state is JobTerminalState.CANCEL_PENDING:
                self._context.checkpoint("render")
            output_path = artifact.output_path
            if not isinstance(output_path, Path):
                raise TypeError("render result did not contain an output path")
            self.notification.emit(
                RenderSucceeded(
                    self._job_id,
                    ArtifactResult(self._source_id, self._request_id, output_path),
                )
            )
        except BaseException as error:
            self._notify_failure_or_cancel(error)
        finally:
            if self._context.terminal_state is JobTerminalState.RUNNING:
                self._context.fail()
            self.finished.emit(self._job_id)

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
        return error
    return AppError(
        ErrorCode.INVALID_RENDER_REQUEST,
        "render",
        "error.render.failed",
        f"render failed: {error}",
        "retry-render",
        job_id,
    )
