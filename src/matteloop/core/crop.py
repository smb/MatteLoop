"""Pure crop editing operations in oriented source coordinates."""

from __future__ import annotations

import math

from matteloop.core.geometry import (
    InteractionGeometry,
    MediaTransform,
    PointF,
    RectF,
    SizeF,
)
from matteloop.core.specs import CropSpec

_HANDLES = frozenset(
    {
        "north_west",
        "north",
        "north_east",
        "east",
        "south_east",
        "south",
        "south_west",
        "west",
    }
)


def crop_from_drag(
    crop: CropSpec,
    target: str,
    start: PointF,
    current: PointF,
    *,
    source_width: int,
    source_height: int,
) -> CropSpec:
    """Apply one pointer drag, rounded to integral oriented source pixels."""
    dx = _rounded_delta(current.x - start.x)
    dy = _rounded_delta(current.y - start.y)
    return nudge_crop(
        crop,
        target,
        dx=dx,
        dy=dy,
        source_width=source_width,
        source_height=source_height,
    )


def nudge_crop(
    crop: CropSpec,
    target: str,
    *,
    dx: int,
    dy: int,
    source_width: int,
    source_height: int,
) -> CropSpec:
    """Move a crop or resize one handle by integral source-pixel deltas."""
    _validate_dimensions(source_width, source_height)
    if target != "crop" and target not in _HANDLES:
        raise ValueError("unknown crop interaction target")
    if target == "crop":
        return _move_crop(crop, dx, dy, source_width, source_height)
    return _resize_crop(crop, target, dx, dy, source_width, source_height)


def clamp_crop(crop: CropSpec, source_width: int, source_height: int) -> CropSpec:
    """Clamp an otherwise valid crop to a non-empty source rectangle."""
    _validate_dimensions(source_width, source_height)
    left = min(max(crop.x, 0), source_width - 1)
    top = min(max(crop.y, 0), source_height - 1)
    width = min(max(crop.width, 1), source_width - left)
    height = min(max(crop.height, 1), source_height - top)
    return CropSpec(left, top, width, height)


def oriented_rect_to_source_rect(
    crop: CropSpec,
    *,
    source_width: int,
    source_height: int,
    rotation: int,
    pixel_aspect: float,
) -> RectF:
    """Map oriented crop pixels into the raw coordinates used by geometry."""
    transform = _orientation_transform(
        source_width, source_height, rotation, pixel_aspect
    )
    return transform.widget_rect_to_source(
        RectF(crop.x, crop.y, crop.width, crop.height)
    )


def oriented_point_from_widget(
    geometry: InteractionGeometry, point: PointF
) -> PointF:
    """Map a widget point through one crop geometry snapshot to oriented space."""
    transform = geometry.transform
    if not isinstance(transform, MediaTransform):
        raise ValueError("crop geometry must use a media transform")
    raw = geometry.widget_to_source(point)
    return _orientation_transform_from_media(transform).source_to_widget(raw)


def _move_crop(
    crop: CropSpec, dx: int, dy: int, source_width: int, source_height: int
) -> CropSpec:
    return CropSpec(
        min(max(crop.x + dx, 0), source_width - crop.width),
        min(max(crop.y + dy, 0), source_height - crop.height),
        crop.width,
        crop.height,
    )


def _resize_crop(
    crop: CropSpec,
    target: str,
    dx: int,
    dy: int,
    source_width: int,
    source_height: int,
) -> CropSpec:
    left, top, right, bottom = crop.x, crop.y, crop.x + crop.width, crop.y + crop.height
    if "west" in target:
        left = min(max(left + dx, 0), right - 1)
    if "east" in target:
        right = min(max(right + dx, left + 1), source_width)
    if "north" in target:
        top = min(max(top + dy, 0), bottom - 1)
    if "south" in target:
        bottom = min(max(bottom + dy, top + 1), source_height)
    return CropSpec(left, top, right - left, bottom - top)


def _orientation_transform(
    source_width: int, source_height: int, rotation: int, pixel_aspect: float
) -> MediaTransform:
    _validate_dimensions(source_width, source_height)
    return MediaTransform(
        source_size=SizeF(source_width, source_height),
        viewport=SizeF(
            source_height if rotation in {90, 270} else source_width,
            source_width * pixel_aspect if rotation in {90, 270} else source_height,
        ),
        rotation=rotation,
        pixel_aspect=pixel_aspect,
    )


def _orientation_transform_from_media(transform: MediaTransform) -> MediaTransform:
    return _orientation_transform(
        int(transform.source_size.width),
        int(transform.source_size.height),
        transform.rotation,
        transform.pixel_aspect,
    )


def _rounded_delta(value: float) -> int:
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def _validate_dimensions(source_width: int, source_height: int) -> None:
    if (
        not isinstance(source_width, int)
        or isinstance(source_width, bool)
        or not isinstance(source_height, int)
        or isinstance(source_height, bool)
        or source_width < 1
        or source_height < 1
    ):
        raise ValueError("source dimensions must be positive integers")
