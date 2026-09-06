"""Preview worker and progress/cancellation bridge."""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QImage

from matteloop.core.errors import AppError, ErrorCode
from matteloop.core.state import (
    CancelAcknowledged,
    JobStageChanged,
    ModelPrepared,
    PreviewFailed,
    PreviewSucceeded,
)
from matteloop.core.state import PreviewResult as StatePreviewResult
from matteloop.core.tokens import ProgressStage
from matteloop.jobs.context import JobContext, JobTerminalState, ProgressEvent
from matteloop.jobs.render import ImmutableRgba
from matteloop.ui.preview_controller.request_assembly import (
    _PreviewInputs,
    _render_request,
)
from matteloop.ui.preview_controller.runtime import PreviewRuntime

_LOGGER = logging.getLogger(__name__)


class _PreviewWorker(QObject):
    notification = Signal(object)
    finished = Signal(str)
    provider_ready = Signal(str)
    provider_notice = Signal(str)

    def __init__(
        self,
        job_id: str,
        source_id: str,
        request_id: str,
        inputs: _PreviewInputs,
        runtime: PreviewRuntime,
        context: JobContext,
    ) -> None:
        super().__init__()
        self._job_id = job_id
        self._source_id = source_id
        self._request_id = request_id
        self._inputs = inputs
        self._runtime = runtime
        self._context = context

    def emit_progress(self, event: ProgressEvent) -> None:
        self.notification.emit(event)

    @Slot()
    def run(self) -> None:
        try:
            self._emit_stage(ProgressStage.PREPARING_MODEL)
            request = _render_request(self._inputs)
            prepared = self._runtime.prepare(
                request.segmentation.model_id,
                {"execution_provider": request.segmentation.execution_provider},
                self._context,
            )
            del prepared
            provider = getattr(
                self._runtime,
                "active_provider",
                request.segmentation.execution_provider,
            )
            if not isinstance(provider, str):
                provider = request.segmentation.execution_provider
            self.provider_ready.emit(provider)
            notice = getattr(self._runtime, "fallback_notice", None)
            if isinstance(notice, str) and notice:
                self.provider_notice.emit(notice)
            self._context.checkpoint("model-preparation")
            self.notification.emit(
                ModelPrepared(
                    self._job_id,
                    self._source_id,
                    self._request_id,
                    ProgressStage.SEGMENTATION,
                )
            )
            self._emit_stage(ProgressStage.SEGMENTATION)
            result = self._runtime.preview(
                request, self._inputs.playhead, self._context
            )
            if self._context.terminal_state is JobTerminalState.RUNNING:
                self._context.commit_if_not_cancelled(lambda: None)
            elif self._context.terminal_state is JobTerminalState.CANCEL_PENDING:
                self._context.checkpoint("preview")
            value = _qimage_from_rgba(result.display_rgba)
            self.notification.emit(
                PreviewSucceeded(
                    self._job_id,
                    StatePreviewResult(self._source_id, self._request_id, value),
                )
            )
        except BaseException as error:
            self._notify_failure_or_cancel(error)
        finally:
            if self._context.terminal_state is JobTerminalState.RUNNING:
                self._context.fail()
            self.finished.emit(self._job_id)

    def _emit_stage(self, stage: ProgressStage) -> None:
        self.notification.emit(
            JobStageChanged(
                self._job_id,
                self._source_id,
                self._request_id,
                stage,
            )
        )

    def _notify_failure_or_cancel(self, error: BaseException) -> None:
        if self._context.cancellation.requested:
            if self._context.terminal_state is JobTerminalState.CANCEL_PENDING:
                try:
                    self._context.checkpoint("preview")
                except AppError as cancellation:
                    if cancellation.code is not ErrorCode.JOB_CANCELLED:
                        raise
            if self._context.cancellation.acknowledged:
                self.notification.emit(CancelAcknowledged(self._job_id))
                return
        self.notification.emit(
            PreviewFailed(
                self._job_id,
                self._source_id,
                self._request_id,
                _preview_error(error, self._job_id),
            )
        )

def _qimage_from_rgba(value: ImmutableRgba) -> QImage:
    if not isinstance(value, ImmutableRgba):
        raise TypeError("preview result did not contain immutable RGBA pixels")
    image = value.to_image()
    try:
        raw = image.tobytes()
        return QImage(
            raw,
            image.width,
            image.height,
            image.width * 4,
            QImage.Format.Format_RGBA8888,
        ).copy()
    finally:
        image.close()


def _notification_job_id(notification: object) -> str | None:
    if isinstance(notification, ProgressEvent):
        return notification.job_id
    return getattr(notification, "job_id", None)


def _preview_error(error: BaseException, job_id: str) -> AppError:
    if isinstance(error, AppError):
        _LOGGER.error(
            "preview job %s failed: %s: %s",
            job_id,
            error.code.value,
            error.technical_detail,
            exc_info=error,
        )
        return error
    _LOGGER.exception("preview job %s raised %s", job_id, type(error).__name__)
    return AppError(
        ErrorCode.INVALID_RENDER_REQUEST,
        "preview",
        "error.preview.failed",
        f"preview failed: {error}",
        "retry-preview",
        job_id,
    )


def _cancel_runtime(cancel: object, job_id: str) -> None:
    try:
        cancel(job_id)  # type: ignore[operator]
    except BaseException:
        return
