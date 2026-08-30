"""Continuous, truthful inspector shell without editor-owned domain state."""

from __future__ import annotations

from PySide6.QtCore import QSettings, QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from rembggui.core.crop_state import CropChanged, CropToggleChanged, ResetCrop
from rembggui.core.parameters import (
    V1_MODEL_IDS,
    AlphaThresholdChanged,
    EdgeModeChanged,
    GlobalTrimChanged,
    ModelChanged,
    OutputFilenameChanged,
    OutputFpsChanged,
    OutputMaxSizeChanged,
    PaddingChanged,
    StretchChanged,
    is_valid_output_filename,
)
from rembggui.core.specs import CropSpec, EdgeMode
from rembggui.core.timeline import DurationChanged, EndChanged, StartChanged
from rembggui.jobs.models.catalog import ModelCatalog
from rembggui.ui.crop_presentation import CropPresentation
from rembggui.ui.parameter_presentation import (
    ParameterPresentation,
    decimal_from_widget_value,
    fraction_from_widget_value,
)
from rembggui.ui.ports import OutputDirectoryRequested
from rembggui.ui.source_presentation import format_source_file_size

_DISCLOSURES = (
    ("segmentation", "Segmentation", True),
    ("time_sampling", "Time & Sampling", True),
    ("crop_cleanup", "Crop & Cleanup", False),
    ("output", "Output", True),
    ("workspace", "Workspace", False),
)


