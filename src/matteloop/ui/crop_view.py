"""GUI-thread rendering helpers for the source crop editor."""

from __future__ import annotations

from PySide6.QtGui import QImage

from matteloop.ui.crop_canvas import CropCanvas
from matteloop.ui.inspector import Inspector
from matteloop.ui.presentation_model import PresentationModel


def render_source_editor(
    canvas: CropCanvas,
    inspector: Inspector,
    model: PresentationModel,
) -> None:
    """Render one presenter snapshot into the crop canvas and inspector."""
    editable = not model.editor_locked
    canvas.set_frame(
        model.source_frame if isinstance(model.source_frame, QImage) else None
    )
    canvas.apply_presentation(model.crop, active=model.crop_enabled, editable=editable)
    inspector.apply_crop(model.crop, model.crop_enabled, editable)
    inspector.apply_parameters(model.parameters, editable)
    inspector.set_model_status(model.model_status)
