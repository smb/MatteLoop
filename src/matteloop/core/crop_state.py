"""Reducer events for the oriented crop editor."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from matteloop.core.crop import clamp_crop
from matteloop.core.specs import CropSpec

if TYPE_CHECKING:
    from matteloop.core.state import AppState


@dataclass(frozen=True, slots=True)
class CropChanged:
    crop: CropSpec


@dataclass(frozen=True, slots=True)
class ResetCrop:
    pass


@dataclass(frozen=True, slots=True)
class CropToggleChanged:
    enabled: bool


CropEvent = CropChanged | ResetCrop | CropToggleChanged


def default_crop_for_source(metadata: object) -> CropSpec | None:
    dimensions = _source_dimensions(metadata)
    return None if dimensions is None else CropSpec(0, 0, *dimensions)


def reduce_crop(state: AppState, event: CropEvent) -> AppState:
    """Apply crop edits and route changed bounds through preview invalidation."""
    from matteloop.core.state import (
        PreviewInvalidated,
        SourceState,
        capabilities,
        reduce,
    )
    from matteloop.core.tokens import PreviewInvalidationReason

    if isinstance(event, CropToggleChanged):
        if not isinstance(event.enabled, bool) or event.enabled == state.crop_enabled:
            return state
        return replace(state, crop_enabled=event.enabled)
    if not capabilities(state).can_edit or state.source is not SourceState.READY:
        return state
    dimensions = _source_dimensions(state.source_value)
    if dimensions is None:
        return state
    if isinstance(event, ResetCrop):
        candidate = CropSpec(0, 0, *dimensions)
    else:
        candidate = clamp_crop(event.crop, *dimensions)
    if candidate == state.crop:
        return state
    return reduce(
        replace(state, crop=candidate),
        PreviewInvalidated(PreviewInvalidationReason.CROP),
    )


def _source_dimensions(metadata: object | None) -> tuple[int, int] | None:
    width = getattr(metadata, "width", None)
    height = getattr(metadata, "height", None)
    if (
        type(width) is not int
        or type(height) is not int
        or width < 1
        or height < 1
    ):
        return None
    return width, height
