"""GUI-thread render orchestration, confirmations, and output actions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import Thread
from typing import Protocol
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.specs import CollisionPolicy, RenderRequest
from rembggui.core.state import (
    ArtifactResult,
    CancelAcknowledged,
    CancelRequested,
    JobKind,
    JobStageChanged,
    JobState,
    PreviewState,
    RenderFailed,
    RenderPreflightDismissed,
    RenderPreflightRequested,
    RenderRequested,
    RenderSucceeded,
    SourceState,
    capabilities,
)
from rembggui.jobs.context import (
    CancellationState,
    ExclusiveJobScheduler,
    JobContext,
    JobTerminalState,
    ProgressEvent,
)
from rembggui.jobs.render import RenderArtifact
from rembggui.ui.ports import (
    OpenOutputFolderRequested,
    OpenOutputRequested,
    RenderVideoRequested,
    StateStore,
)
from rembggui.ui.preview_controller import (
    PreviewJobDialog,
    PreviewRuntime,
    _preview_inputs,
    _render_request,
)


class RenderRuntime(Protocol):
    def render(self, request: RenderRequest, context: JobContext) -> RenderArtifact: ...


class _RenderWorker(QObject):
    notification = Signal(object)
    finished = Signal(str)

    def __init__(
        self,
        job_id: str,
        source_id: str,
        request_id: str,
        request: RenderRequest,
        runtime: RenderRuntime,
        context: JobContext,
    ) -> None:
        super().__init__()
        self._job_id = job_id
        self._source_id = source_id
        self._request_id = request_id
        self._request = request
        self._runtime = runtime
        self._context = context

    def emit_progress(self, event: ProgressEvent) -> None:
        self.notification.emit(event)

    @Slot()
    def run(self) -> None:
        try:
            artifact = self._runtime.render(self._request, self._context)
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


class RenderController(QObject):
    """Start render jobs after reducer-approved UI confirmations."""

    def __init__(
        self,
        store: StateStore,
        *,
        runtime: PreviewRuntime,
        scheduler: ExclusiveJobScheduler,
        preview_callback: Callable[[], None],
        dialog_parent: QWidget | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._runtime = runtime
        self._scheduler = scheduler
        self._preview_callback = preview_callback
        self._dialog_parent = dialog_parent
        self._contexts: dict[str, JobContext] = {}
        self._threads: dict[str, tuple[QThread, _RenderWorker]] = {}
        self._active_job_id: str | None = None
        self._dialog: PreviewJobDialog | None = None
        self._dialog_cancel_connected = False
        self._preflight_dialog: QMessageBox | None = None
        self._preflight_request: RenderRequest | None = None
        self._collision_dialog: QMessageBox | None = None
        self._collision_request: RenderRequest | None = None
        self._closed = False

    @property
    def dialog(self) -> PreviewJobDialog | None:
        return self._dialog

    @property
    def preflight_dialog(self) -> QMessageBox | None:
        return self._preflight_dialog

    @property
    def collision_dialog(self) -> QMessageBox | None:
        return self._collision_dialog

    @property
    def active_render_count(self) -> int:
        return sum(thread.isRunning() for thread, _worker in self._threads.values())

    def set_dialog_parent(self, parent: QWidget) -> None:
        self._dialog_parent = parent
        if self._dialog is not None:
            self._dialog.setParent(parent)

    def dispatch(self, command: object) -> None:
        if self._closed:
            return
        if isinstance(command, RenderVideoRequested):
            self._request_render()
        elif isinstance(command, OpenOutputRequested):
            self._open_output()
        elif isinstance(command, OpenOutputFolderRequested):
            self._open_output_folder()

    def shutdown(self) -> None:
        self._closed = True
        if self._preflight_dialog is not None:
            self._preflight_dialog.close()
        if self._collision_dialog is not None:
            self._collision_dialog.close()
        for context in tuple(self._contexts.values()):
            context.request_cancel()
        cancel = getattr(self._runtime, "cancel", None)
        if callable(cancel):
            for job_id in tuple(self._contexts):
                Thread(
                    target=_cancel_runtime,
                    args=(cancel, job_id),
                    name="rembggui-render-cancel",
                    daemon=True,
                ).start()
        for thread, _worker in tuple(self._threads.values()):
            thread.quit()
        for thread, _worker in tuple(self._threads.values()):
            thread.wait(5000)

    def _request_render(self) -> None:
        state = self._store.state
        if not capabilities(state).can_render:
            return
        source_id = state.source_id
        if source_id is None or state.source is not SourceState.READY:
            return
        try:
            inputs = _preview_inputs(state.source_value, state.timeline, state.crop)
            request = _render_request(
                inputs,
                "birefnet-portrait",
                fps=15,
                filename=f"{inputs.source.stem}.webp",
            )
        except BaseException:
            return
        if state.preview in {
            PreviewState.NONE,
            PreviewState.STALE,
            PreviewState.ERROR,
        }:
            self._preflight_request = request
            self._store.dispatch(RenderPreflightRequested())
            if self._store.state.preflight_warning:
                self._show_preflight()
            return
        self._resolve_collision(request)

    def _show_preflight(self) -> None:
        if self._preflight_dialog is not None:
            return
        dialog = QMessageBox(self._dialog_parent)
        dialog.setObjectName("render_preflight_dialog")
        dialog.setWindowTitle("Preview recommended")
        dialog.setText("Preview this frame before rendering?")
        dialog.setInformativeText(
            "A preview lets you verify the cutout before processing the whole video."
        )
        preview = dialog.addButton(
            "Preview first", QMessageBox.ButtonRole.AcceptRole
        )
        render = dialog.addButton(
            "Render anyway", QMessageBox.ButtonRole.AcceptRole
        )
        cancel = dialog.addButton("Cancel", QMessageBox.ButtonRole.AcceptRole)
        dialog.setDefaultButton(preview)
        dialog.setEscapeButton(cancel)
        dialog.buttonClicked.connect(
            lambda button: self._preflight_selected(button, preview, render, cancel)
        )
        dialog.finished.connect(lambda _result: self._preflight_closed())
        self._preflight_dialog = dialog
        dialog.open()

    def _preflight_selected(
        self, button: object, preview: object, render: object, cancel: object
    ) -> None:
        if button is preview:
            self._finish_preflight("preview")
        elif button is render:
            self._finish_preflight("render")
        elif button is cancel:
            self._finish_preflight("cancel")

    def _preflight_closed(self) -> None:
        if self._preflight_dialog is not None:
            self._finish_preflight("cancel")

    def _finish_preflight(self, choice: str) -> None:
        dialog = self._preflight_dialog
        request = self._preflight_request
        if dialog is None or request is None:
            return
        self._preflight_dialog = None
        self._preflight_request = None
        dialog.close()
        self._store.dispatch(RenderPreflightDismissed())
        if choice == "preview":
            self._preview_callback()
        elif choice == "render":
            self._resolve_collision(request)

    def _resolve_collision(self, request: RenderRequest) -> None:
        try:
            exists = request.output.path.exists()
        except OSError:
            exists = False
        if not exists:
            self._start(request)
            return
        self._collision_request = request
        self._show_collision()

    def _show_collision(self) -> None:
        if self._collision_dialog is not None or self._collision_request is None:
            return
        dialog = QMessageBox(self._dialog_parent)
        dialog.setObjectName("render_collision_dialog")
        dialog.setWindowTitle("Output already exists")
        dialog.setText(f"{self._collision_request.output.path} already exists.")
        dialog.setInformativeText("Choose how to handle the existing output.")
        replace_button = dialog.addButton(
            "Replace", QMessageBox.ButtonRole.AcceptRole
        )
        choose_button = dialog.addButton(
            "Choose another name", QMessageBox.ButtonRole.AcceptRole
        )
        cancel_button = dialog.addButton("Cancel", QMessageBox.ButtonRole.AcceptRole)
        dialog.setDefaultButton(cancel_button)
        dialog.setEscapeButton(cancel_button)
        dialog.buttonClicked.connect(
            lambda button: self._collision_selected(
                button, replace_button, choose_button, cancel_button
            )
        )
        dialog.finished.connect(lambda _result: self._collision_closed())
        self._collision_dialog = dialog
        dialog.open()

    def _collision_selected(
        self,
        button: object,
        replace_button: object,
        choose_button: object,
        cancel: object,
    ) -> None:
        if button is replace_button:
            self._finish_collision("replace")
        elif button is choose_button:
            self._finish_collision("choose")
        elif button is cancel:
            self._finish_collision("cancel")

    def _collision_closed(self) -> None:
        if self._collision_dialog is not None:
            self._collision_dialog = None
            self._collision_request = None

    def _finish_collision(self, choice: str) -> None:
        dialog = self._collision_dialog
        request = self._collision_request
        if dialog is None or request is None:
            return
        self._collision_dialog = None
        self._collision_request = None
        dialog.close()
        if choice == "replace":
            self._start(
                replace(
                    request,
                    output=replace(
                        request.output, collision_policy=CollisionPolicy.REPLACE
                    ),
                )
            )
        elif choice == "choose":
            self._choose_output_name(request)

    def _choose_output_name(self, request: RenderRequest) -> None:
        filename, _filter = QFileDialog.getSaveFileName(
            self._dialog_parent,
            "Choose output name",
            str(request.output.path),
            "WebP files (*.webp)",
        )
        if not filename:
            return
        chosen = Path(filename)
        if chosen.suffix.casefold() != ".webp":
            chosen = (
                chosen.with_suffix(".webp")
                if chosen.suffix
                else Path(f"{chosen}.webp")
            )
        try:
            selected = replace(
                request,
                output=replace(
                    request.output,
                    directory=chosen.parent,
                    filename=chosen.name,
                    collision_policy=CollisionPolicy.CANCEL,
                ),
            )
        except AppError:
            return
        self._resolve_collision(selected)

    def _start(self, request: RenderRequest) -> None:
        state = self._store.state
        source_id = state.source_id
        if source_id is None:
            return
        job_id = uuid4().hex
        request_id = uuid4().hex
        self._store.dispatch(RenderRequested(job_id, request_id))
        if self._store.state.job.job_id != job_id:
            return
        worker_ref: dict[str, _RenderWorker] = {}

        def progress_sink(event: ProgressEvent) -> None:
            worker_ref["worker"].emit_progress(event)

        cancellation = CancellationState()
        lease = self._scheduler.claim(
            JobKind.RENDER,
            job_id,
            workspace=request.output.directory,
            progress_sink=progress_sink,
            cancellation=cancellation,
        )
        context = lease.__enter__()
        worker = _RenderWorker(
            job_id,
            source_id,
            request_id,
            request,
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
        thread.finished.connect(lambda job_id=job_id: self._thread_finished(job_id))
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
        self._dialog.reset("Rendering video")
        self._dialog.stage_label.setText("Decode")
        self._dialog.setProperty("jobId", job_id)
        self._dialog.open()

    @Slot()
    def _cancel_active(self) -> None:
        job_id = self._active_job_id
        if job_id is None:
            return
        context = self._contexts.get(job_id)
        if context is None or not context.request_cancel():
            return
        self._store.dispatch(CancelRequested(job_id))
        if self._dialog is not None:
            self._dialog.set_cancelling()
        cancel = getattr(self._runtime, "cancel", None)
        if callable(cancel):
            Thread(
                target=_cancel_runtime,
                args=(cancel, job_id),
                name="rembggui-render-cancel",
                daemon=True,
            ).start()

    @Slot(object)
    def _notification(self, notification: object) -> None:
        job_id = getattr(notification, "job_id", None)
        if job_id is None or job_id != self._active_job_id:
            return
        if self._store.state.job.job_id != job_id:
            return
        if isinstance(notification, ProgressEvent):
            progress = _normalise_progress(notification)
            state = self._store.state
            self._store.dispatch(
                JobStageChanged(
                    job_id,
                    state.source_id or "",
                    state.job_request_id or "",
                    progress.stage,
                )
            )
            if (
                self._dialog is not None
                and self._store.state.job.phase is not JobState.CANCELLING
            ):
                self._dialog.set_progress(progress)
            return
        if isinstance(
            notification, (RenderSucceeded, RenderFailed, CancelAcknowledged)
        ):
            self._terminal_notification(notification)

    def _terminal_notification(
        self, notification: RenderSucceeded | RenderFailed | CancelAcknowledged
    ) -> None:
        job_id = getattr(notification, "job_id", None)
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

    def _open_output(self) -> None:
        state = self._store.state
        if not capabilities(state).can_open_output or state.artifact_result is None:
            return
        path = _path_value(state.artifact_result.value)
        if path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_output_folder(self) -> None:
        state = self._store.state
        if not capabilities(state).can_open_folder or state.artifact_result is None:
            return
        path = _path_value(state.artifact_result.value)
        if path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


def _normalise_progress(event: ProgressEvent) -> ProgressEvent:
    if event.stage != "render-cut":
        return event
    return ProgressEvent(
        event.job_id,
        "Decode",
        event.completed,
        event.total,
        event.detail,
    )


def _path_value(value: object) -> Path | None:
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value:
        return Path(value)
    return None


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


def _cancel_runtime(cancel: object, job_id: str) -> None:
    try:
        cancel(job_id)  # type: ignore[operator]
    except BaseException:
        return
