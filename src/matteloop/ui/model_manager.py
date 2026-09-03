"""Model-cache dialog and controller for the existing session manager."""

from __future__ import annotations

import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QObject, QSize, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from matteloop.core.parameters import V1_MODEL_IDS
from matteloop.core.state import JobState, ModelAvailabilityChanged
from matteloop.jobs.models.catalog import ModelCatalog
from matteloop.ui.aligned_rows import (
    ROW_DATA_ROLE,
    AlignedColumn,
    AlignedRow,
    AlignedRowDelegate,
    install_aligned_row,
)
from matteloop.ui.ports import StateStore
from matteloop.ui.source_presentation import (
    DownloadRateEstimator,
    format_model_download_detail,
    format_source_file_size,
)

MODEL_ENTRY_ROLE = ROW_DATA_ROLE + 11


class ModelRemovalService(Protocol):
    @property
    def active_id(self) -> str | None: ...

    def remove(self, model_id: str) -> bool: ...

    def remove_obsolete_versions(self) -> int: ...

    def fetch(self, model_id: str, progress: object = None) -> object: ...


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """One catalog model plus the cache facts shown by the manager."""

    model_id: str
    display_name: str
    download_size_bytes: int
    artifact_path: Path
    disk_size_bytes: int | None
    active: bool
    outdated_size_bytes: int | None = None
    outdated_rembg_version: str | None = None

    @property
    def cached(self) -> bool:
        return self.disk_size_bytes is not None


def present_model(entry: ModelEntry) -> AlignedRow:
    """Present one model with aligned metadata and spoken status words."""
    if entry.cached:
        cache_status = "cached locally"
        cache_detail = cache_status
        glyph = "◆" if entry.active else "✓"
    elif entry.outdated_size_bytes is not None:
        cache_status = "outdated weight"
        version = entry.outdated_rembg_version or "obsolete rembg"
        cache_detail = (
            f"{cache_status} from rembg {version}; "
            f"{format_source_file_size(entry.outdated_size_bytes)} on disk"
        )
        glyph = "⟳"
    else:
        cache_status = "not cached"
        cache_detail = cache_status
        glyph = "◆" if entry.active else "↓"
    active_status = "active model" if entry.active else "not active"
    size = format_source_file_size(entry.download_size_bytes)
    detail = f"{entry.display_name}; {size}; {cache_detail}; {active_status}"
    return AlignedRow(
        glyph,
        "cached" if entry.cached else "uncached",
        (
            AlignedColumn(entry.display_name),
            AlignedColumn(size, True),
            AlignedColumn(cache_status),
            AlignedColumn(active_status),
        ),
        detail,
    )


