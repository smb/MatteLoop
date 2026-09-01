"""Model-cache dialog and controller for the existing session manager."""

from __future__ import annotations

import stat
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
from matteloop.ui.source_presentation import format_source_file_size

MODEL_ENTRY_ROLE = ROW_DATA_ROLE + 11


class ModelRemovalService(Protocol):
    @property
    def active_id(self) -> str | None: ...

    def remove(self, model_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """One catalog model plus the cache facts shown by the manager."""

    model_id: str
    display_name: str
    download_size_bytes: int
    artifact_path: Path
    disk_size_bytes: int | None
    active: bool

    @property
    def cached(self) -> bool:
        return self.disk_size_bytes is not None


def present_model(entry: ModelEntry) -> AlignedRow:
    """Present one model with aligned metadata and spoken status words."""
    cache_status = "cached locally" if entry.cached else "not cached"
    active_status = "active model" if entry.active else "not active"
    size = format_source_file_size(entry.download_size_bytes)
    detail = f"{entry.display_name}; {size}; {cache_status}; {active_status}"
    glyph = "◆" if entry.active else ("✓" if entry.cached else "↓")
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

    remove_requested = Signal(object)
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
        self.remove_button = QPushButton("Remove downloaded weight")
        self.remove_button.setObjectName("remove_model")
        self.remove_button.setAccessibleName("Remove selected model weight")
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
        actions = QHBoxLayout()
        actions.addWidget(self.remove_button)
        actions.addWidget(self.show_cache_button)
        actions.addStretch(1)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)

    def _connect_signals(self) -> None:
        self.model_list.currentItemChanged.connect(
            lambda _current, _previous: self._update_actions()
        )
        self.remove_button.clicked.connect(self._remove_selected)
        self.show_cache_button.clicked.connect(self.show_cache_requested.emit)
        self.close_button.clicked.connect(self.close)

    @property
    def cache_root(self) -> Path:
        return self._cache_root

    @property
    def entries(self) -> tuple[ModelEntry, ...]:
        return self._entries

    @property
    def selected_entry(self) -> ModelEntry | None:
        item = self.model_list.currentItem()
        if item is None:
            return None
        value = item.data(MODEL_ENTRY_ROLE)
        return value if isinstance(value, ModelEntry) else None

    def set_removal_guard(
        self, guard: Callable[[ModelEntry], str | None]
    ) -> None:
        self._removal_guard = guard
        self._update_actions()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.model_list.setEnabled(not busy)
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
            entry.disk_size_bytes or 0
            for entry in self._entries
        )
        self.total_size_label.setText(
            f"Total on disk: {format_source_file_size(total)}"
        )
        self.total_size_label.setAccessibleDescription(
            f"Total on disk: {format_source_file_size(total)}"
        )
        self.set_message(
            f"{len(self._entries)} V1 model(s); cache: {self._cache_root}"
        )
        self._update_actions()

    def _update_actions(self) -> None:
        entry = self.selected_entry
        blocked = self._removal_guard(entry) if entry is not None else None
        enabled = (
            not self._busy
            and entry is not None
            and entry.cached
            and blocked is None
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
            manager_active_id
            if manager is None
            else lambda: manager.active_id
        )
        self.dialog = ModelManagerDialog(
            catalog,
            cache_root,
            active_model=active_model,
            parent=dialog_parent,
        )
        self.dialog.set_removal_guard(self._removal_block_reason)
        self.dialog.remove_requested.connect(self._remove_requested)
        self.dialog.show_cache_requested.connect(self._show_cache)
        self._remove_thread: QThread | None = None
        self._remove_worker: _ModelRemovalWorker | None = None
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
            self.dialog.set_message("The selected weight was already absent.")
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
        cache_root
        / catalog.rembg_version
        / model_id
        / artifact.runtime_filename
    )
    return ModelEntry(
        model_id,
        spec.display_name,
        artifact.size_bytes,
        artifact_path,
        _regular_file_size(artifact_path),
        model_id == active_id,
    )


def _regular_file_size(path: Path) -> int | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None
