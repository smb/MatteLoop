"""GUI-thread preview orchestration and its job-dialog owner."""

from __future__ import annotations

from threading import Thread
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QWidget

from rembggui.core.execution_providers import ProviderOption
from rembggui.core.parameters import V1_MODEL_IDS
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
from rembggui.jobs.context import (
    CancellationState,
    ExclusiveJobScheduler,
    JobContext,
    ProgressEvent,
)
from rembggui.ui.ports import PreviewFrameRequested, StateStore
from rembggui.ui.preview_controller.dialog import PreviewJobDialog
from rembggui.ui.preview_controller.request_assembly import (
    _preview_inputs,
    _PreviewInputs,
)
from rembggui.ui.preview_controller.runtime import (
    PreviewRuntime,
    ProductionPreviewRuntime,
)
from rembggui.ui.preview_controller.worker import (
    _cancel_runtime,
    _notification_job_id,
    _PreviewWorker,
)


class PreviewController(QObject):
    """Run previews away from Qt widgets while owning their job dialog."""

    provider_ready = Signal(str)
    provider_notice = Signal(str)

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
    def model_options(self) -> tuple[tuple[str, bool], ...]:
        options = getattr(self._runtime, "model_options", ())
        if isinstance(options, tuple) and all(
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], bool)
            for item in options
        ):
            return options
        return tuple((model_id, False) for model_id in V1_MODEL_IDS)

    @property
    def provider_options(self) -> tuple[ProviderOption, ...]:
        options = getattr(self._runtime, "provider_options", ())
        if isinstance(options, tuple) and all(
            isinstance(item, ProviderOption) for item in options
        ):
            return options
        return ()

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
        inputs = _preview_inputs(
            metadata, state.timeline, state.crop, state.parameters
        )
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
        for job_id, (thread, _worker) in tuple(self._threads.items()):
            try:
                thread.quit()
            except RuntimeError:
                self._threads.pop(job_id, None)
        for job_id, (thread, _worker) in tuple(self._threads.items()):
            try:
                thread.wait(5000)
            except RuntimeError:
                self._threads.pop(job_id, None)
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
        worker.provider_ready.connect(self._provider_ready)
        worker.provider_notice.connect(self._provider_notice)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(
            lambda job_id=job_id: self._thread_finished(job_id)
        )
        thread.finished.connect(thread.deleteLater)
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

    @Slot(str)
    def _provider_ready(self, provider: str) -> None:
        if (
            self._active_job_id is None
            or self._store.state.job.job_id != self._active_job_id
        ):
            return
        self.provider_ready.emit(provider)

    @Slot(str)
    def _provider_notice(self, notice: str) -> None:
        if (
            self._active_job_id is None
            or self._store.state.job.job_id != self._active_job_id
        ):
            return
        if self._dialog is not None:
            self._dialog.set_provider_notice(notice)

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