class ModelManagerDialog(QDialog):
    """List the V1 model cache and expose actions for its selected row."""

    download_requested = Signal(object)
    remove_requested = Signal(object)
    redownload_outdated_requested = Signal()
    delete_outdated_requested = Signal()
    show_cache_requested = Signal()

    def __init__(
        self,
        catalog: ModelCatalog,
        cache_root: Path,
        *,
        active_model: Callable[[], str | None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if type(catalog) is not ModelCatalog:
            raise TypeError("catalog must be an exact ModelCatalog")
        if not isinstance(cache_root, Path):
            raise TypeError("cache_root must be a Path")
        self.setObjectName("model_manager")
        self.setWindowTitle("Model manager")
        self.setAccessibleName("Model manager")
        self.setModal(True)
        self.resize(1080, 460)
        self._catalog = catalog
        self._cache_root = cache_root
        self._active_model = active_model or (lambda: None)
        self._entries: tuple[ModelEntry, ...] = ()
        self._busy = False
        self._removal_guard: Callable[[ModelEntry], str | None] = (
            self._active_removal_guard
        )
        self._build_widgets()
        self._build_layout()
        self._connect_signals()
        self._update_actions()

    def _build_widgets(self) -> None:
        self._message = QLabel()
        self._message.setObjectName("model_manager_message")
        self._message.setProperty("secondary", True)
        self.total_size_label = QLabel()
        self.total_size_label.setObjectName("model_cache_total")
        self.total_size_label.setAccessibleName("Total model cache size")
        self.cache_location_label = QLabel(str(self._cache_root))
        self.cache_location_label.setObjectName("model_cache_location")
        self.cache_location_label.setAccessibleName("Model cache location")
        self.cache_location_label.setToolTip(str(self._cache_root))
        self.cache_location_label.setAccessibleDescription(str(self._cache_root))
        self.model_list = QListWidget()
        self.model_list.setObjectName("model_list")
        self.model_list.setAccessibleName("V1 models")
        self.model_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.model_list.setItemDelegate(AlignedRowDelegate(self.model_list))
        self.model_list.setUniformItemSizes(True)
        self.download_button = QPushButton("Download weight")
        self.download_button.setObjectName("download_model")
        self.download_button.setAccessibleName("Download selected model weight")
        self.remove_button = QPushButton("Remove downloaded weight")
        self.remove_button.setObjectName("remove_model")
        self.remove_button.setAccessibleName("Remove selected model weight")
        self.outdated_notice_label = QLabel()
        self.outdated_notice_label.setObjectName("outdated_model_notice")
        self.outdated_notice_label.setWordWrap(True)
        self.outdated_notice_label.setVisible(False)
        self.redownload_outdated_button = QPushButton("Re-download outdated")
        self.redownload_outdated_button.setObjectName("redownload_outdated")
        self.redownload_outdated_button.setAccessibleName(
            "Re-download outdated model weights"
        )
        self.delete_outdated_button = QPushButton("Delete outdated")
        self.delete_outdated_button.setObjectName("delete_outdated")
        self.delete_outdated_button.setAccessibleName("Delete outdated model weights")
        self.show_cache_button = QPushButton("Show cache location")
        self.show_cache_button.setObjectName("show_model_cache")
        self.show_cache_button.setAccessibleName("Show model cache location")
        self.close_button = QPushButton("Close")
        self.close_button.setAccessibleName("Close model manager")

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Manage the downloaded V1 model weights."))
        layout.addWidget(self._message)
        layout.addWidget(self.total_size_label)
        layout.addWidget(self.cache_location_label)
        layout.addWidget(self.model_list, 1)
        layout.addWidget(self.outdated_notice_label)
        actions = QHBoxLayout()
        actions.addWidget(self.download_button)
        actions.addWidget(self.remove_button)
        actions.addWidget(self.redownload_outdated_button)
        actions.addWidget(self.delete_outdated_button)
        actions.addWidget(self.show_cache_button)
        actions.addStretch(1)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)

    def _connect_signals(self) -> None:
        self.model_list.currentItemChanged.connect(
            lambda _current, _previous: self._update_actions()
        )
        self.download_button.clicked.connect(self._download_selected)
        self.remove_button.clicked.connect(self._remove_selected)
        self.redownload_outdated_button.clicked.connect(
            self.redownload_outdated_requested.emit
        )
        self.delete_outdated_button.clicked.connect(self.delete_outdated_requested.emit)
        self.show_cache_button.clicked.connect(self.show_cache_requested.emit)
        self.close_button.clicked.connect(self.close)

    @property
    def cache_root(self) -> Path:
        return self._cache_root

    @property
    def entries(self) -> tuple[ModelEntry, ...]:
        return self._entries

    @property
    def outdated_entries(self) -> tuple[ModelEntry, ...]:
        return tuple(
            entry for entry in self._entries if entry.outdated_size_bytes is not None
        )

    @property
    def selected_entry(self) -> ModelEntry | None:
        item = self.model_list.currentItem()
        if item is None:
            return None
        value = item.data(MODEL_ENTRY_ROLE)
        return value if isinstance(value, ModelEntry) else None

    def set_removal_guard(self, guard: Callable[[ModelEntry], str | None]) -> None:
        self._removal_guard = guard
        self._update_actions()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.model_list.setEnabled(not busy)
        self.download_button.setEnabled(not busy)
        self.show_cache_button.setEnabled(not busy)
        self.close_button.setEnabled(not busy)
        self._update_actions()

    def set_message(self, message: str) -> None:
        self._message.setText(message)
        self._message.setAccessibleDescription(message)

    def _active_removal_guard(self, entry: ModelEntry) -> str | None:
        if self._active_model() == entry.model_id:
            return "Cannot remove the model used by the active session."
        return None

    def refresh(self) -> None:
        """Read only the thirteen catalog paths and rebuild the aligned rows."""
        selected_id = self.selected_entry.model_id if self.selected_entry else None
        active_id = self._active_model()
        active_id = active_id if isinstance(active_id, str) else None
        self._entries = tuple(
            _model_entry(self._catalog, self._cache_root, model_id, active_id)
            for model_id in V1_MODEL_IDS
        )
        self.model_list.clear()
        for entry in self._entries:
            row = present_model(entry)
            item = QListWidgetItem(row.display_text)
            install_aligned_row(item, row)
            item.setData(MODEL_ENTRY_ROLE, entry)
            item.setSizeHint(QSize(0, 46))
            self.model_list.addItem(item)
        if selected_id is not None:
            selected_index = next(
                (
                    index
                    for index, entry in enumerate(self._entries)
                    if entry.model_id == selected_id
                ),
                -1,
            )
            if selected_index >= 0:
                self.model_list.setCurrentRow(selected_index)
        if self.model_list.currentItem() is None and self.model_list.count():
            self.model_list.setCurrentRow(0)
        total = sum(
            entry.disk_size_bytes
            if entry.disk_size_bytes is not None
            else entry.outdated_size_bytes or 0
            for entry in self._entries
        )
        self.total_size_label.setText(
            f"Total on disk: {format_source_file_size(total)}"
        )
        self.total_size_label.setAccessibleDescription(
            f"Total on disk: {format_source_file_size(total)}"
        )
        self.set_message(f"{len(self._entries)} V1 model(s); cache: {self._cache_root}")
        self._update_outdated_notice(self.outdated_entries)
        self._update_actions()

    def _update_outdated_notice(self, outdated: tuple[ModelEntry, ...]) -> None:
        if not outdated:
            self.outdated_notice_label.clear()
            self.outdated_notice_label.setVisible(False)
            return
        total = sum(entry.outdated_size_bytes or 0 for entry in outdated)
        versions = ", ".join(
            dict.fromkeys(
                entry.outdated_rembg_version
                for entry in outdated
                if entry.outdated_rembg_version is not None
            )
        )
        self.outdated_notice_label.setText(
            f"{len(outdated)} outdated weight(s) from rembg {versions} use "
            f"{format_source_file_size(total)} on disk and cannot be used by "
            "this version."
        )
        self.outdated_notice_label.setVisible(True)

    def _update_actions(self) -> None:
        entry = self.selected_entry
        blocked = self._removal_guard(entry) if entry is not None else None
        enabled = (
            not self._busy and entry is not None and entry.cached and blocked is None
        )
        self.remove_button.setEnabled(enabled)
        if blocked is not None:
            self.remove_button.setToolTip(blocked)
            self.remove_button.setAccessibleDescription(blocked)
        else:
            self.remove_button.setToolTip(
                "Remove the selected downloaded weight and free its disk space."
            )
            self.remove_button.setAccessibleDescription("")
        self.download_button.setEnabled(
            not self._busy and entry is not None and not entry.cached
        )
        self.download_button.setToolTip(
            "Download the selected weight now instead of waiting for a preview."
        )
        has_outdated = bool(self.outdated_entries)
        self.redownload_outdated_button.setVisible(has_outdated)
        self.delete_outdated_button.setVisible(has_outdated)
        self.redownload_outdated_button.setEnabled(not self._busy and has_outdated)
        self.delete_outdated_button.setEnabled(not self._busy and has_outdated)

    def _download_selected(self) -> None:
        entry = self.selected_entry
        if entry is not None and not entry.cached:
            self.download_requested.emit(entry)

    def _remove_selected(self) -> None:
        entry = self.selected_entry
        if entry is not None and self._removal_guard(entry) is None:
            self.remove_requested.emit(entry)


class _ModelRemovalWorker(QObject):
    removed = Signal(str, bool)
    failed = Signal(str, object)
    finished = Signal()

    def __init__(self, manager: ModelRemovalService, model_id: str) -> None:
        super().__init__()
        self._manager = manager
        self._model_id = model_id

    @Slot()
    def run(self) -> None:
        try:
            self.removed.emit(self._model_id, self._manager.remove(self._model_id))
        except Exception as error:
            self.failed.emit(self._model_id, error)
        finally:
            self.finished.emit()


class _ModelDownloadWorker(QObject):
    downloaded = Signal(str)
    failed = Signal(str, object)
    progressed = Signal(int, int)
    finished = Signal()

    def __init__(self, manager: ModelRemovalService, model_id: str) -> None:
        super().__init__()
        self._manager = manager
        self._model_id = model_id

    @Slot()
    def run(self) -> None:
        try:
            self._manager.fetch(self._model_id, self._report)
        except Exception as error:
            self.failed.emit(self._model_id, error)
        else:
            self.downloaded.emit(self._model_id)
        finally:
            self.finished.emit()

    def _report(self, completed: int, total: int) -> None:
        self.progressed.emit(completed, total)


class _ObsoleteRemovalWorker(QObject):
    removed = Signal(int)
    failed = Signal(object)
    finished = Signal()

    def __init__(self, manager: ModelRemovalService) -> None:
        super().__init__()
        self._manager = manager

    @Slot()
    def run(self) -> None:
        try:
            self.removed.emit(self._manager.remove_obsolete_versions())
        except Exception as error:
            self.failed.emit(error)
        finally:
            self.finished.emit()


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
        self.dialog.redownload_outdated_requested.connect(
            self._redownload_outdated_requested
        )
        self.dialog.delete_outdated_requested.connect(self._delete_outdated_requested)
        self.dialog.show_cache_requested.connect(self._show_cache)
        self._remove_thread: QThread | None = None
        self._remove_worker: (
            _ModelRemovalWorker | _ModelDownloadWorker | _ObsoleteRemovalWorker | None
        ) = None
        self._removal_entry: ModelEntry | None = None
        self._pending_outdated_ids: list[str] = []
        self._outdated_entries: dict[str, ModelEntry] = {}
        self._outdated_action: str | None = None
        self._start_outdated_after_thread = False

    def set_dialog_parent(self, parent: QWidget) -> None:
        self.dialog.setParent(parent, Qt.WindowType.Dialog)

    def open(self) -> None:
        self.dialog.refresh()
        self.dialog.open()

    def close(self) -> None:
        self._clear_outdated_state()
        thread = self._remove_thread
        if thread is not None:
            thread.quit()
            thread.wait(5000)
            self._remove_thread = None
        self.dialog.close()

    def _clear_outdated_state(self) -> None:
        self._pending_outdated_ids.clear()
        self._outdated_entries.clear()
        self._outdated_action = None
        self._start_outdated_after_thread = False

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
        if self._remove_thread is not None:
            return
        if self._store.state.job.phase is not JobState.IDLE:
            self.dialog.set_message("Cannot download a model while a job is running.")
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
        entries = self.dialog.outdated_entries
        if not entries:
            return
        if self._store.state.job.phase is not JobState.IDLE:
            self.dialog.set_message("Cannot remove a model while a job is running.")
            return
        if manager is None:
            self.dialog.set_message("Model removal is unavailable in this runtime.")
            return
        total = sum(entry.outdated_size_bytes or 0 for entry in entries)
        answer = QMessageBox.question(
            self.dialog,
            "Delete outdated model weights?",
            f"Delete {len(entries)} outdated model weight(s)?\n"
            f"This frees {format_source_file_size(total)}.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        self._outdated_action = "delete"
        self._start_obsolete_removal(manager)

    def _redownload_outdated_requested(self) -> None:
        manager = self._manager
        entries = self.dialog.outdated_entries
        if not entries:
            return
        if self._store.state.job.phase is not JobState.IDLE:
            self.dialog.set_message("Cannot download a model while a job is running.")
            return
        if manager is None:
            self.dialog.set_message("Model removal is unavailable in this runtime.")
            return
        self._outdated_action = "redownload"
        self._pending_outdated_ids = [entry.model_id for entry in entries]
        self._outdated_entries = {entry.model_id: entry for entry in entries}
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
        if self._outdated_action == "redownload":
            if self._pending_outdated_ids[:1] == [model_id]:
                self._pending_outdated_ids.pop(0)
            self._start_outdated_after_thread = bool(self._pending_outdated_ids)
            if not self._pending_outdated_ids:
                self._outdated_action = None

    @Slot(str, object)
    def _download_failed(self, model_id: str, error: object) -> None:
        entry = self._removal_entry
        if entry is None or entry.model_id != model_id:
            return
        self._clear_outdated_state()
        self.dialog.set_message(f"Could not download the weight: {error}")
        QMessageBox.warning(self.dialog, "Could not download model", str(error))

    @Slot(int)
    def _obsolete_removal_succeeded(self, removed: int) -> None:
        action = self._outdated_action
        if action is None:
            return
        self.dialog.refresh()
        if action == "redownload":
            self.dialog.set_message(
                f"Removed {removed} outdated model version(s); re-downloading…"
            )
            self._start_outdated_after_thread = True
            return
        self._clear_outdated_state()
        self.dialog.set_message(
            f"Removed {removed} outdated model version(s) from the cache."
        )

    @Slot(object)
    def _obsolete_removal_failed(self, error: object) -> None:
        self._clear_outdated_state()
        self.dialog.set_message(f"Could not remove outdated weights: {error}")
        QMessageBox.warning(self.dialog, "Could not remove outdated models", str(error))

    def _start_next_outdated_download(self) -> None:
        if not self._pending_outdated_ids:
            self._outdated_action = None
            return
        entry = self._outdated_entries.get(self._pending_outdated_ids[0])
        if entry is not None:
            self._download_requested(entry)

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
        start_outdated = self._start_outdated_after_thread
        self._start_outdated_after_thread = False
        self.dialog.set_busy(False)
        self._remove_worker = None
        self._remove_thread = None
        if start_outdated:
            self._start_next_outdated_download()

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


def manager_active_id() -> str | None:
    """Default active-model callback used by a read-only fallback dialog."""
    return None


def _model_entry(
    catalog: ModelCatalog,
    cache_root: Path,
    model_id: str,
    active_id: str | None,
) -> ModelEntry:
    spec = catalog.get(model_id)
    artifact = spec.artifact
    if artifact is None:
        raise ValueError(f"V1 model {model_id!r} has no artifact")
    artifact_path = (
        cache_root / catalog.rembg_version / model_id / artifact.runtime_filename
    )
    disk_size_bytes = _regular_file_size(artifact_path)
    outdated_size_bytes: int | None = None
    outdated_rembg_version: str | None = None
    if disk_size_bytes is None:
        for version in catalog.obsolete_rembg_versions:
            outdated_path = cache_root / version / model_id / artifact.runtime_filename
            outdated_size_bytes = _regular_file_size(outdated_path)
            if outdated_size_bytes is not None:
                outdated_rembg_version = version
                break
    return ModelEntry(
        model_id=model_id,
        display_name=spec.display_name,
        download_size_bytes=artifact.size_bytes,
        artifact_path=artifact_path,
        disk_size_bytes=disk_size_bytes,
        active=model_id == active_id,
        outdated_size_bytes=outdated_size_bytes,
        outdated_rembg_version=outdated_rembg_version,
    )


def _regular_file_size(path: Path) -> int | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None