class Inspector(QFrame):
    command_requested = Signal(object)

    def __init__(
        self,
        settings: QSettings,
        model_options: tuple[tuple[str, bool], ...] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("inspector")
        self.setAccessibleName("Processing settings")
        self._settings = settings
        self._model_options = dict(model_options or ())
        self._build_parameter_controls()
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

    def _build_parameter_controls(self) -> None:
        self._parameter_syncing = False
        self._build_segmentation_parameter_controls()
        self._build_sampling_parameter_controls()
        self._build_cleanup_parameter_controls()
        self._build_output_parameter_controls()
        self._connect_parameter_controls()

    def _build_segmentation_parameter_controls(self) -> None:
        self.model_picker = QComboBox()
        self.model_picker.setObjectName("model_picker")
        self.model_picker.setAccessibleName("Segmentation model")
        catalog = ModelCatalog.load_resource()
        for model_id in V1_MODEL_IDS:
            spec = catalog.get(model_id)
            artifact = spec.artifact
            size = format_source_file_size(
                artifact.size_bytes if artifact is not None else None
            )
            availability = self._model_options.get(model_id, False)
            status = "cached locally" if availability else "not cached locally"
            self.model_picker.addItem(
                f"{spec.display_name} — {size} — {status}", model_id
            )
            self.model_picker.setItemData(
                self.model_picker.count() - 1,
                f"{spec.display_name} ({size}; {status})",
                Qt.ItemDataRole.ToolTipRole,
            )
        self.model_picker.setCurrentIndex(
            self.model_picker.findData(catalog.default_id)
        )
        self.edge_picker = QComboBox()
        self.edge_picker.setObjectName("edge_picker")
        self.edge_picker.setAccessibleName("Edge treatment")
        self.edge_picker.addItem("Standard", EdgeMode.STANDARD)
        self.edge_picker.addItem("Decontaminate colors", EdgeMode.DECONTAMINATE_COLORS)

    def _build_sampling_parameter_controls(self) -> None:
        self.fps_spinbox = QSpinBox()
        self.fps_spinbox.setObjectName("output_fps")
        self.fps_spinbox.setAccessibleName("Output FPS")
        self.fps_spinbox.setRange(1, 240)
        self.fps_spinbox.setSuffix(" fps")
        self.fps_warning = QLabel("High output FPS may increase render cost")
        self.fps_warning.setObjectName("fps_warning")
        self.fps_warning.setProperty("warning", True)
        self.fps_warning.setAccessibleName("Output FPS cost warning")
        self.fps_warning.hide()
        self.start_spinbox = self._time_spinbox("start")
        self.end_spinbox = self._time_spinbox("end")
        self.duration_spinbox = self._time_spinbox("duration")

    def _build_cleanup_parameter_controls(self) -> None:
        self.trim_checkbox = QCheckBox("Global trim")
        self.trim_checkbox.setObjectName("global_trim")
        self.trim_checkbox.setAccessibleName("Global alpha trim")
        self.alpha_threshold_spinbox = self._decimal_spinbox(
            "alpha_threshold", 0.0, 100.0, 1
        )
        self.alpha_threshold_spinbox.setSuffix(" %")
        self.padding_spinbox = QSpinBox()
        self.padding_spinbox.setObjectName("padding")
        self.padding_spinbox.setAccessibleName("Padding pixels")
        self.padding_spinbox.setRange(0, 2_147_483_647)
        self.stretch_spinbox = self._decimal_spinbox(
            "horizontal_stretch", 0.000001, 1_000_000_000.0, 6
        )
        self.stretch_spinbox.setAccessibleName("Horizontal stretch")

    def _build_output_parameter_controls(self) -> None:
        self.output_directory_edit = QLineEdit()
        self.output_directory_edit.setObjectName("output_directory")
        self.output_directory_edit.setAccessibleName("Output directory")
        self.output_directory_edit.setReadOnly(True)
        self.output_directory_button = QPushButton("Choose…")
        self.output_directory_button.setObjectName("choose_output_directory")
        self.output_directory_button.setAccessibleName("Choose output directory")
        self.output_filename_edit = QLineEdit()
        self.output_filename_edit.setObjectName("output_filename")
        self.output_filename_edit.setAccessibleName("Output filename")
        self.output_filename_edit.setPlaceholderText("filename.webp")
        self.output_filename_edit.setToolTip(
            "Use one non-empty filename ending in .webp; "
            "path separators are not allowed."
        )
        self.max_size_spinbox = self._decimal_spinbox(
            "maximum_size", 0.0, 1_000_000_000.0, 3
        )
        self.max_size_spinbox.setAccessibleName("Maximum file size in MiB")
        self.max_size_spinbox.setSuffix(" MiB")

    def _connect_parameter_controls(self) -> None:
        self.model_picker.currentIndexChanged.connect(self._model_changed)
        self.edge_picker.currentIndexChanged.connect(self._edge_changed)
        self.fps_spinbox.valueChanged.connect(
            lambda value: self._emit_if_editable(OutputFpsChanged(value))
        )
        self.start_spinbox.valueChanged.connect(self._start_changed)
        self.end_spinbox.valueChanged.connect(self._end_changed)
        self.duration_spinbox.valueChanged.connect(self._duration_changed)
        self.trim_checkbox.toggled.connect(
            lambda value: self._emit_if_editable(GlobalTrimChanged(value))
        )
        self.alpha_threshold_spinbox.valueChanged.connect(
            lambda value: self._emit_if_editable(
                AlphaThresholdChanged(decimal_from_widget_value(value))
            )
        )
        self.padding_spinbox.valueChanged.connect(
            lambda value: self._emit_if_editable(PaddingChanged(value))
        )
        self.stretch_spinbox.valueChanged.connect(
            lambda value: self._emit_if_editable(
                StretchChanged(decimal_from_widget_value(value))
            )
        )
        self.output_directory_button.clicked.connect(
            lambda: self.command_requested.emit(OutputDirectoryRequested())
        )
        self.output_filename_edit.editingFinished.connect(self._filename_changed)
        self.max_size_spinbox.valueChanged.connect(
            lambda value: self._emit_if_editable(
                OutputMaxSizeChanged(decimal_from_widget_value(value))
            )
        )

    def _emit_if_editable(self, command: object) -> None:
        if not self._parameter_syncing:
            self.command_requested.emit(command)

    def _model_changed(self, _index: int) -> None:
        model_id = self.model_picker.currentData()
        if isinstance(model_id, str):
            self._emit_if_editable(ModelChanged(model_id))

    def _edge_changed(self, _index: int) -> None:
        try:
            edge_mode = EdgeMode(self.edge_picker.currentData())
        except (TypeError, ValueError):
            return
        if edge_mode in {EdgeMode.STANDARD, EdgeMode.DECONTAMINATE_COLORS}:
            self._emit_if_editable(EdgeModeChanged(edge_mode))

    def _start_changed(self, value: float) -> None:
        self._emit_if_editable(StartChanged(fraction_from_widget_value(value)))

    def _end_changed(self, value: float) -> None:
        self._emit_if_editable(EndChanged(fraction_from_widget_value(value)))

    def _duration_changed(self, value: float) -> None:
        self._emit_if_editable(DurationChanged(fraction_from_widget_value(value)))

    def _filename_changed(self) -> None:
        if self._parameter_syncing:
            return
        filename = self.output_filename_edit.text()
        if not is_valid_output_filename(filename):
            self._show_filename_error()
            return
        self._clear_filename_error()
        self.command_requested.emit(OutputFilenameChanged(filename))

    def _show_filename_error(self) -> None:
        message = "Filename must be a single non-empty .webp filename."
        self.output_filename_edit.setProperty("invalid", True)
        self.output_filename_edit.setToolTip(message)
        self.output_filename_edit.setAccessibleDescription(message)
        self.output_filename_edit.style().unpolish(self.output_filename_edit)
        self.output_filename_edit.style().polish(self.output_filename_edit)

    def _clear_filename_error(self) -> None:
        self.output_filename_edit.setProperty("invalid", False)
        self.output_filename_edit.setToolTip(
            "Use one non-empty filename ending in .webp; "
            "path separators are not allowed."
        )
        self.output_filename_edit.setAccessibleDescription("")
        self.output_filename_edit.style().unpolish(self.output_filename_edit)
        self.output_filename_edit.style().polish(self.output_filename_edit)

    def apply_parameters(
        self, presentation: ParameterPresentation, editable: bool
    ) -> None:
        """Render reducer-owned parameters into standard inspector widgets."""
        self._parameter_syncing = True
        widgets = (
            self.model_picker,
            self.edge_picker,
            self.fps_spinbox,
            self.start_spinbox,
            self.end_spinbox,
            self.duration_spinbox,
            self.trim_checkbox,
            self.alpha_threshold_spinbox,
            self.padding_spinbox,
            self.stretch_spinbox,
            self.output_filename_edit,
            self.max_size_spinbox,
        )
        blockers = [QSignalBlocker(widget) for widget in widgets]
        try:
            model_index = self.model_picker.findData(presentation.model_id)
            if model_index >= 0:
                self.model_picker.setCurrentIndex(model_index)
            edge_index = self.edge_picker.findData(presentation.edge_mode.value)
            if edge_index >= 0:
                self.edge_picker.setCurrentIndex(edge_index)
            self.fps_spinbox.setValue(presentation.fps)
            self.trim_checkbox.setChecked(presentation.trim)
            self.alpha_threshold_spinbox.setValue(float(presentation.alpha_threshold))
            self.padding_spinbox.setValue(presentation.padding)
            self.stretch_spinbox.setValue(float(presentation.stretch_x))
            self.output_filename_edit.setText(presentation.output_filename)
            self.max_size_spinbox.setValue(float(presentation.max_mib))
            self._apply_time_values(presentation)
            self.output_directory_edit.setText(
                str(presentation.output_directory or "")
            )
            self._clear_filename_error()
        finally:
            del blockers
            self._parameter_syncing = False
        available = editable and presentation.duration is not None
        for widget in (
            self.model_picker,
            self.edge_picker,
            self.fps_spinbox,
            self.start_spinbox,
            self.end_spinbox,
            self.duration_spinbox,
            self.trim_checkbox,
            self.alpha_threshold_spinbox,
            self.padding_spinbox,
            self.stretch_spinbox,
            self.output_directory_button,
            self.output_filename_edit,
            self.max_size_spinbox,
        ):
            widget.setEnabled(available)
        self.fps_warning.setVisible(available and presentation.fps > 60)

    def _apply_time_values(self, presentation: ParameterPresentation) -> None:
        duration = presentation.source_duration
        if duration is None or presentation.start is None or presentation.end is None:
            for widget in (self.start_spinbox, self.end_spinbox, self.duration_spinbox):
                widget.setRange(0.0, 1.0)
                widget.setValue(0.0)
            return
        maximum = float(duration)
        interval = 1.0 / presentation.fps
        self.start_spinbox.setRange(0.0, max(0.0, float(presentation.end) - interval))
        self.end_spinbox.setRange(
            min(maximum, float(presentation.start) + interval), maximum
        )
        self.duration_spinbox.setRange(
            interval, max(interval, maximum - float(presentation.start))
        )
        self.start_spinbox.setValue(float(presentation.start))
        self.end_spinbox.setValue(float(presentation.end))
        self.duration_spinbox.setValue(float(presentation.duration or 0))

    def _time_spinbox(self, name: str) -> QDoubleSpinBox:
        field = QDoubleSpinBox()
        field.setObjectName(f"{name}_time")
        field.setAccessibleName(name.capitalize())
        field.setDecimals(3)
        field.setRange(0.0, 1.0)
        field.setSingleStep(0.001)
        field.setSuffix(" s")
        return field

    def _decimal_spinbox(
        self, name: str, minimum: float, maximum: float, decimals: int
    ) -> QDoubleSpinBox:
        field = QDoubleSpinBox()
        field.setObjectName(name)
        field.setDecimals(decimals)
        field.setRange(minimum, maximum)
        field.setSingleStep(1.0 if decimals == 0 else 0.1)
        return field

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
            body_layout.addWidget(self._segmentation_controls())
            body_layout.addWidget(self.manage_models)
        if key == "time_sampling":
            body_layout.addWidget(self._time_controls())
        if key == "crop_cleanup":
            body_layout.addWidget(self._cleanup_controls())
            body_layout.addWidget(self._crop_controls())
        if key == "output":
            body_layout.addWidget(self._output_controls())
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

    def _segmentation_controls(self) -> QWidget:
        controls = QWidget()
        layout = QFormLayout(controls)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addRow("Model", self.model_picker)
        layout.addRow("Edge treatment", self.edge_picker)
        return controls

    def _time_controls(self) -> QWidget:
        controls = QWidget()
        layout = QFormLayout(controls)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addRow("Output FPS", self.fps_spinbox)
        layout.addRow(self.fps_warning)
        layout.addRow("Start", self.start_spinbox)
        layout.addRow("End", self.end_spinbox)
        layout.addRow("Duration", self.duration_spinbox)
        return controls

    def _cleanup_controls(self) -> QWidget:
        controls = QWidget()
        layout = QFormLayout(controls)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addRow(self.trim_checkbox)
        layout.addRow("Alpha threshold", self.alpha_threshold_spinbox)
        layout.addRow("Padding", self.padding_spinbox)
        layout.addRow("Horizontal stretch", self.stretch_spinbox)
        return controls

    def _output_controls(self) -> QWidget:
        controls = QWidget()
        layout = QFormLayout(controls)
        layout.setContentsMargins(0, 0, 0, 0)
        directory = QWidget()
        directory_layout = QVBoxLayout(directory)
        directory_layout.setContentsMargins(0, 0, 0, 0)
        directory_layout.addWidget(self.output_directory_edit)
        directory_layout.addWidget(self.output_directory_button)
        layout.addRow("Directory", directory)
        layout.addRow("Filename", self.output_filename_edit)
        layout.addRow("Maximum size", self.max_size_spinbox)
        return controls

    def parameter_tab_widgets(self) -> tuple[QWidget, ...]:
        """Return standard parameter controls in consequence order."""
        return (
            self.model_picker,
            self.edge_picker,
            self.fps_spinbox,
            self.start_spinbox,
            self.end_spinbox,
            self.duration_spinbox,
            self.trim_checkbox,
            self.alpha_threshold_spinbox,
            self.padding_spinbox,
            self.stretch_spinbox,
            self.output_directory_button,
            self.output_filename_edit,
            self.max_size_spinbox,
        )

    def tab_widgets(self) -> tuple[QWidget, ...]:
        """Return the full inspector tab route in consequence order."""
        return (
            self.disclosures["segmentation"][0],
            self.model_picker,
            self.edge_picker,
            self.manage_models,
            self.disclosures["time_sampling"][0],
            self.fps_spinbox,
            self.start_spinbox,
            self.end_spinbox,
            self.duration_spinbox,
            self.disclosures["crop_cleanup"][0],
            *self.crop_tab_widgets(),
            self.trim_checkbox,
            self.alpha_threshold_spinbox,
            self.padding_spinbox,
            self.stretch_spinbox,
            self.disclosures["output"][0],
            self.output_directory_button,
            self.output_filename_edit,
            self.max_size_spinbox,
            self.disclosures["workspace"][0],
            self.edited_cut_recovery,
            self.rebuild_button,
            self.manage_workspaces,
        )

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
