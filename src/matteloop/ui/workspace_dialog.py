"""Asynchronous-friendly promoted cut-set picker dialog."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QSize, Signal
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

from matteloop.core.errors import AppError
from matteloop.jobs.models.catalog import ModelCatalog
from matteloop.jobs.workspace import (
    WorkspaceSummary,
    detect_external_edits,
    list_workspaces,
)
from matteloop.ui.aligned_rows import (
    ROW_DATA_ROLE,
    AlignedRowDelegate,
    install_aligned_row,
)
from matteloop.ui.workspace_presentation import present_workspace

SUMMARY_ROLE = ROW_DATA_ROLE + 10


class WorkspacePickerDialog(QDialog):
    """List validated promoted sets and emit actions for the selected row."""

    use_requested = Signal(object)
    open_requested = Signal(object)
    delete_requested = Signal(object)

    def __init__(self, catalog: ModelCatalog, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._catalog = catalog
        self._summaries: tuple[WorkspaceSummary, ...] = ()
        self._output_directory: Path | None = None
        self._build_widgets()
        self._build_layout()
        self._connect_signals()
        self._update_actions()

    def _build_widgets(self) -> None:
        self.setObjectName("workspace_picker")
        self.setWindowTitle(
            QCoreApplication.translate("WorkspacePickerDialog", "Promoted cut sets")
        )
        self.setAccessibleName(
            QCoreApplication.translate("WorkspacePickerDialog", "Promoted cut sets")
        )
        self.resize(980, 360)
        self._message = QLabel()
        self._message.setProperty("secondary", True)
        self.cut_set_list = QListWidget()
        self.cut_set_list.setObjectName("cut_set_list")
        self.cut_set_list.setAccessibleName(
            QCoreApplication.translate("WorkspacePickerDialog", "Promoted cut sets")
        )
        self.cut_set_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.cut_set_list.setItemDelegate(AlignedRowDelegate(self.cut_set_list))
        self.cut_set_list.setUniformItemSizes(True)
        self.use_button = QPushButton(
            QCoreApplication.translate("WorkspacePickerDialog", "Use this set")
        )
        self.use_button.setAccessibleName(
            QCoreApplication.translate("WorkspacePickerDialog", "Use selected cut set")
        )
        self.open_button = QPushButton(
            QCoreApplication.translate("WorkspacePickerDialog", "Open folder")
        )
        self.open_button.setAccessibleName(
            QCoreApplication.translate(
                "WorkspacePickerDialog", "Open selected cut folder"
            )
        )
        self.delete_button = QPushButton(
            QCoreApplication.translate("WorkspacePickerDialog", "Delete set")
        )
        self.delete_button.setAccessibleName(
            QCoreApplication.translate(
                "WorkspacePickerDialog", "Delete selected cut set"
            )
        )
        self.close_button = QPushButton(
            QCoreApplication.translate("WorkspacePickerDialog", "Close")
        )

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                QCoreApplication.translate(
                    "WorkspacePickerDialog",
                    "Choose a validated cut set for this output directory.",
                )
            )
        )
        layout.addWidget(self._message)
        layout.addWidget(self.cut_set_list, 1)
        actions = QHBoxLayout()
        actions.addWidget(self.use_button)
        actions.addWidget(self.open_button)
        actions.addWidget(self.delete_button)
        actions.addStretch(1)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)

    def _connect_signals(self) -> None:
        self.cut_set_list.currentItemChanged.connect(
            lambda _current, _previous: self._update_actions()
        )
        self.cut_set_list.itemDoubleClicked.connect(lambda _item: self._use_selected())
        self.use_button.clicked.connect(self._use_selected)
        self.open_button.clicked.connect(self._open_selected)
        self.delete_button.clicked.connect(self._delete_selected)
        self.close_button.clicked.connect(self.close)

    @property
    def summaries(self) -> tuple[WorkspaceSummary, ...]:
        return self._summaries

    def load(self, output_directory: Path) -> None:
        """Reload and re-scan rows so external edits are labelled immediately."""
        self._output_directory = output_directory
        try:
            listing = list_workspaces(output_directory)
            refreshed: list[WorkspaceSummary] = []
            for summary in listing:
                manifest = detect_external_edits(summary.workspace)
                size_bytes = sum(frame.size_bytes for frame in manifest.frames)
                size_bytes += len(manifest.to_json_bytes())
                refreshed.append(
                    replace(summary, manifest=manifest, size_bytes=size_bytes)
                )
        except (AppError, OSError) as error:
            self._summaries = ()
            self._message.setText(
                QCoreApplication.translate(
                    "WorkspacePickerDialog", "Could not read cut sets: %s"
                )
                % error
            )
            self.cut_set_list.clear()
            self._update_actions()
            return
        self._summaries = tuple(refreshed)
        self.cut_set_list.clear()
        for summary in self._summaries:
            row = present_workspace(summary, self._catalog)
            item = QListWidgetItem(row.display_text)
            install_aligned_row(item, row)
            item.setData(SUMMARY_ROLE, summary)
            item.setSizeHint(QSize(0, 44))
            self.cut_set_list.addItem(item)
        if self.cut_set_list.count():
            self.cut_set_list.setCurrentRow(0)
        if self._summaries:
            summary_count = len(self._summaries)
            self._message.setText(
                self.tr(
                    "%n promoted cut set(s) in %1",
                    "",
                    summary_count,
                ).replace("%1", str(output_directory))
            )
        else:
            self._message.setText(
                QCoreApplication.translate(
                    "WorkspacePickerDialog", "No promoted cut sets found."
                )
            )
        self._update_actions()

    def selected_summary(self) -> WorkspaceSummary | None:
        item = self.cut_set_list.currentItem()
        if item is None:
            return None
        value = item.data(SUMMARY_ROLE)
        return value if isinstance(value, WorkspaceSummary) else None

    def _update_actions(self) -> None:
        enabled = self.selected_summary() is not None
        self.use_button.setEnabled(enabled)
        self.open_button.setEnabled(enabled)
        self.delete_button.setEnabled(enabled)

    def _use_selected(self) -> None:
        summary = self.selected_summary()
        if summary is not None:
            self.use_requested.emit(summary)

    def _open_selected(self) -> None:
        summary = self.selected_summary()
        if summary is not None:
            self.open_requested.emit(summary)

    def _delete_selected(self) -> None:
        summary = self.selected_summary()
        if summary is not None:
            self.delete_requested.emit(summary)
