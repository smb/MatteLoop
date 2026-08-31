from __future__ import annotations

from matteloop.core.crop import (
    crop_from_drag,
    nudge_crop,
    oriented_point_from_widget,
    oriented_rect_to_source_rect,
)
from matteloop.core.geometry import (
    CropGeometryState,
    PointF,
    RectF,
    SizeF,
    build_crop_geometry,
)
from matteloop.core.specs import CropSpec


def test_dragging_crop_moves_and_clamps_in_oriented_source_pixels() -> None:
    moved = crop_from_drag(
        CropSpec(2, 3, 6, 5),
        "crop",
        PointF(10, 10),
        PointF(30, -10),
        source_width=10,
        source_height=10,
    )

    assert moved == CropSpec(4, 0, 6, 5)


def test_resizing_crop_keeps_the_opposite_edge_and_one_pixel_minimum() -> None:
    resized = crop_from_drag(
        CropSpec(2, 3, 6, 5),
        "north_west",
        PointF(10, 10),
        PointF(30, 30),
        source_width=10,
        source_height=10,
    )

    assert resized == CropSpec(7, 7, 1, 1)


def test_crop_keyboard_nudge_uses_ten_source_pixels_with_shift_step() -> None:
    resized = nudge_crop(
        CropSpec(2, 3, 6, 5),
        "east",
        dx=10,
        dy=0,
        source_width=10,
        source_height=10,
    )

    assert resized == CropSpec(2, 3, 8, 5)


def test_oriented_crop_geometry_maps_rotation_and_pixel_aspect_once() -> None:
    crop = CropSpec(0, 0, 8, 8)
    raw_crop = oriented_rect_to_source_rect(
        crop,
        source_width=16,
        source_height=8,
        rotation=270,
        pixel_aspect=2,
    )
    geometry = build_crop_geometry(
        state=CropGeometryState(
            source_size=SizeF(16, 8),
            crop=raw_crop,
            rotation=270,
            pixel_aspect=2,
        ),
        viewport=SizeF(8, 32),
        dpr=1,
    )

    assert geometry.visual["crop"] == RectF(0, 0, 8, 8)
    assert oriented_point_from_widget(geometry, PointF(0, 0)) == PointF(0, 0)
