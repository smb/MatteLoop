"""Background preview orchestration and its exclusive modal progress dialog."""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from threading import Thread
from typing import Protocol
from uuid import uuid4

from platformdirs import user_cache_dir
from PySide6.QtCore import QEvent, QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QImage, QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.specs import (
    CollisionPolicy,
    CropSpec,
    EdgeMode,
    FramingSpec,
    OutputSpec,
    RenderRequest,
    SamplingSpec,
    SegmentationSpec,
)
from rembggui.core.state import (
    CancelAcknowledged,
    CancelRequested,
    FocusTarget,
    JobKind,
    JobStageChanged,
    JobState,
    ModelPrepared,
    PreviewFailed,
    PreviewRequested,
    PreviewSucceeded,
    SourceState,
    capabilities,
)
from rembggui.core.state import PreviewResult as StatePreviewResult
from rembggui.core.timeline import TimelineState
from rembggui.jobs.context import (
    CancellationState,
    ExclusiveJobScheduler,
    JobContext,
    JobTerminalState,
    ProgressEvent,
)
from rembggui.jobs.models.catalog import ModelCatalog
from rembggui.jobs.models.download import ModelDownloader
from rembggui.jobs.models.session import ModelSessionManager
from rembggui.jobs.render import (
    FilesystemWorkspacePort,
    ImmutableRgba,
    LocalSourcePort,
    PreparedSegmentation,
    PreviewService,
    RenderArtifact,
    SystemClock,
)
from rembggui.jobs.render import PreviewResult as RenderPreviewResult
from rembggui.jobs.segmentation_host import SegmentationClient
from rembggui.ui.download_transport import (
    QtNetworkDownloadTransport as _QtNetworkDownloadTransport,
)
from rembggui.ui.ports import PreviewFrameRequested, StateStore
from rembggui.ui.source_presentation import (
    DownloadRateEstimator,
    format_model_download_detail,
    format_model_download_progress,
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


class ProductionPreviewRuntime:
    """Wire the pinned catalog, session manager, render ports, and real child."""

    def __init__(self, *, cache_root: Path | None = None) -> None:
        self.catalog = ModelCatalog.load_resource()
        self.cache_root = (
            Path(user_cache_dir("rembggui")) / "models"
            if cache_root is None
            else cache_root
        )
        self._context: JobContext | None = None
        self._prepared: PreparedSegmentation | None = None
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

    def prepare(
        self, model_id: str, extras: dict[str, object], context: JobContext
    ) -> PreparedSegmentation:
        self._context = context
        self._sessions.context = context
        spec = self.catalog.get(model_id)
        if self._manager.active_id != model_id:
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
        result = self._manager.prepare(model_id, extras)
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
        if prepared is None or prepared.model_id != request.segmentation.model_id:
            prepared = self.prepare(request.segmentation.model_id, {}, context)
        from rembggui.ui.render_pipeline import render_prepared

        return render_prepared(prepared, request, context)

    def close(self) -> None:
        self._manager.close()

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


class PreviewJobDialog(QDialog):
    """One reusable, non-blocking, application-modal job dialog."""

    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("preview_job_dialog")
        self.setAccessibleName("Preview job")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setModal(True)
        self.stage_label = QLabel()
        self.stage_label.setObjectName("job_stage")
        self.detail_label = QLabel()
        self.detail_label.setObjectName("job_detail")
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("job_progress")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("job_cancel")
        layout = QVBoxLayout(self)
        layout.addWidget(self.stage_label)
        layout.addWidget(self.detail_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.cancel_button)
        self.cancel_button.clicked.connect(self._request_cancel)
        self.installEventFilter(self)
        self.cancel_button.installEventFilter(self)
        self.reset()

    def reset(self, title: str = "Previewing selected frame") -> None:
        self._terminal_close_requested = False
        self.setWindowTitle(title)
        self.stage_label.setText("Preparing model")
        self.detail_label.setText("")
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("")
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Cancel")
        self._cancel_emitted = False

    def set_progress(self, event: ProgressEvent) -> None:
        self.stage_label.setText(event.stage)
        self.detail_label.setText(event.detail)
        if event.total is None:
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat("")
        else:
            self.progress_bar.setRange(0, event.total)
            self.progress_bar.setValue(event.completed)
            if event.stage == "Downloading model":
                self.progress_bar.setFormat(
                    format_model_download_progress(event.completed, event.total)
                )
            elif event.stage == "Decode":
                self.progress_bar.setFormat("%v / %m frames")
            else:
                self.progress_bar.setFormat("%v / %m")

    def set_cancelling(self) -> None:
        self.stage_label.setText("Cancelling…")
        self.detail_label.setText("Waiting for the current safe checkpoint…")
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("")
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancelling…")

    def close_for_terminal(self) -> None:
        self._terminal_close_requested = True
        self.done(0)
        self._terminal_close_requested = False

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if not self._terminal_close_requested:
            event.ignore()
            return
        self._terminal_close_requested = False
        super().closeEvent(event)

    def reject(self) -> None:
        if self._terminal_close_requested:
            super().reject()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            if event.key() == Qt.Key.Key_Escape:
                self._request_cancel()
                return True
        return super().eventFilter(watched, event)

    @Slot()
    def _request_cancel(self) -> None:
        if self._cancel_emitted:
            return
        self._cancel_emitted = True
        self.cancel_requested.emit()


@dataclass(frozen=True)
class _PreviewInputs:
    source: Path
    width: int
    height: int
    duration: Fraction
    start: Fraction
    end: Fraction
    playhead: Fraction


class _PreviewWorker(QObject):
    notification = Signal(object)
    finished = Signal(str)

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
            self._emit_stage("Preparing model")
            request = _render_request(
                self._inputs,
                getattr(self._runtime, "default_model_id", "birefnet-portrait"),
            )
            prepared = self._runtime.prepare(
                request.segmentation.model_id, {}, self._context
            )
            del prepared
            self._context.checkpoint("model-preparation")
            self.notification.emit(
                ModelPrepared(
                    self._job_id,
                    self._source_id,
                    self._request_id,
                    "Segmentation",
                )
            )
            self._emit_stage("Segmentation")
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

    def _emit_stage(self, stage: str) -> None:
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


class PreviewController(QObject):
    """Run previews away from Qt widgets while owning their job dialog."""

    def __init__(
        self,
        store: StateStore,
        *,
        runtime: PreviewRuntime | None = None,
        dialog_parent: QWidget | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._runtime = runtime or ProductionPreviewRuntime()
        self._dialog_parent = dialog_parent
        self._scheduler = ExclusiveJobScheduler()
        self._contexts: dict[str, JobContext] = {}
        self._threads: dict[str, tuple[QThread, _PreviewWorker]] = {}
        self._active_job_id: str | None = None
        self._dialog: PreviewJobDialog | None = None
        self._dialog_cancel_connected = False
        self._closed = False

    @property
    def dialog(self) -> PreviewJobDialog | None:
        return self._dialog

    @property
    def runtime(self) -> PreviewRuntime:
        return self._runtime

    @property
    def scheduler(self) -> ExclusiveJobScheduler:
        return self._scheduler

    @property
    def active_preview_count(self) -> int:
        return sum(thread.isRunning() for thread, _worker in self._threads.values())

    def set_dialog_parent(self, parent: QWidget) -> None:
        self._dialog_parent = parent
        if self._dialog is not None:
            self._dialog.setParent(parent)

    def dispatch(self, command: object) -> None:
        if self._closed or not isinstance(command, PreviewFrameRequested):
            return
        state = self._store.state
        if not capabilities(state).can_preview:
            return
        source_id = state.source_id
        metadata = state.source_value
        if source_id is None or state.source is not SourceState.READY:
            return
        inputs = _preview_inputs(metadata, state.timeline)
        job_id = uuid4().hex
        request_id = uuid4().hex
        self._store.dispatch(
            PreviewRequested(
                job_id,
                request_id,
                requires_model_preparation=True,
                initiator_focus=FocusTarget.PREVIEW_ACTION,
            )
        )
        if self._store.state.job.job_id != job_id:
            return
        self._start(job_id, source_id, request_id, inputs)

    def shutdown(self) -> None:
        self._closed = True
        for context in tuple(self._contexts.values()):
            context.request_cancel()
        for thread, _worker in tuple(self._threads.values()):
            thread.quit()
        for thread, _worker in tuple(self._threads.values()):
            thread.wait(5000)
        self._runtime.close()

    def _start(
        self,
        job_id: str,
        source_id: str,
        request_id: str,
        inputs: _PreviewInputs,
    ) -> None:
        worker_ref: dict[str, _PreviewWorker] = {}

        def progress_sink(event: ProgressEvent) -> None:
            worker_ref["worker"].emit_progress(event)

        cancellation = CancellationState()
        lease = self._scheduler.claim(
            JobKind.PREVIEW,
            job_id,
            workspace=inputs.source.parent,
            progress_sink=progress_sink,
            cancellation=cancellation,
        )
        context = lease.__enter__()
        worker = _PreviewWorker(
            job_id,
            source_id,
            request_id,
            inputs,
            self._runtime,
            context,
        )
        worker_ref["worker"] = worker
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.notification.connect(self._notification)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(
            lambda job_id=job_id: self._thread_finished(job_id)
        )
        thread.started.connect(worker.run)
        self._contexts[job_id] = context
        self._threads[job_id] = (thread, worker)
        self._active_job_id = job_id
        self._open_dialog(job_id)
        thread.start()

    def _open_dialog(self, job_id: str) -> None:
        if self._dialog is None:
            self._dialog = PreviewJobDialog(self._dialog_parent)
        if not self._dialog_cancel_connected:
            self._dialog.cancel_requested.connect(self._cancel_active)
            self._dialog_cancel_connected = True
        self._dialog.reset("Preparing model")
        self._dialog.setProperty("jobId", job_id)
        self._dialog.open()

    @Slot()
    def _cancel_active(self) -> None:
        job_id = self._active_job_id
        if job_id is None:
            return
        context = self._contexts.get(job_id)
        if context is None:
            return
        if not context.request_cancel():
            return
        self._store.dispatch(CancelRequested(job_id))
        if self._dialog is not None:
            self._dialog.set_cancelling()
        cancel = getattr(self._runtime, "cancel", None)
        if callable(cancel):
            Thread(
                target=_cancel_runtime,
                args=(cancel, job_id),
                name="rembggui-preview-cancel",
                daemon=True,
            ).start()

    @Slot(object)
    def _notification(self, notification: object) -> None:
        job_id = _notification_job_id(notification)
        if job_id is None or job_id != self._active_job_id:
            return
        if self._store.state.job.job_id != job_id:
            return
        if isinstance(notification, ProgressEvent):
            if self._store.state.job.phase is not JobState.CANCELLING:
                assert self._dialog is not None
                self._dialog.set_progress(notification)
            return
        if isinstance(notification, (PreviewRequested, ModelPrepared, JobStageChanged)):
            self._store.dispatch(notification)
            self._update_dialog_for_stage(notification)
            return
        if isinstance(
            notification, (PreviewSucceeded, PreviewFailed, CancelAcknowledged)
        ):
            self._terminal_notification(notification)

    def _update_dialog_for_stage(self, notification: object) -> None:
        if self._dialog is None:
            return
        stage = getattr(notification, "stage", "")
        if stage:
            self._dialog.stage_label.setText(stage)
        if isinstance(notification, ModelPrepared):
            self._dialog.setWindowTitle("Previewing selected frame")
        elif stage and stage != "Segmentation":
            self._dialog.setWindowTitle("Preparing model")

    def _terminal_notification(
        self, notification: PreviewSucceeded | PreviewFailed | CancelAcknowledged
    ) -> None:
        job_id = _notification_job_id(notification)
        if job_id is None or self._store.state.job.job_id != job_id:
            return
        self._store.dispatch(notification)
        if self._store.state.job.phase is JobState.IDLE:
            if self._dialog is not None:
                self._dialog.close_for_terminal()
            if self._active_job_id == job_id:
                self._active_job_id = None

    def _thread_finished(self, job_id: str) -> None:
        self._contexts.pop(job_id, None)
        self._threads.pop(job_id, None)


def _preview_inputs(
    metadata: object, timeline: TimelineState | None = None
) -> _PreviewInputs:
    source = getattr(metadata, "path", None)
    width = getattr(metadata, "width", None)
    height = getattr(metadata, "height", None)
    duration = getattr(metadata, "duration", None)
    if (
        not isinstance(source, Path)
        or type(width) is not int
        or type(height) is not int
        or not isinstance(duration, Fraction)
    ):
        raise ValueError("loaded source metadata cannot build a preview request")
    if timeline is None:
        return _PreviewInputs(
            source, width, height, duration, Fraction(0), duration, Fraction(0)
        )
    if timeline.duration != duration:
        raise ValueError("timeline duration does not match loaded source metadata")
    return _PreviewInputs(
        source,
        width,
        height,
        duration,
        timeline.start,
        timeline.end,
        timeline.playhead,
    )


def _render_request(
    inputs: _PreviewInputs,
    model_id: str,
    *,
    fps: int = 1,
    filename: str = "preview.webp",
    collision_policy: CollisionPolicy = CollisionPolicy.CANCEL,
) -> RenderRequest:
    return RenderRequest(
        source=inputs.source,
        sampling=SamplingSpec(inputs.start, inputs.end, fps=fps),
        crop=CropSpec(0, 0, inputs.width, inputs.height),
        segmentation=SegmentationSpec(
            model_id=model_id, edge_mode=EdgeMode.STANDARD
        ),
        framing=FramingSpec(
            trim=False,
            alpha_threshold=Decimal("2.0"),
            padding=0,
            stretch_x=Decimal("1.0"),
        ),
        output=OutputSpec(
            inputs.source.parent,
            filename,
            collision_policy=collision_policy,
        ),
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
        return error
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
