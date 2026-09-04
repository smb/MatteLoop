"""Trim, crop, and resize controls for a finished cut's post-render transform."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from fractions import Fraction
from itertools import groupby

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from matteloop.core.crop import centered_crop_for_aspect
from matteloop.core.errors import ValidationError
from matteloop.core.parameters import TransformChanged
from matteloop.core.specs import (
    MAX_FINAL_DIMENSION,
    MIN_FINAL_DIMENSION,
    PLATFORM_SIZE_PRESETS,
    CropSpec,
    MismatchMode,
    ResizeSpec,
    SizePreset,
    TransformSpec,
)
from matteloop.core.state import ArtifactResult
from matteloop.ui.compact_widgets import compact_field
from matteloop.ui.parameter_presentation import ParameterPresentation
from matteloop.ui.source_presentation import (
    format_source_dimensions,
    format_source_file_size,
)

_ASPECT_PRESETS: tuple[tuple[str, Fraction | None], ...] = (
    ("Free", None),
    ("1:1", Fraction(1, 1)),
    ("2:1", Fraction(2, 1)),
    ("3:1", Fraction(3, 1)),
    ("4:3", Fraction(4, 3)),
    ("16:9", Fraction(16, 9)),
)

_MISMATCH_LABELS: tuple[tuple[str, MismatchMode], ...] = (
    ("Keep original aspect ratio", MismatchMode.KEEP),
    ("Stretch to fit", MismatchMode.STRETCH),
    ("Center and crop to fit", MismatchMode.COVER),
    ("Add transparent padding", MismatchMode.PAD),
)

_RESIZE_ERROR = "Set at least one of width or height."


@dataclass(frozen=True)
class CutFacts:
    """Cut-derived facts the group needs but ParameterState does not hold."""

    cache_key: str
    frame_count: int
    framed_size: tuple[int, int]
    fps: int
    delays_ms: tuple[int, ...]


class TransformGroup(QWidget):
    """A self-contained trim/crop/resize editor for the current cut's transform."""

    aspect_lock_changed = Signal(object)  # Fraction | None
    crop_edit_toggled = Signal(bool)
    use_playhead_requested = Signal(str)  # "first" | "last"

    def __init__(
        self,
        emit: Callable[[object], None],
        parent: QWidget | None = None,
        *,
        presets: tuple[SizePreset, ...] = PLATFORM_SIZE_PRESETS,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("transform_group")
        self.setAccessibleName("Transform")
        self._emit = emit
        self._presets = presets
        self._transform = TransformSpec()
        self._facts: CutFacts | None = None
        self._artifact: ArtifactResult | None = None
        self._playhead: int | None = None
        self._editable = False
        self._syncing = False
        self._mismatch = MismatchMode.KEEP
        self._build_trim_widgets()
        self._build_crop_widgets()
        self._build_resize_widgets()
        self._build_readout_widget()
        self._connect_signals()
        self._build_layout()
        self._syncing = True
        try:
            self._configure_ranges()
        finally:
            self._syncing = False
        self._refresh_enabled()

    # -- public surface ----------------------------------------------------

    def apply(self, presentation: ParameterPresentation, editable: bool) -> None:
        """Render reducer-owned transform and artifact values into the widgets."""
        self._syncing = True
        try:
            self._transform = presentation.transform
            self._artifact = presentation.artifact
            self._apply_transform_to_widgets()
        finally:
            self._syncing = False
        self._editable = editable
        self._refresh_enabled()
        self._update_readout()

    def set_cut(self, facts: CutFacts | None) -> None:
        """Update the cut this group edits, re-displaying the current transform."""
        self._facts = facts
        self._syncing = True
        try:
            self._configure_ranges()
            self._sync_trim_widgets()
            self._sync_crop_widgets()
        finally:
            self._syncing = False
        self._refresh_enabled()
        self._update_readout()

    def set_playhead_frame(self, index: int | None) -> None:
        """Enable the "Use playhead" buttons once a stored-cut index is known."""
        self._playhead = index
        self._refresh_enabled()

    def tab_widgets(self) -> tuple[QWidget, ...]:
        """Return the group's controls in keyboard navigation order."""
        return (
            self.first_frame_spinbox,
            self.first_playhead_button,
            self.last_frame_spinbox,
            self.last_playhead_button,
            self.crop_edit_checkbox,
            self.aspect_combo,
            *self._crop_field_widgets(),
            self.width_spinbox,
            self.height_spinbox,
            self.percent_spinbox,
            self.mismatch_combo,
            self.size_preset_combo,
        )

    # -- widget construction -------------------------------------------------

    def _build_trim_widgets(self) -> None:
        self.first_frame_spinbox = compact_field(QSpinBox())
        self.first_frame_spinbox.setObjectName("transform_first_frame")
        self.first_frame_spinbox.setAccessibleName("First frame")
        self.first_playhead_button = QPushButton("Use Playhead")
        self.first_playhead_button.setObjectName("transform_first_playhead")
        self.first_playhead_button.setAccessibleName("Use playhead for first frame")
        self.last_frame_spinbox = compact_field(QSpinBox())
        self.last_frame_spinbox.setObjectName("transform_last_frame")
        self.last_frame_spinbox.setAccessibleName("Last frame")
        self.last_playhead_button = QPushButton("Use Playhead")
        self.last_playhead_button.setObjectName("transform_last_playhead")
        self.last_playhead_button.setAccessibleName("Use playhead for last frame")

    def _build_crop_widgets(self) -> None:
        self.crop_edit_checkbox = QCheckBox("Edit crop")
        self.crop_edit_checkbox.setObjectName("transform_crop_edit")
        self.crop_edit_checkbox.setAccessibleName("Edit crop")
        self.aspect_combo = compact_field(QComboBox())
        self.aspect_combo.setObjectName("transform_aspect")
        self.aspect_combo.setAccessibleName("Crop aspect ratio")
        for label, ratio in _ASPECT_PRESETS:
            self.aspect_combo.addItem(label, ratio)
        self.crop_x_spinbox = self._crop_spinbox("x")
        self.crop_y_spinbox = self._crop_spinbox("y")
        self.crop_width_spinbox = self._crop_spinbox("width")
        self.crop_height_spinbox = self._crop_spinbox("height")

    def _crop_spinbox(self, name: str) -> QSpinBox:
        field = compact_field(QSpinBox())
        field.setObjectName(f"transform_crop_{name}")
        field.setAccessibleName(f"Crop {name}")
        field.setMaximum(MAX_FINAL_DIMENSION)
        return field

    def _build_resize_widgets(self) -> None:
        self.width_spinbox = self._dimension_spinbox("width")
        self.height_spinbox = self._dimension_spinbox("height")
        self.percent_spinbox = compact_field(QDoubleSpinBox())
        self.percent_spinbox.setObjectName("transform_percent")
        self.percent_spinbox.setAccessibleName("Resize percentage")
        self.percent_spinbox.setRange(0, 1000)
        self.percent_spinbox.setSuffix(" %")
        self.mismatch_combo = compact_field(QComboBox())
        self.mismatch_combo.setObjectName("transform_mismatch")
        self.mismatch_combo.setAccessibleName("Aspect mismatch handling")
        for label, mode in _MISMATCH_LABELS:
            self.mismatch_combo.addItem(label, mode)
        self.size_preset_combo = compact_field(QComboBox())
        self.size_preset_combo.setObjectName("transform_size_preset")
        self.size_preset_combo.setAccessibleName("Output size preset")
        self.size_preset_combo.setModel(self._preset_model())

    def _dimension_spinbox(self, name: str) -> QSpinBox:
        field = compact_field(QSpinBox())
        field.setObjectName(f"transform_{name}")
        field.setAccessibleName(name.capitalize())
        field.setRange(MIN_FINAL_DIMENSION - 1, MAX_FINAL_DIMENSION)
        field.setSpecialValueText("Auto")
        return field

    def _preset_model(self) -> QStandardItemModel:
        model = QStandardItemModel(self.size_preset_combo)
        custom = QStandardItem("Custom")
        custom.setData(None, Qt.ItemDataRole.UserRole)
        model.appendRow(custom)
        grouped = groupby(self._presets, key=lambda preset: preset.platform)
        for platform, group in grouped:
            header = QStandardItem(platform)
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            model.appendRow(header)
            for preset in group:
                item = QStandardItem(preset.label)
                item.setData(preset, Qt.ItemDataRole.UserRole)
                model.appendRow(item)
        return model

    def _build_readout_widget(self) -> None:
        self.readout_label = QLabel()
        self.readout_label.setObjectName("transform_readout")
        self.readout_label.setAccessibleName("Transform summary")
        self.readout_label.setWordWrap(True)

    def _connect_signals(self) -> None:
        self.first_frame_spinbox.valueChanged.connect(self._first_frame_changed)
        self.last_frame_spinbox.valueChanged.connect(self._last_frame_changed)
        self.first_playhead_button.clicked.connect(
            lambda: self.use_playhead_requested.emit("first")
        )
        self.last_playhead_button.clicked.connect(
            lambda: self.use_playhead_requested.emit("last")
        )
        self.crop_edit_checkbox.toggled.connect(self._crop_edit_changed)
        self.aspect_combo.currentIndexChanged.connect(self._aspect_changed)
        for field in self._crop_field_widgets():
            field.valueChanged.connect(self._crop_field_changed)
        self.width_spinbox.valueChanged.connect(self._resize_field_changed)
        self.height_spinbox.valueChanged.connect(self._resize_field_changed)
        self.percent_spinbox.valueChanged.connect(self._percent_changed)
        self.mismatch_combo.currentIndexChanged.connect(self._mismatch_changed)
        self.size_preset_combo.currentIndexChanged.connect(self._size_preset_changed)

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self._trim_section())
        layout.addWidget(self._crop_section())
        layout.addWidget(self._resize_section())
        layout.addWidget(self.readout_label)

    def _trim_section(self) -> QWidget:
        section = QWidget()
        layout = QFormLayout(section)
        layout.addRow("First frame", self._trim_row(
            self.first_frame_spinbox, self.first_playhead_button
        ))
        layout.addRow("Last frame", self._trim_row(
            self.last_frame_spinbox, self.last_playhead_button
        ))
        return section

    def _trim_row(self, spinbox: QSpinBox, button: QPushButton) -> QWidget:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(spinbox, 1)
        row_layout.addWidget(button)
        return row

    def _crop_section(self) -> QWidget:
        section = QWidget()
        layout = QFormLayout(section)
        layout.addRow(self.crop_edit_checkbox)
        layout.addRow("Aspect", self.aspect_combo)
        layout.addRow("X", self.crop_x_spinbox)
        layout.addRow("Y", self.crop_y_spinbox)
        layout.addRow("Width", self.crop_width_spinbox)
        layout.addRow("Height", self.crop_height_spinbox)
        return section

    def _resize_section(self) -> QWidget:
        section = QWidget()
        layout = QFormLayout(section)
        layout.addRow("Width", self.width_spinbox)
        layout.addRow("Height", self.height_spinbox)
        layout.addRow("Percent", self.percent_spinbox)
        layout.addRow("Mismatch", self.mismatch_combo)
        layout.addRow("Preset", self.size_preset_combo)
        return section

    # -- reducer-owned state -> widgets --------------------------------------

    def _apply_transform_to_widgets(self) -> None:
        # The aspect lock is a UI-only choice, not derived from TransformSpec,
        # so a reducer round trip must never touch aspect_combo's selection.
        self._sync_trim_widgets()
        self._sync_crop_widgets()
        self._sync_resize_widgets()

    def _sync_trim_widgets(self) -> None:
        self.first_frame_spinbox.setValue(self._transform.first_frame)
        last_default = self._facts.frame_count - 1 if self._facts is not None else 0
        last_frame = self._transform.last_frame
        self.last_frame_spinbox.setValue(
            last_default if last_frame is None else last_frame
        )

    def _sync_crop_widgets(self) -> None:
        self._set_crop_widgets(self._transform.crop or self._default_crop())

    def _default_crop(self) -> CropSpec:
        if self._facts is None:
            return CropSpec(0, 0, 1, 1)
        return CropSpec(0, 0, *self._facts.framed_size)

    def _set_crop_widgets(self, crop: CropSpec) -> None:
        blockers = [QSignalBlocker(field) for field in self._crop_field_widgets()]
        try:
            self.crop_x_spinbox.setValue(crop.x)
            self.crop_y_spinbox.setValue(crop.y)
            self.crop_width_spinbox.setValue(crop.width)
            self.crop_height_spinbox.setValue(crop.height)
        finally:
            del blockers

    def _sync_resize_widgets(self) -> None:
        resize = self._transform.resize
        self._mismatch = resize.mismatch if resize is not None else MismatchMode.KEEP
        self.width_spinbox.setValue(self._dimension_display(resize, "width"))
        self.height_spinbox.setValue(self._dimension_display(resize, "height"))
        self.mismatch_combo.setCurrentIndex(self.mismatch_combo.findData(self._mismatch))
        self.percent_spinbox.setValue(0)
        self.size_preset_combo.setCurrentIndex(0)

    def _dimension_display(self, resize: ResizeSpec | None, axis: str) -> int:
        spinbox = self.width_spinbox if axis == "width" else self.height_spinbox
        value = None if resize is None else getattr(resize, axis)
        return spinbox.minimum() if value is None else value

    def _configure_ranges(self) -> None:
        if self._facts is None:
            self.first_frame_spinbox.setRange(0, 0)
            self.last_frame_spinbox.setRange(0, 0)
            self.crop_x_spinbox.setRange(0, 0)
            self.crop_y_spinbox.setRange(0, 0)
            self.crop_width_spinbox.setRange(1, 1)
            self.crop_height_spinbox.setRange(1, 1)
            return
        frame_count = self._facts.frame_count
        width, height = self._facts.framed_size
        self.first_frame_spinbox.setRange(0, max(0, frame_count - 1))
        self.last_frame_spinbox.setRange(0, max(0, frame_count - 1))
        self.crop_x_spinbox.setRange(0, max(0, width - 1))
        self.crop_y_spinbox.setRange(0, max(0, height - 1))
        self.crop_width_spinbox.setRange(1, width)
        self.crop_height_spinbox.setRange(1, height)

    # -- widget edits -> reducer events --------------------------------------

    def _first_frame_changed(self, value: int) -> None:
        if self._syncing:
            return
        last_frame = self._transform.last_frame
        if last_frame is not None and value > last_frame:
            last_frame = value
            self._set_spinbox_silently(self.last_frame_spinbox, value)
        self._update_transform(
            replace(self._transform, first_frame=value, last_frame=last_frame)
        )

    def _last_frame_changed(self, _value: int) -> None:
        if self._syncing:
            return
        last_frame = self._last_frame_value()
        first_frame = self._transform.first_frame
        if last_frame is not None and last_frame < first_frame:
            first_frame = last_frame
            self._set_spinbox_silently(self.first_frame_spinbox, first_frame)
        self._update_transform(
            replace(self._transform, first_frame=first_frame, last_frame=last_frame)
        )

    def _set_spinbox_silently(self, spinbox: QSpinBox, value: int) -> None:
        """Move *spinbox* to keep first/last coupled without re-entering a slot."""
        was_syncing = self._syncing
        self._syncing = True
        try:
            spinbox.setValue(value)
        finally:
            self._syncing = was_syncing

    def _last_frame_value(self) -> int | None:
        value = self.last_frame_spinbox.value()
        if self._facts is not None and value == self._facts.frame_count - 1:
            return None
        return value

    def _crop_edit_changed(self, checked: bool) -> None:
        if not self._syncing:
            self.crop_edit_toggled.emit(checked)

    def _aspect_changed(self, _index: int) -> None:
        if self._syncing:
            return
        ratio = self.aspect_combo.currentData()
        if ratio is None:
            self.aspect_lock_changed.emit(None)
            return
        if self._facts is None or not isinstance(ratio, Fraction):
            return
        width, height = self._facts.framed_size
        crop = centered_crop_for_aspect(ratio, source_width=width, source_height=height)
        self._set_crop_widgets(crop)
        self._update_transform(replace(self._transform, crop=crop))
        self.aspect_lock_changed.emit(ratio)

    def _crop_field_changed(self, _value: int) -> None:
        if self._syncing:
            return
        crop = CropSpec(
            self.crop_x_spinbox.value(),
            self.crop_y_spinbox.value(),
            self.crop_width_spinbox.value(),
            self.crop_height_spinbox.value(),
        )
        self._update_transform(replace(self._transform, crop=crop))

    def _resize_field_changed(self, _value: int) -> None:
        if self._syncing:
            return
        with QSignalBlocker(self.percent_spinbox):
            self.percent_spinbox.setValue(0)
        self._apply_resize_fields()

    def _percent_changed(self, value: float) -> None:
        if self._syncing or self._facts is None or value <= 0:
            return
        cropped_width, cropped_height = self._cropped_size()
        fraction = Fraction(value) / 100
        width = _rhu(Fraction(cropped_width) * fraction)
        height = _rhu(Fraction(cropped_height) * fraction)
        with QSignalBlocker(self.width_spinbox), QSignalBlocker(self.height_spinbox):
            self.width_spinbox.setValue(width)
            self.height_spinbox.setValue(height)
        self._apply_resize_fields()

    def _cropped_size(self) -> tuple[int, int]:
        if self._transform.crop is not None:
            return (self._transform.crop.width, self._transform.crop.height)
        if self._facts is not None:
            return self._facts.framed_size
        return (1, 1)

    def _mismatch_changed(self, _index: int) -> None:
        if self._syncing:
            return
        try:
            # PySide's QVariant boxing degrades a StrEnum to a plain str on
            # round trip (cf. inspector.py's EdgeMode combo handling), so the
            # value is re-parsed through the enum rather than isinstance-checked.
            mode = MismatchMode(self.mismatch_combo.currentData())
        except (TypeError, ValueError):
            return
        self._mismatch = mode
        if self._transform.resize is not None:
            resize = replace(self._transform.resize, mismatch=mode)
            self._update_transform(replace(self._transform, resize=resize))

    def _size_preset_changed(self, _index: int) -> None:
        if self._syncing:
            return
        preset = self.size_preset_combo.currentData()
        if not isinstance(preset, SizePreset):
            return
        with QSignalBlocker(self.width_spinbox), QSignalBlocker(self.height_spinbox):
            self.width_spinbox.setValue(preset.width)
            self.height_spinbox.setValue(preset.height)
        self._apply_resize_fields()

    def _apply_resize_fields(self) -> None:
        width = self._width_value()
        height = self._height_value()
        if width is None and height is None:
            self._show_resize_error()
            return
        self._clear_resize_error()
        resize = ResizeSpec(width, height, self._mismatch)
        self._update_transform(replace(self._transform, resize=resize))

    def _width_value(self) -> int | None:
        value = self.width_spinbox.value()
        return None if value == self.width_spinbox.minimum() else value

    def _height_value(self) -> int | None:
        value = self.height_spinbox.value()
        return None if value == self.height_spinbox.minimum() else value

    def _update_transform(self, transform: TransformSpec) -> None:
        if transform == self._transform:
            return
        self._transform = transform
        self._emit(TransformChanged(transform))
        self._update_readout()

    # -- readout and error surfaces -------------------------------------

    def _update_readout(self) -> None:
        if self._facts is None:
            self.readout_label.setText("")
            self._clear_transform_error()
            return
        try:
            size = self._transform.validate_for(
                self._facts.frame_count, self._facts.framed_size
            )
        except ValidationError as error:
            self._show_transform_error(str(error))
            return
        self._clear_transform_error()
        self.readout_label.setText(self._readout_text(size))

    def _readout_text(self, size: tuple[int, int]) -> str:
        facts = self._facts
        assert facts is not None
        kept = self._transform.kept_range(facts.frame_count)
        delays = self._transform.select_kept(facts.delays_ms)
        duration = sum(delays) / 1000
        text = (
            f"{len(kept)} of {facts.frame_count} frames · {duration:.3f} s · "
            f"{format_source_dimensions(*size)} px"
        )
        artifact = self._artifact
        if artifact is not None and artifact.width and artifact.height:
            rendered = format_source_dimensions(artifact.width, artifact.height)
            file_size = format_source_file_size(artifact.file_size)
            text += f" · rendered {rendered} px, {file_size}"
        return text

    def _show_transform_error(self, message: str) -> None:
        self.readout_label.setText(message)
        self._set_widget_invalid(self.readout_label, True, message)

    def _clear_transform_error(self) -> None:
        self._set_widget_invalid(self.readout_label, False, "")

    def _show_resize_error(self) -> None:
        for spin in (self.width_spinbox, self.height_spinbox):
            self._set_widget_invalid(spin, True, _RESIZE_ERROR)

    def _clear_resize_error(self) -> None:
        for spin in (self.width_spinbox, self.height_spinbox):
            self._set_widget_invalid(spin, False, "")

    def _set_widget_invalid(self, widget: QWidget, invalid: bool, message: str) -> None:
        widget.setProperty("invalid", invalid)
        widget.setToolTip(message)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    # -- enablement -----------------------------------------------------

    def _refresh_enabled(self) -> None:
        enabled = self._editable and self._facts is not None
        for widget in self._core_widgets():
            widget.setEnabled(enabled)
        playhead_enabled = enabled and self._playhead is not None
        self.first_playhead_button.setEnabled(playhead_enabled)
        self.last_playhead_button.setEnabled(playhead_enabled)

    def _core_widgets(self) -> tuple[QWidget, ...]:
        return (
            self.first_frame_spinbox,
            self.last_frame_spinbox,
            self.crop_edit_checkbox,
            self.aspect_combo,
            *self._crop_field_widgets(),
            self.width_spinbox,
            self.height_spinbox,
            self.percent_spinbox,
            self.mismatch_combo,
            self.size_preset_combo,
        )

    def _crop_field_widgets(self) -> tuple[QSpinBox, ...]:
        return (
            self.crop_x_spinbox,
            self.crop_y_spinbox,
            self.crop_width_spinbox,
            self.crop_height_spinbox,
        )


def _rhu(value: Fraction) -> int:
    """Round half up (towards +infinity) on an exact Fraction."""
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)
