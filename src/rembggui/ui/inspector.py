"""Continuous, truthful inspector shell without editor-owned domain state."""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

_DISCLOSURES = (
    ("segmentation", "Segmentation", True),
    ("time_sampling", "Time & Sampling", True),
    ("crop_cleanup", "Crop & Cleanup", False),
    ("output", "Output", True),
    ("workspace", "Workspace", False),
)


class Inspector(QFrame):
    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("inspector")
        self._settings = settings
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("inspector_scroll")
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll_area.setWidgetResizable(True)
        content = QWidget()
        content.setObjectName("inspector_content")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(16, 12, 16, 16)
        content_layout.setSpacing(4)
        self.disclosures: dict[str, tuple[QToolButton, QWidget]] = {}
        self.manage_models = QPushButton("Manage Models…")
        self.manage_models.setObjectName("manage_models")
        self.manage_models.setAccessibleName("Manage Models…")
        self.manage_workspaces = QPushButton("Manage Workspaces…")
        self.manage_workspaces.setObjectName("manage_workspaces")
        self.manage_workspaces.setAccessibleName("Manage Workspaces…")
        self.edited_cut_recovery = QLabel("Edited cuts need attention")
        self.edited_cut_recovery.setObjectName("edited_cut_recovery")
        self.edited_cut_recovery.setAccessibleName("Edited cut recovery")
        self.edited_cut_recovery.setProperty("secondary", True)
        self.edited_cut_recovery.hide()
        for key, title, default in _DISCLOSURES:
            section = self._section(key, title, default)
            content_layout.addWidget(section)
        content_layout.addStretch(1)
        self.scroll_area.setWidget(content)
        outer.addWidget(self.scroll_area)

    def _section(self, key: str, title: str, default: bool) -> QFrame:
        section = QFrame()
        section.setObjectName(f"{key}_section")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        button = QToolButton()
        button.setText(title)
        button.setCheckable(True)
        button.setObjectName(f"{key}_disclosure")
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 8, 8)
        copy = QLabel("Available when a video is ready")
        copy.setProperty("secondary", True)
        body_layout.addWidget(copy)
        if key == "segmentation":
            body_layout.addWidget(self.manage_models)
        if key == "workspace":
            body_layout.addWidget(self.edited_cut_recovery)
            body_layout.addWidget(self.manage_workspaces)
        checked = self._read_bool(f"inspector/{key}", default)
        button.setChecked(checked)
        body.setVisible(checked)
        button.toggled.connect(body.setVisible)
        button.toggled.connect(
            lambda value, name=key: self._settings.setValue(f"inspector/{name}", value)
        )
        layout.addWidget(button)
        layout.addWidget(body)
        self.disclosures[key] = (button, body)
        return section

    def _read_bool(self, name: str, default: bool) -> bool:
        value = self._settings.value(name, default)
        return value if type(value) is bool else default
