"""Immutable crop values passed from the presenter to the Qt editor."""

from __future__ import annotations

from dataclasses import dataclass

from matteloop.core.specs import CropSpec
from matteloop.core.state import AppState, SourceState


@dataclass(frozen=True, slots=True)
class CropPresentation:
    source_id: str
    width: int
    height: int
    coded_width: int
    coded_height: int
    rotation: int
    pixel_aspect: float
    crop: CropSpec


def present_crop(state: AppState) -> CropPresentation | None:
    """Expose oriented crop values and raw orientation metadata to the widget."""
    if (
        state.source is not SourceState.READY
        or state.source_id is None
        or state.crop is None
    ):
        return None
    metadata = state.source_value
    width = getattr(metadata, "width", None)
    height = getattr(metadata, "height", None)
    coded_width = getattr(metadata, "coded_width", width)
    coded_height = getattr(metadata, "coded_height", height)
    rotation = getattr(metadata, "rotation", 0)
    pixel_aspect = getattr(metadata, "pixel_aspect", 1.0)
    if (
        type(width) is not int
        or type(height) is not int
        or width < 1
        or height < 1
        or type(coded_width) is not int
        or type(coded_height) is not int
        or coded_width < 1
        or coded_height < 1
        or type(rotation) is not int
    ):
        return None
    try:
        aspect = float(pixel_aspect)
    except (TypeError, ValueError, OverflowError):
        return None
    return CropPresentation(
        state.source_id,
        width,
        height,
        coded_width,
        coded_height,
        rotation,
        aspect,
        state.crop,
    )
