from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QWidget

from matteloop.core.state import JobState, ModelAvailabilityChanged
from matteloop.jobs.models.catalog import ModelCatalog
from matteloop.ui.ports import StateStore
from matteloop.ui.source_presentation import (
    DownloadRateEstimator,
    format_model_download_detail,
    format_source_file_size,
)

from ._dialog import ModelManagerDialog
from ._entries import ModelEntry, manager_active_id
from ._workers import (
    _ModelDownloadWorker,
    _ModelRemovalWorker,
    _ObsoleteRemovalWorker,
)

_DOWNLOAD_BLOCKED_MESSAGE = "Cannot download a model while a job is running."


class ModelRemovalService(Protocol):
    @property
    def active_id(self) -> str | None: ...

    def remove(self, model_id: str) -> bool: ...

    def remove_obsolete_versions(self) -> int: ...

    def fetch(self, model_id: str, progress: object = None) -> object: ...


class ModelManagerController(QObject):
    """Own model-manager actions while keeping widgets on the GUI thread."""

    def __init__(
        self,
        store: StateStore,
        *,
        catalog: ModelCatalog,
        cache_root: Path,
        manager: ModelRemovalService | None,
        dialog_parent: QWidget | None = None,
    ) -> None:
        super().__init__(dialog_parent)
        self._store = store
        self._manager = manager
        active_model = (
            manager_active_id if manager is None else lambda: manager.active_id
        )
        self.dialog = ModelManagerDialog(
            catalog,
            cache_root,
            active_model=active_model,
            parent=dialog_parent,
        )
        self.dialog.set_removal_guard(self._removal_block_reason)
        self.dialog.download_requested.connect(self._download_requested)
        self.dialog.remove_requested.connect(self._remove_requested)
        self.dialog.delete_outdated_requested.connect(self._delete_outdated_requested)
        self.dialog.show_cache_requested.connect(self._show_cache)
        self._remove_thread: QThread | None = None
        self._remove_worker: (
            _ModelRemovalWorker | _ModelDownloadWorker | _ObsoleteRemovalWorker | None
        ) = None
        self._removal_entry: ModelEntry | None = None

    def set_dialog_parent(self, parent: QWidget) -> None:
        self.dialog.setParent(parent, Qt.WindowType.Dialog)

    def open(self) -> None:
        self.dialog.refresh()
        self.dialog.open()

    def close(self) -> None:
        thread = self._remove_thread
        if thread is not None:
            thread.quit()
            thread.wait(5000)
            self._remove_thread = None
        self.dialog.close()

    def _removal_block_reason(self, entry: ModelEntry) -> str | None:
        if self._store.state.job.phase is not JobState.IDLE:
            return "Cannot remove a model while a job is running."
        if self._manager is None:
            return "Model removal is unavailable in this runtime."
        if self._manager.active_id == entry.model_id:
            return "Cannot remove the model used by the active session."
        return None

    def _download_requested(self, value: object) -> None:
        manager = self._manager
        if not isinstance(value, ModelEntry) or value.cached or manager is None:
            return
        if self._remove_thread or self._store.state.job.phase is not JobState.IDLE:
            if self._remove_thread is None:
                self.dialog.set_message(_DOWNLOAD_BLOCKED_MESSAGE)
            return
        worker = _ModelDownloadWorker(manager, value.model_id)
        thread = QThread(self)
        worker.moveToThread(thread)
        self._download_rate = DownloadRateEstimator()
        worker.progressed.connect(self._download_progressed)
        worker.downloaded.connect(self._download_succeeded)
        worker.failed.connect(self._download_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._removal_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._remove_thread = thread
        self._remove_worker = worker
        self._removal_entry = value
        self.dialog.set_busy(True)
        self.dialog.set_message(f"Downloading {value.display_name}…")
        thread.started.connect(worker.run)
        thread.start()

    def _delete_outdated_requested(self) -> None:
        manager = self._manager
        if self.dialog.obsolete_size_bytes <= 0:
            return
        if self._store.state.job.phase is not JobState.IDLE:
            self.dialog.set_message("Cannot remove a model while a job is running.")
            return
        if manager is None:
            self.dialog.set_message("Model removal is unavailable in this runtime.")
            return
        versions = ", ".join(self.dialog.obsolete_versions)
        directory_word = (
            "directory" if len(self.dialog.obsolete_versions) == 1 else "directories"
        )
        size = format_source_file_size(self.dialog.obsolete_size_bytes)
        answer = QMessageBox.question(
            self.dialog,
            f"Delete all weights from rembg {versions}?",
            f"This removes the whole {versions} {directory_word}: {size} on disk. "
            "Weights this version needs are downloaded again on demand.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        self._start_obsolete_removal(manager)

    def _start_obsolete_removal(self, manager: ModelRemovalService) -> None:
        worker = _ObsoleteRemovalWorker(manager)
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.removed.connect(self._obsolete_removal_succeeded)
        worker.failed.connect(self._obsolete_removal_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._removal_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._remove_thread = thread
        self._remove_worker = worker
        self._removal_entry = None
        self.dialog.set_busy(True)
        self.dialog.set_message("Removing outdated model weights…")
        thread.started.connect(worker.run)
        thread.start()

    @Slot(int, int)
    def _download_progressed(self, completed: int, total: int) -> None:
        entry = self._removal_entry
        if entry is None:
            return
        speed = self._download_rate.update(completed, time.monotonic())
        self.dialog.set_message(
            format_model_download_detail(entry.display_name, completed, total, speed)
        )

    @Slot(str)
    def _download_succeeded(self, model_id: str) -> None:
        entry = self._removal_entry
        if entry is None or entry.model_id != model_id:
            return
        self.dialog.refresh()
        self.dialog.set_message(f"Downloaded {entry.display_name}.")
        if self._store.state.parameters.model_id == model_id:
            self._store.dispatch(ModelAvailabilityChanged(True))

    @Slot(str, object)
    def _download_failed(self, model_id: str, error: object) -> None:
        entry = self._removal_entry
        if entry is None or entry.model_id != model_id:
            return
        self.dialog.set_message(f"Could not download the weight: {error}")
        QMessageBox.warning(self.dialog, "Could not download model", str(error))

    @Slot(int)
    def _obsolete_removal_succeeded(self, removed: int) -> None:
        self.dialog.refresh()
        self.dialog.set_message(
            f"Removed {removed} outdated model version(s) from the cache."
        )

    @Slot(object)
    def _obsolete_removal_failed(self, error: object) -> None:
        self.dialog.refresh()
        self.dialog.set_message(
            f"Could not remove outdated weights: {type(error).__name__}: {error}"
        )

    def _remove_requested(self, value: object) -> None:
        if not isinstance(value, ModelEntry) or not value.cached:
            return
        if self._removal_block_reason(value) is not None:
            return
        if self._remove_thread is not None:
            return
        answer = QMessageBox.question(
            self.dialog,
            "Remove model weight?",
            f"Remove {value.display_name}'s downloaded weight?\n"
            f"This frees {format_source_file_size(value.disk_size_bytes or 0)}.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        manager = self._manager
        if manager is None:
            return
        self._start_removal(manager, value)

    def _start_removal(self, manager: ModelRemovalService, entry: ModelEntry) -> None:
        worker = _ModelRemovalWorker(manager, entry.model_id)
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.removed.connect(self._removal_succeeded)
        worker.failed.connect(self._removal_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._removal_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._remove_thread = thread
        self._remove_worker = worker
        self._removal_entry = entry
        self.dialog.set_busy(True)
        thread.started.connect(worker.run)
        thread.start()

    @Slot(str, bool)
    def _removal_succeeded(self, model_id: str, removed: bool) -> None:
        entry = self._removal_entry
        if entry is None:
            return
        if entry.model_id != model_id:
            return
        if not removed:
            self.dialog.refresh()
            self.dialog.set_message(
                f"No weight could be removed at {entry.artifact_path}."
            )
            return
        freed = format_source_file_size(entry.disk_size_bytes or 0)
        self.dialog.refresh()
        self.dialog.set_message(f"Removed {entry.display_name}; freed {freed}.")
        if self._store.state.parameters.model_id == model_id:
            self._store.dispatch(ModelAvailabilityChanged(False))
        QMessageBox.information(
            self.dialog,
            "Model removed",
            f"Removed {entry.display_name}'s downloaded weight.\nFreed {freed}.",
        )

    @Slot(str, object)
    def _removal_failed(self, model_id: str, error: object) -> None:
        entry = self._removal_entry
        if entry is None or entry.model_id != model_id:
            return
        self.dialog.set_message(f"Could not remove the selected weight: {error}")
        QMessageBox.warning(self.dialog, "Could not remove model", str(error))

    @Slot()
    def _removal_thread_finished(self) -> None:
        self.dialog.set_busy(False)
        self._remove_worker = None
        self._remove_thread = None

    @Slot()
    def _show_cache(self) -> None:
        # Nothing has created the cache directory before the first download,
        # and openUrl on a missing directory fails silently.
        root = self.dialog.cache_root
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self.dialog.set_message(f"Could not open the model folder: {error}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(root))):
            self.dialog.set_message(f"Model folder: {root}")
