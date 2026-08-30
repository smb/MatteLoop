"""GUI-thread command controller for the first source-loading slice."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import count
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from PIL import Image
from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QFileDialog, QWidget

from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.state import (
    SourceLoaded,
    SourceLoadFailed,
    SourceLoadRequested,
    SourceState,
)
from rembggui.jobs.source import decode_frame, probe_source
from rembggui.ui.ports import (
    ChooseVideoRequested,
    ManageModelsRequested,
    ManageWorkspacesRequested,
    OpenOutputFolderRequested,
    OpenOutputRequested,
    PreviewFrameRequested,
    RebuildEditedCutsRequested,
    RenderVideoRequested,
    StateStore,
    VideoDropped,
    WindowCommand,
)
from rembggui.ui.preview_controller import PreviewController, PreviewRuntime
from rembggui.ui.render_controller import RenderController

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
        dialog_parent: QWidget | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._source_adapter = source_adapter or PyAVSourceAdapter()
        self._dialog_parent = dialog_parent
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
        self._decode_request_ids = count(1)
        self._threads: dict[str, tuple[QThread, _SourceLoadWorker]] = {}
        self._closed = False

    def set_dialog_parent(self, parent: QWidget) -> None:
        """Set the window used as the parent for native source dialogs."""
        self._dialog_parent = parent
        self._preview_controller.set_dialog_parent(parent)
        self._render_controller.set_dialog_parent(parent)

    @property
    def active_load_count(self) -> int:
        """Expose worker count for lifecycle tests without exposing worker state."""
        return sum(thread.isRunning() for thread, _worker in self._threads.values())

    @property
    def render_controller(self) -> RenderController:
        """Expose the render command owner for lifecycle and UI integration tests."""
        return self._render_controller

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
        elif isinstance(command, RebuildEditedCutsRequested):
            # TODO(next slice: rebuild): wire edited-cut rebuild to the job service.
            return
        elif isinstance(command, ManageModelsRequested):
            # TODO(next slice: models): add the model manager command service.
            return
        elif isinstance(command, ManageWorkspacesRequested):
            # TODO(next slice: workspaces): add the workspace manager command service.
            return
        elif isinstance(command, OpenOutputRequested):
            self._render_controller.dispatch(command)
        elif isinstance(command, OpenOutputFolderRequested):
            self._render_controller.dispatch(command)

    def shutdown(self) -> None:
        """Stop accepting results while the application is closing."""
        self._closed = True
        self._render_controller.shutdown()
        self._preview_controller.shutdown()
        threads = tuple(self._threads.items())
        for _request_id, (thread, _worker) in threads:
            thread.quit()
        for _request_id, (thread, _worker) in threads:
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
