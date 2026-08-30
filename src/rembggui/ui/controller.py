"""GUI-thread command controller for the first source-loading slice."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from fractions import Fraction
from itertools import count
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from PIL import Image
from PySide6.QtCore import QObject, QSettings, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QFileDialog, QWidget

from rembggui.core.crop_state import CropEvent
from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.execution_providers import ProviderOption, is_allowed_provider
from rembggui.core.parameters import (
    ExecutionProviderChanged,
    OutputDirectoryChanged,
    ParameterEvent,
    output_directory_for_source,
)
from rembggui.core.state import (
    AppState,
    JobState,
    SourceLoaded,
    SourceLoadFailed,
    SourceLoadRequested,
    SourceState,
)
from rembggui.core.timeline import (
    DurationChanged,
    EndChanged,
    PlayheadChanged,
    ResetRange,
    SetEndToPlayhead,
    SetStartToPlayhead,
    SourceFrameDecoded,
    StartChanged,
    StepFrame,
)
from rembggui.jobs.source import (
    SourceRevision,
    SourceValidationProof,
    decode_frame,
    probe_source,
)
from rembggui.ui.ports import (
    ChooseVideoRequested,
    ManageModelsRequested,
    ManageWorkspacesRequested,
    OpenOutputFolderRequested,
    OpenOutputRequested,
    OutputDirectoryRequested,
    PreviewFrameRequested,
    RebuildEditedCutsRequested,
    RenderVideoRequested,
    StateStore,
    VideoDropped,
    WindowCommand,
)
from rembggui.ui.preferences import persist_parameters
from rembggui.ui.preview_controller import PreviewController, PreviewRuntime
from rembggui.ui.render_controller import RenderController
from rembggui.ui.timeline import SourceFrameWorker

VIDEO_FILE_FILTER = "Video files (*.mp4 *.mov *.webm *.mkv)"
_THREAD_SHUTDOWN_TIMEOUT_MS = 5000


@dataclass(frozen=True)
class SourceLoadResult:
    """Domain result returned by a source adapter before Qt image conversion."""

    metadata: object
    frame: Image.Image


@dataclass(frozen=True)
class LoadedSource:
    """Reducer payload containing metadata and a worker-produced display image."""

    metadata: object
    frame: QImage


class SourceAdapter(Protocol):
    """Probe and decode seam used by the worker and by controller tests."""

    def load(self, path: Path, request_id: int) -> SourceLoadResult: ...


class PyAVSourceAdapter:
    """Use a fresh private PyAV container for probing and for exact frame decode."""

    def load(self, path: Path, request_id: int) -> SourceLoadResult:
        metadata = probe_source(path)
        decoded = decode_frame(
            path,
            Fraction(0),
            request_id,
            expected_revision=metadata.revision,
            validation_proof=metadata.validation_proof,
        )
        return SourceLoadResult(metadata, decoded.image)


class _SourceLoadWorker(QObject):
    loaded = Signal(str, str, object)
    failed = Signal(str, str, object)
    finished = Signal()

    def __init__(
        self,
        source_id: str,
        request_id: str,
        path: Path,
        decode_request_id: int,
        adapter: SourceAdapter,
    ) -> None:
        super().__init__()
        self._source_id = source_id
        self._request_id = request_id
        self._path = path
        self._decode_request_id = decode_request_id
        self._adapter = adapter

    @Slot()
    def run(self) -> None:
        try:
            result = self._adapter.load(self._path, self._decode_request_id)
            image = _qimage_from_pillow(result.frame)
            self.loaded.emit(
                self._source_id,
                self._request_id,
                LoadedSource(result.metadata, image),
            )
        except Exception as error:
            self.failed.emit(
                self._source_id,
                self._request_id,
                _as_app_error(error),
            )
        finally:
            self.finished.emit()


class SourceController(QObject):
    """Translate source commands into reducer events and background work."""

    def __init__(
        self,
        store: StateStore,
        *,
        source_adapter: SourceAdapter | None = None,
        preview_controller: PreviewController | None = None,
        preview_runtime: PreviewRuntime | None = None,
        settings: QSettings | None = None,
        dialog_parent: QWidget | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._source_adapter = source_adapter or PyAVSourceAdapter()
        self._dialog_parent = dialog_parent
        self._settings = settings
        self._preview_controller = preview_controller or PreviewController(
            store,
            runtime=preview_runtime,
            dialog_parent=dialog_parent,
            parent=self,
        )
        self._render_controller = RenderController(
            store,
            runtime=self._preview_controller.runtime,
            scheduler=self._preview_controller.scheduler,
            preview_callback=lambda: self._preview_controller.dispatch(
                PreviewFrameRequested()
            ),
            dialog_parent=dialog_parent,
            parent=self,
        )
        self._preview_controller.provider_ready.connect(self._provider_ready)
        self._render_controller.provider_ready.connect(self._provider_ready)
        self._working_provider = store.state.parameters.execution_provider
        self._failed_provider: str | None = None
        self._pending_provider: str | None = None
        self._provider_reconcile_scheduled = False
        self._unsubscribe: Callable[[], None] | None = store.subscribe(
            self._state_changed
        )
        self._decode_request_ids = count(1)
        self._threads: dict[str, tuple[QThread, _SourceLoadWorker]] = {}
        self._frame_threads: list[tuple[QThread, SourceFrameWorker]] = []
        self._frame_timer = QTimer(self)
        self._frame_timer.setSingleShot(True)
        self._frame_timer.setInterval(125)
        self._frame_timer.timeout.connect(self._decode_current_playhead)
        self._closed = False

    def set_dialog_parent(self, parent: QWidget) -> None:
        """Set the window used as the parent for native source dialogs."""
        self._dialog_parent = parent
        self._preview_controller.set_dialog_parent(parent)
        self._render_controller.set_dialog_parent(parent)

    @property
    def active_load_count(self) -> int:
        """Expose worker count for lifecycle tests without exposing worker state."""
        active = 0
        for request_id, (thread, _worker) in tuple(self._threads.items()):
            try:
                active += int(thread.isRunning())
            except RuntimeError:
                self._threads.pop(request_id, None)
        return active

    @property
    def render_controller(self) -> RenderController:
        """Expose the render command owner for lifecycle and UI integration tests."""
        return self._render_controller

    @property
    def model_options(self) -> tuple[tuple[str, bool], ...]:
        """Expose runtime model availability for the passive inspector view."""
        return self._preview_controller.model_options

    @property
    def provider_options(self) -> tuple[ProviderOption, ...]:
        """Expose runtime provider availability for the passive inspector view."""
        return self._preview_controller.provider_options

    def dispatch(self, command: WindowCommand) -> None:
        if self._closed:
            return
        if isinstance(command, ChooseVideoRequested):
            self._choose_video(command.replace)
        elif isinstance(command, VideoDropped):
            self._start_load(command.path)
        elif isinstance(command, PreviewFrameRequested):
            self._preview_controller.dispatch(command)
        elif isinstance(command, RenderVideoRequested):
            self._render_controller.dispatch(command)
        elif isinstance(command, CropEvent):
            self._store.dispatch(command)
        elif isinstance(command, ParameterEvent):
            self._dispatch_parameter(command)
        elif isinstance(command, OutputDirectoryRequested):
            self._choose_output_directory()
        elif isinstance(
            command,
            (
                PlayheadChanged,
                StepFrame,
                StartChanged,
                EndChanged,
                DurationChanged,
                SetStartToPlayhead,
                SetEndToPlayhead,
                ResetRange,
            ),
        ):
            self._dispatch_timeline(command)
        elif isinstance(command, RebuildEditedCutsRequested):
            self._render_controller.dispatch(command)
        elif isinstance(command, ManageModelsRequested):
            # TODO(next slice: models): add the model manager command service.
            return
        elif isinstance(command, ManageWorkspacesRequested):
            self._render_controller.dispatch(command)
        elif isinstance(command, OpenOutputRequested):
            self._render_controller.dispatch(command)
        elif isinstance(command, OpenOutputFolderRequested):
            self._render_controller.dispatch(command)

    def shutdown(self) -> None:
        """Stop accepting results while the application is closing."""
        self._closed = True
        self._pending_provider = None
        if self._unsubscribe is not None:
            unsubscribe = self._unsubscribe
            self._unsubscribe = None
            unsubscribe()
        self._frame_timer.stop()
        self._cancel_frame_threads()
        for thread, _worker in tuple(self._frame_threads):
            thread.wait(_THREAD_SHUTDOWN_TIMEOUT_MS)
        self._render_controller.shutdown()
        self._preview_controller.shutdown()
        threads = tuple(self._threads.items())
        for _request_id, (thread, _load_worker) in threads:
            thread.quit()
        for _request_id, (thread, _load_worker) in threads:
            thread.wait(_THREAD_SHUTDOWN_TIMEOUT_MS)

    def _choose_video(self, replace: bool) -> None:
        caption = "Replace video" if replace else "Choose video"
        filename, _selected_filter = QFileDialog.getOpenFileName(
            self._dialog_parent,
            caption,
            "",
            VIDEO_FILE_FILTER,
        )
        if filename:
            self._start_load(Path(filename))

    def _start_load(self, path: Path) -> None:
        self._frame_timer.stop()
        self._cancel_frame_threads()
        source_id = uuid4().hex
        request_id = uuid4().hex
        self._store.dispatch(SourceLoadRequested(source_id, request_id))
        if not self._is_current_load(source_id, request_id):
            return

        worker = _SourceLoadWorker(
            source_id,
            request_id,
            path,
            next(self._decode_request_ids),
            self._source_adapter,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.loaded.connect(self._source_loaded)
        worker.failed.connect(self._source_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(
            lambda request_id=request_id: self._load_thread_finished(request_id)
        )
        self._threads[request_id] = (thread, worker)
        thread.started.connect(worker.run)
        thread.start()

    @Slot(str, str, object)
    def _source_loaded(
        self, source_id: str, request_id: str, loaded: object
    ) -> None:
        if not self._is_current_load(source_id, request_id):
            return
        if not isinstance(loaded, LoadedSource):
            self._source_failed(
                source_id,
                request_id,
                _as_app_error(TypeError("source adapter returned an invalid result")),
            )
            return
        self._store.dispatch(
            SourceLoaded(
                source_id,
                request_id,
                loaded.metadata,
                loaded.frame,
            )
        )

    @Slot(str, str, object)
    def _source_failed(self, source_id: str, request_id: str, error: object) -> None:
        if not self._is_current_load(source_id, request_id):
            return
        self._store.dispatch(
            SourceLoadFailed(source_id, request_id, _as_app_error(error))
        )

    def _is_current_load(self, source_id: str, request_id: str) -> bool:
        state = self._store.state
        return (
            not self._closed
            and state.source is SourceState.LOADING
            and state.source_id == source_id
            and state.source_request_id == request_id
        )

    def _load_thread_finished(self, request_id: str) -> None:
        self._threads.pop(request_id, None)

    def _dispatch_timeline(self, event: object) -> None:
        before = self._store.state
        self._store.dispatch(event)  # type: ignore[arg-type]
        after = self._store.state
        if after is before or after.timeline is None:
            return
        if isinstance(event, StepFrame):
            self._frame_timer.stop()
            self._decode_current_playhead()
        elif isinstance(event, PlayheadChanged):
            self._cancel_frame_threads()
            self._frame_timer.start()

    def _dispatch_parameter(self, event: ParameterEvent) -> None:
        before = self._store.state
        self._store.dispatch(event)
        after = self._store.state
        if after is not before and self._settings is not None:
            if isinstance(event, ExecutionProviderChanged):
                self._failed_provider = None
                parameters = after.parameters
            else:
                parameters = after.parameters
                if parameters.execution_provider == self._failed_provider:
                    parameters = replace(
                        parameters,
                        execution_provider=self._working_provider,
                    )
            persist_parameters(
                self._settings,
                parameters,
            )

    @Slot(str)
    def _provider_ready(self, provider: str) -> None:
        if self._closed or not is_allowed_provider(provider):
            return
        self._pending_provider = provider
        if self._store.state.job.phase is JobState.IDLE:
            self._schedule_provider_reconciliation()

    def _state_changed(self, state: AppState) -> None:
        if (
            self._pending_provider is not None
            and state.job.phase is JobState.IDLE
        ):
            self._schedule_provider_reconciliation()

    def _schedule_provider_reconciliation(self) -> None:
        if self._provider_reconcile_scheduled:
            return
        self._provider_reconcile_scheduled = True
        QTimer.singleShot(0, self._reconcile_provider)

    @Slot()
    def _reconcile_provider(self) -> None:
        self._provider_reconcile_scheduled = False
        if self._closed or self._store.state.job.phase is not JobState.IDLE:
            return
        provider, self._pending_provider = self._pending_provider, None
        if provider is None:
            return
        selected = self._store.state.parameters.execution_provider
        if selected != provider:
            self._dispatch_parameter(ExecutionProviderChanged(provider))
        self._failed_provider = None
        self._working_provider = provider

    def _choose_output_directory(self) -> None:
        state = self._store.state
        metadata = state.source_value
        source = getattr(metadata, "path", None)
        if (
            state.source is not SourceState.READY
            or not isinstance(source, Path)
        ):
            return
        current = output_directory_for_source(state.parameters, source)
        selected = QFileDialog.getExistingDirectory(
            self._dialog_parent,
            "Choose output directory",
            str(current),
        )
        if selected:
            self._dispatch_parameter(OutputDirectoryChanged(Path(selected)))

    @Slot()
    def _decode_current_playhead(self) -> None:
        if self._closed:
            return
        state = self._store.state
        metadata = state.source_value
        timeline = state.timeline
        revision = getattr(metadata, "revision", None)
        source_path = getattr(metadata, "path", None)
        if (
            state.source is not SourceState.READY
            or state.source_id is None
            or timeline is None
            or not isinstance(source_path, Path)
            or not isinstance(revision, SourceRevision)
        ):
            return
        self._cancel_frame_threads()
        proof = getattr(metadata, "validation_proof", None)
        worker = SourceFrameWorker(
            state.source_id,
            timeline.generation,
            source_path,
            timeline.playhead,
            revision,
            proof if isinstance(proof, SourceValidationProof) else None,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.result.connect(self._frame_decoded)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda thread=thread: self._forget_frame_thread(thread))
        self._frame_threads.append((thread, worker))
        thread.started.connect(worker.run)
        thread.start()

    @Slot(object)
    def _frame_decoded(self, value: object) -> None:
        if not isinstance(value, tuple) or len(value) != 3:
            return
        source_id, generation, frame = value
        if not isinstance(source_id, str) or not isinstance(generation, int):
            return
        self._store.dispatch(SourceFrameDecoded(source_id, generation, frame))

    def _cancel_frame_threads(self) -> None:
        for thread, _worker in self._frame_threads:
            thread.requestInterruption()
            thread.quit()

    def _forget_frame_thread(self, thread: QThread) -> None:
        self._frame_threads = [
            (current, worker)
            for current, worker in self._frame_threads
            if current is not thread
        ]


def _qimage_from_pillow(image: Image.Image) -> QImage:
    rgba = image.convert("RGBA")
    raw = rgba.tobytes()
    return QImage(
        raw,
        rgba.width,
        rgba.height,
        rgba.width * 4,
        QImage.Format.Format_RGBA8888,
    ).copy()


def _as_app_error(error: object) -> AppError:
    if isinstance(error, AppError):
        return error
    return AppError(
        ErrorCode.SOURCE_CORRUPT,
        "source.load",
        "source.load.failed",
        f"Could not load the video: {error}",
        "choose-another-file",
    )
