"""Preferences dialog for reducer-owned application settings."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QSettings, QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QComboBox,
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
from matteloop.ui.i18n import (
    SUPPORTED_LANGUAGES,
    persist_language,
    selected_language,
    translate_language_name,
)
from matteloop.ui.ports import StateStore, WindowServices


class SettingsDialog(QDialog):
    """Edit the output directory already held by the application state."""

    def __init__(
        self,
        store: StateStore,
        services: WindowServices,
        parent: QWidget | None = None,
        *,
        settings: QSettings | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settings_dialog")
        self.setWindowTitle(QCoreApplication.translate("SettingsDialog", "Preferences"))
        self.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Preferences")
        )
        self.setMinimumWidth(560)
        self._store = store
        self._services = services
        self._settings = settings or QSettings()
        self._directory: Path | None = None

        self._build_widgets()
        self._build_layout()
        self._connect_controls()
        self.load()

    def _build_widgets(self) -> None:
        self.output_directory_edit = compact_field(MiddleElidingLineEdit())
        self.output_directory_edit.setObjectName("output_directory")
        self.output_directory_edit.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Output directory")
        )
        self.output_directory_edit.setAccessibleDescription(
            QCoreApplication.translate(
                "SettingsDialog", "Directory used for output files"
            )
        )
        self.output_directory_edit.setProperty("mono", True)
        self.output_directory_edit.setReadOnly(True)
        self.choose_output_directory_button = QPushButton(
            QCoreApplication.translate("SettingsDialog", "Choose…")
        )
        self.choose_output_directory_button.setObjectName("choose_output_directory")
        self.choose_output_directory_button.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Choose output directory")
        )
        self.clear_output_directory_button = QPushButton(
            QCoreApplication.translate("SettingsDialog", "Clear")
        )
        self.clear_output_directory_button.setObjectName("clear_output_directory")
        self.clear_output_directory_button.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Clear output directory")
        )
        self.language_selector = QComboBox()
        self.language_selector.setObjectName("interface_language")
        self.language_selector.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Interface language")
        )
        for language in SUPPORTED_LANGUAGES:
            self.language_selector.addItem(translate_language_name(language), language)
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.button_box.setObjectName("settings_actions")
        self.button_box.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Preferences actions")
        )

    def _build_layout(self) -> None:
        self.description_label = QLabel()
        self.description_label.setObjectName("settings_description")
        self.description_label.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Preferences description")
        )
        self.description_label.setProperty("secondary", True)
        self.language_note_label = QLabel(
            QCoreApplication.translate(
                "SettingsDialog",
                "Interface language changes apply after restarting MatteLoop.",
            )
        )
        self.language_note_label.setObjectName("language_restart_note")
        self.language_note_label.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Language restart note")
        )
        self.language_note_label.setProperty("secondary", True)

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
        label = QLabel(QCoreApplication.translate("SettingsDialog", "Output directory"))
        label.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Output directory label")
        )
        label.setBuddy(self.output_directory_edit)
        form.addRow(label, directory_row)
        language_label = QLabel(
            QCoreApplication.translate("SettingsDialog", "Interface language")
        )
        language_label.setBuddy(self.language_selector)
        form.addRow(language_label, self.language_selector)

        layout = QVBoxLayout(self)
        layout.addWidget(self.description_label)
        layout.addWidget(self.language_note_label)
        layout.addLayout(form)
        layout.addWidget(self.button_box)

    def _connect_controls(self) -> None:
        self.choose_output_directory_button.clicked.connect(self._choose_directory)
        self.clear_output_directory_button.clicked.connect(self._clear_directory)
        self.language_selector.currentIndexChanged.connect(self._language_changed)
        self.button_box.rejected.connect(self.reject)
        close_button = self.button_box.button(QDialogButtonBox.StandardButton.Close)
        if close_button is not None:
            close_button.setAccessibleName(
                QCoreApplication.translate("SettingsDialog", "Close preferences")
            )
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
        language = selected_language(self._settings)
        with QSignalBlocker(self.language_selector):
            self.language_selector.setCurrentIndex(
                self.language_selector.findData(language)
            )
        self.output_directory_edit.setText(
            str(directory) if directory is not None else ""
        )
        self.output_directory_edit.setEnabled(controls_enabled)
        self.choose_output_directory_button.setEnabled(controls_enabled)
        self.clear_output_directory_button.setEnabled(
            controls_enabled and directory is not None
        )
        if controls_enabled:
            self.description_label.setText(
                QCoreApplication.translate(
                    "SettingsDialog", "Choose where rendered output files are saved."
                )
            )
        else:
            self.description_label.setText(
                QCoreApplication.translate(
                    "SettingsDialog",
                    "Output directory controls are disabled while a job is running.",
                )
            )

    def _choose_directory(self) -> None:
        current = str(self._directory or Path.home())
        selected = QFileDialog.getExistingDirectory(
            self,
            QCoreApplication.translate("SettingsDialog", "Choose output directory"),
            current,
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

    def _language_changed(self, index: int) -> None:
        language = self.language_selector.itemData(index)
        if isinstance(language, str):
            persist_language(self._settings, language)
