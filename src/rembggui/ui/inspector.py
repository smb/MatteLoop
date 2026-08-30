"""Continuous, truthful inspector shell without editor-owned domain state."""

from __future__ import annotations

from PySide6.QtCore import QSettings, QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from rembggui.core.crop_state import CropChanged, CropToggleChanged, ResetCrop
from rembggui.core.specs import CropSpec
from rembggui.ui.crop_presentation import CropPresentation

_DISCLOSURES = (
    ("segmentation", "Segmentation", True),
    ("time_sampling", "Time & Sampling", True),
    ("crop_cleanup", "Crop & Cleanup", False),
    ("output", "Output", True),
    ("workspace", "Workspace", False),
)


class Inspector(QFrame):
    command_requested = Signal(object)

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("inspector")
        self.setAccessibleName("Processing settings")
        self._settings = settings
        self._build_crop_controls()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("inspector_scroll")
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll_area.setWidgetResizable(True)
        content = QWidget()
        content.setObjectName("inspector_content")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(16, 12, 16, 16)
        content_layout.setSpacing(4)
        self.disclosures: dict[str, tuple[QToolButton, QWidget]] = {}
        self.edited_cut_recovery = QPushButton("Retry Rebuild")
        self.edited_cut_recovery.setObjectName("edited_cut_recovery")
        self.edited_cut_recovery.setAccessibleName("Edited cut recovery")
        self.edited_cut_recovery.setToolTip(
            "Edited cut frames could not be validated. Retry the rebuild scan."
        )
        self.edited_cut_recovery.hide()
        self.rebuild_button = QPushButton("Rebuild from edited cuts")
        self.rebuild_button.setObjectName("rebuild_action")
        self.rebuild_button.setAccessibleName("Rebuild from edited cuts")
        self.rebuild_button.setMinimumHeight(40)
        self.manage_models = QPushButton("Manage Models…")
        self.manage_models.setObjectName("manage_models")
        self.manage_models.setAccessibleName("Manage Models…")
        self.manage_workspaces = QPushButton("Manage Workspaces…")
        self.manage_workspaces.setObjectName("manage_workspaces")
        self.manage_workspaces.setAccessibleName("Manage Workspaces…")
        for key, title, default in _DISCLOSURES:
            section = self._section(key, title, default)
            content_layout.addWidget(section)
        content_layout.addStretch(1)
        self.scroll_area.setWidget(content)
        outer.addWidget(self.scroll_area)

    def _build_crop_controls(self) -> None:
        self._crop_syncing = False
        self.crop_toggle = QCheckBox("Crop")
        self.crop_toggle.setObjectName("crop_toggle")
        self.crop_toggle.setAccessibleName("Crop overlay")
        self.crop_toggle.setChecked(True)
        self.crop_reset_button = QPushButton("Reset Crop")
        self.crop_reset_button.setObjectName("crop_reset")
        self.crop_reset_button.setAccessibleName("Reset crop")
        self.crop_x_spinbox = self._crop_spinbox("x")
        self.crop_y_spinbox = self._crop_spinbox("y")
        self.crop_width_spinbox = self._crop_spinbox("width")
        self.crop_height_spinbox = self._crop_spinbox("height")
        self.crop_fields = {
            "x": self.crop_x_spinbox,
            "y": self.crop_y_spinbox,
            "width": self.crop_width_spinbox,
            "height": self.crop_height_spinbox,
        }
        self.crop_toggle.toggled.connect(self._crop_toggle_changed)
        self.crop_reset_button.clicked.connect(
            lambda: self.command_requested.emit(ResetCrop())
        )
        for field in self.crop_fields.values():
            field.valueChanged.connect(self._crop_fields_changed)

    def apply_crop(
        self, presentation: CropPresentation | None, enabled: bool, editable: bool
    ) -> None:
        """Render reducer-owned crop values into standard inspector widgets."""
        self._crop_syncing = True
        blockers = [QSignalBlocker(self.crop_toggle), *(
            QSignalBlocker(field) for field in self.crop_fields.values()
        )]
        try:
            self.crop_toggle.setChecked(enabled)
            if presentation is None:
                for field in self.crop_fields.values():
                    field.setRange(0, 1)
                    field.setValue(0)
            else:
                crop = presentation.crop
                self.crop_x_spinbox.setRange(0, max(0, presentation.width - 1))
                self.crop_y_spinbox.setRange(0, max(0, presentation.height - 1))
                self.crop_width_spinbox.setRange(1, presentation.width)
                self.crop_height_spinbox.setRange(1, presentation.height)
                self.crop_x_spinbox.setValue(crop.x)
                self.crop_y_spinbox.setValue(crop.y)
                self.crop_width_spinbox.setValue(crop.width)
                self.crop_height_spinbox.setValue(crop.height)
        finally:
            del blockers
            self._crop_syncing = False
        available = presentation is not None and editable
        self.crop_toggle.setEnabled(available)
        self.crop_reset_button.setEnabled(available)
        for field in self.crop_fields.values():
            field.setEnabled(available)

    def _crop_spinbox(self, name: str) -> QSpinBox:
        field = QSpinBox()
        field.setObjectName(f"crop_{name}")
        field.setAccessibleName(f"Crop {name}")
        field.setMinimum(0 if name in {"x", "y"} else 1)
        field.setMaximum(1)
        return field

    def _crop_toggle_changed(self, enabled: bool) -> None:
        if not self._crop_syncing:
            self.command_requested.emit(CropToggleChanged(enabled))

    def _crop_fields_changed(self, _value: int) -> None:
        if self._crop_syncing:
            return
        self.command_requested.emit(
            CropChanged(
                CropSpec(
                    self.crop_x_spinbox.value(),
                    self.crop_y_spinbox.value(),
                    self.crop_width_spinbox.value(),
                    self.crop_height_spinbox.value(),
                )
            )
        )

    def _section(self, key: str, title: str, default: bool) -> QFrame:
        section = QFrame()
        section.setObjectName(f"{key}_section")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        button = QToolButton()
        button.setText(title)
        button.setCheckable(True)
        button.setObjectName(f"{key}_disclosure")
        button.setAccessibleName(title)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 8, 8)
        copy = QLabel("Available when a video is ready")
        copy.setProperty("secondary", True)
        body_layout.addWidget(copy)
        if key == "segmentation":
            body_layout.addWidget(self.manage_models)
        if key == "crop_cleanup":
            body_layout.addWidget(self._crop_controls())
        if key == "workspace":
            body_layout.addWidget(self.edited_cut_recovery)
            body_layout.addWidget(self.rebuild_button)
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

    def _crop_controls(self) -> QWidget:
        controls = QWidget()
        layout = QFormLayout(controls)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addRow(self.crop_toggle, self.crop_reset_button)
        layout.addRow("X", self.crop_x_spinbox)
        layout.addRow("Y", self.crop_y_spinbox)
        layout.addRow("Width", self.crop_width_spinbox)
        layout.addRow("Height", self.crop_height_spinbox)
        return controls

    def crop_tab_widgets(self) -> tuple[QWidget, ...]:
        """Return the crop controls in their keyboard navigation order."""
        return (
            self.crop_toggle,
            self.crop_reset_button,
            self.crop_x_spinbox,
            self.crop_y_spinbox,
            self.crop_width_spinbox,
            self.crop_height_spinbox,
        )

    def set_workspace_state(self, attention: bool, open_: bool) -> None:
        """Apply presenter-owned attention and disclosure state."""
        workspace_button, workspace_body = self.disclosures["workspace"]
        workspace_button.setProperty("attention", attention)
        if open_ and not workspace_button.isChecked():
            workspace_button.setChecked(True)
        workspace_body.setVisible(workspace_button.isChecked())
        workspace_button.style().unpolish(workspace_button)
        workspace_button.style().polish(workspace_button)

    def show_workspace_attention(self, visible: bool) -> None:
        """Compatibility wrapper for older callers."""
        self.set_workspace_state(visible, visible)

    def _read_bool(self, name: str, default: bool) -> bool:
        value = self._settings.value(name, default)
        return value if type(value) is bool else default
