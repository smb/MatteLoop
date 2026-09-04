from __future__ import annotations

from fractions import Fraction

from matteloop.core.crop import (
    centered_crop_for_aspect,
    crop_from_drag,
    fit_crop_aspect,
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


def test_fit_crop_aspect_on_a_corner_keeps_the_opposite_corner_fixed() -> None:
    fitted = fit_crop_aspect(
        CropSpec(2, 3, 6, 5),
        Fraction(2, 1),
        "north_west",
        source_width=20,
        source_height=20,
    )

    assert fitted == CropSpec(2, 5, 6, 3)
    # The south-east corner (the one opposite the dragged north-west handle)
    # is exactly where it was before the re-fit.
    assert (fitted.x + fitted.width, fitted.y + fitted.height) == (8, 8)
    assert fitted.width == 2 * fitted.height


def test_fit_crop_aspect_on_a_corner_adjusts_the_axis_needing_less_change() -> None:
    fitted = fit_crop_aspect(
        CropSpec(0, 0, 8, 4),
        Fraction(1, 1),
        "south_east",
        source_width=20,
        source_height=20,
    )

    assert fitted == CropSpec(0, 0, 4, 4)


def test_fit_crop_aspect_on_an_edge_recentres_the_perpendicular_axis() -> None:
    fitted = fit_crop_aspect(
        CropSpec(2, 3, 6, 5),
        Fraction(3, 1),
        "east",
        source_width=20,
        source_height=20,
    )

    assert fitted == CropSpec(2, 5, 6, 2)
    assert fitted.width == 3 * fitted.height


def test_fit_crop_aspect_leaves_a_body_move_unchanged() -> None:
    crop = CropSpec(2, 3, 6, 5)

    fitted = fit_crop_aspect(
        crop, Fraction(4, 3), "crop", source_width=20, source_height=20
    )

    assert fitted == crop


def test_fit_crop_aspect_clamps_a_result_that_would_leave_the_source() -> None:
    fitted = fit_crop_aspect(
        CropSpec(0, 0, 6, 5),
        Fraction(2, 1),
        "south_east",
        source_width=8,
        source_height=8,
    )

    assert fitted.x + fitted.width <= 8
    assert fitted.y + fitted.height <= 8
    assert fitted.width >= 1
    assert fitted.height >= 1


def test_centered_crop_for_aspect_uses_the_full_width_when_it_fits() -> None:
    crop = centered_crop_for_aspect(
        Fraction(16, 9), source_width=1920, source_height=1080
    )

    assert crop == CropSpec(0, 0, 1920, 1080)


def test_centered_crop_for_aspect_centres_a_narrower_rectangle() -> None:
    crop = centered_crop_for_aspect(Fraction(1, 1), source_width=200, source_height=100)

    assert crop == CropSpec(50, 0, 100, 100)
