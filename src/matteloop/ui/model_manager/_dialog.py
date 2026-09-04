from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QSize, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from matteloop.core.parameters import V1_MODEL_IDS
from matteloop.jobs.models.catalog import ModelCatalog
from matteloop.ui.aligned_rows import AlignedRowDelegate, install_aligned_row
from matteloop.ui.source_presentation import format_source_file_size

from ._entries import (
    MODEL_ENTRY_ROLE,
    ModelEntry,
    _model_entry,
    _obsolete_directory_size,
    present_model,
)


class ModelManagerDialog(QDialog):
    """List the V1 model cache and expose actions for its selected row."""

    download_requested = Signal(object)
    remove_requested = Signal(object)
    delete_outdated_requested = Signal()
    redownload_outdated_requested = Signal()
    cancel_requested = Signal()
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
        self._obsolete_size_bytes = 0
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
        self.redownload_outdated_button = QPushButton("Re-download outdated")
        self.redownload_outdated_button.setObjectName("redownload_outdated")
        self.redownload_outdated_button.setAccessibleName(
            "Re-download outdated model weights"
        )
        self.outdated_notice_label = QLabel()
        self.outdated_notice_label.setObjectName("outdated_model_notice")
        self.outdated_notice_label.setWordWrap(True)
        self.outdated_notice_label.setVisible(False)
        self.delete_outdated_button = QPushButton("Delete outdated")
        self.delete_outdated_button.setObjectName("delete_outdated")
        self.delete_outdated_button.setAccessibleName("Delete outdated model weights")
        self.show_cache_button = QPushButton("Show cache location")
        self.show_cache_button.setObjectName("show_model_cache")
        self.show_cache_button.setAccessibleName("Show model cache location")
        self.close_button = QPushButton("Close")
        self.close_button.setAccessibleName("Close model manager")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("cancel_model_download")
        self.cancel_button.setAccessibleName("Cancel the running model download")
        self.cancel_button.setVisible(False)

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
        actions.addWidget(self.cancel_button)
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
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
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
    def redownload_entries(self) -> tuple[ModelEntry, ...]:
        return tuple(
            entry
            for entry in self._entries
            if entry.outdated_size_bytes is not None and not entry.cached
        )

    @property
    def obsolete_size_bytes(self) -> int:
        return self._obsolete_size_bytes

    @property
    def obsolete_versions(self) -> tuple[str, ...]:
        return self._catalog.obsolete_rembg_versions

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

    def set_busy(self, busy: bool, *, cancellable: bool = False) -> None:
        self._busy = busy
        self.model_list.setEnabled(not busy)
        self.download_button.setEnabled(not busy)
        self.show_cache_button.setEnabled(not busy)
        self.close_button.setEnabled(not busy)
        if busy and cancellable:
            self.cancel_button.setText("Cancel")
            self.cancel_button.setVisible(True)
            self.cancel_button.setEnabled(True)
        else:
            self.cancel_button.setText("Cancel")
            self.cancel_button.setVisible(False)
            self.cancel_button.setEnabled(True)
        self._update_actions()

    def set_cancelling(self) -> None:
        self.cancel_button.setText("Cancelling…")
        self.cancel_button.setEnabled(False)
        self.set_message("Cancelling…")

    def reject(self) -> None:
        if self._busy:
            self.cancel_requested.emit()
            return
        super().reject()

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
        self._obsolete_size_bytes = _obsolete_directory_size(
            self._cache_root, self._catalog.obsolete_rembg_versions
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
        total = sum(entry.disk_size_bytes or 0 for entry in self._entries)
        total += self._obsolete_size_bytes
        self.total_size_label.setText(
            f"Total on disk: {format_source_file_size(total)}"
        )
        self.total_size_label.setAccessibleDescription(
            f"Total on disk: {format_source_file_size(total)}"
        )
        self.set_message(f"{len(self._entries)} V1 model(s); cache: {self._cache_root}")
        self._update_outdated_notice(self._obsolete_size_bytes)
        self._update_actions()

    def _update_outdated_notice(self, size_bytes: int) -> None:
        if size_bytes <= 0:
            self.outdated_notice_label.clear()
            self.outdated_notice_label.setVisible(False)
            return
        versions = ", ".join(self._catalog.obsolete_rembg_versions)
        self.outdated_notice_label.setText(
            f"Outdated weights from rembg {versions} occupy "
            f"{format_source_file_size(size_bytes)} on disk and cannot be used by "
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
        has_outdated = self._obsolete_size_bytes > 0
        self.delete_outdated_button.setVisible(has_outdated)
        self.delete_outdated_button.setEnabled(not self._busy and has_outdated)
        has_redownloadable = bool(self.redownload_entries)
        self.redownload_outdated_button.setVisible(has_redownloadable)
        self.redownload_outdated_button.setEnabled(
            not self._busy and has_redownloadable
        )

    def _download_selected(self) -> None:
        entry = self.selected_entry
        if entry is not None and not entry.cached:
            self.download_requested.emit(entry)

    def _remove_selected(self) -> None:
        entry = self.selected_entry
        if entry is not None and self._removal_guard(entry) is None:
            self.remove_requested.emit(entry)
