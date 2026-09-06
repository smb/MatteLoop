"""Preferences dialog for reducer-owned application settings."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from matteloop.core.parameters import OutputDirectoryChanged
from matteloop.core.state import JobState
from matteloop.ui.compact_widgets import MiddleElidingLineEdit, compact_field
from matteloop.ui.ports import StateStore, WindowServices


class SettingsDialog(QDialog):
    """Edit the output directory already held by the application state."""

    def __init__(
        self,
        store: StateStore,
        services: WindowServices,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settings_dialog")
        self.setWindowTitle("Preferences")
        self.setAccessibleName("Preferences")
        self.setMinimumWidth(560)
        self._store = store
        self._services = services
        self._directory: Path | None = None

        self.output_directory_edit = compact_field(MiddleElidingLineEdit())
        self.output_directory_edit.setObjectName("output_directory")
        self.output_directory_edit.setAccessibleName("Output directory")
        self.output_directory_edit.setAccessibleDescription(
            "Directory used for output files"
        )
        self.output_directory_edit.setProperty("mono", True)
        self.output_directory_edit.setReadOnly(True)

        self.choose_output_directory_button = QPushButton("Choose…")
        self.choose_output_directory_button.setObjectName(
            "choose_output_directory"
        )
        self.choose_output_directory_button.setAccessibleName(
            "Choose output directory"
        )
        self.clear_output_directory_button = QPushButton("Clear")
        self.clear_output_directory_button.setObjectName("clear_output_directory")
        self.clear_output_directory_button.setAccessibleName(
            "Clear output directory"
        )
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close
        )
        self.button_box.setObjectName("settings_actions")
        self.button_box.setAccessibleName("Preferences actions")
        self._build_layout()
        self._connect_controls()
        self.load()

    def _build_layout(self) -> None:
        self.description_label = QLabel()
        self.description_label.setObjectName("settings_description")
        self.description_label.setAccessibleName("Preferences description")
        self.description_label.setProperty("secondary", True)

        directory_row = QWidget()
        directory_layout = QHBoxLayout(directory_row)
        directory_layout.setContentsMargins(0, 0, 0, 0)
        directory_layout.setSpacing(8)
        directory_layout.addWidget(self.output_directory_edit, 1)
        directory_layout.addWidget(self.choose_output_directory_button)
        directory_layout.addWidget(self.clear_output_directory_button)

        form = QFormLayout()
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        label = QLabel("Output directory")
        label.setAccessibleName("Output directory label")
        label.setBuddy(self.output_directory_edit)
        form.addRow(label, directory_row)

        layout = QVBoxLayout(self)
        layout.addWidget(self.description_label)
        layout.addLayout(form)
        layout.addWidget(self.button_box)

    def _connect_controls(self) -> None:
        self.choose_output_directory_button.clicked.connect(self._choose_directory)
        self.clear_output_directory_button.clicked.connect(self._clear_directory)
        self.button_box.rejected.connect(self.reject)
        close_button = self.button_box.button(
            QDialogButtonBox.StandardButton.Close
        )
        if close_button is not None:
            close_button.setAccessibleName("Close preferences")
        self.setTabOrder(
            self.output_directory_edit,
            self.choose_output_directory_button,
        )
        self.setTabOrder(
            self.choose_output_directory_button,
            self.clear_output_directory_button,
        )
        if close_button is not None:
            self.setTabOrder(self.clear_output_directory_button, close_button)

    def load(self) -> None:
        """Reload the current reducer-owned value before showing the dialog."""
        state = self._store.state
        directory = state.parameters.output_directory
        controls_enabled = state.job.phase is JobState.IDLE
        self._directory = directory
        self.output_directory_edit.setText(
            str(directory) if directory is not None else ""
        )
        self.output_directory_edit.setEnabled(controls_enabled)
        self.choose_output_directory_button.setEnabled(controls_enabled)
        self.clear_output_directory_button.setEnabled(
            controls_enabled and directory is not None
        )
        self.description_label.setText(
            "Choose where rendered output files are saved. "
            "Changes apply immediately."
            if controls_enabled
            else "Output directory controls are disabled while a job is running."
        )

    def _choose_directory(self) -> None:
        current = str(self._directory or Path.home())
        selected = QFileDialog.getExistingDirectory(
            self, "Choose output directory", current
        )
        if selected:
            self._set_directory(Path(selected))

    def _clear_directory(self) -> None:
        self._set_directory(None)

    def _set_directory(self, directory: Path | None) -> None:
        self._directory = directory
        self.output_directory_edit.setText(str(directory) if directory else "")
        self.clear_output_directory_button.setEnabled(directory is not None)
        self._services.dispatch(OutputDirectoryChanged(directory))
