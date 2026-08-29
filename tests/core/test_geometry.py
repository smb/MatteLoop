from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from fractions import Fraction

import pytest

from rembggui.core.geometry import (
    CropGeometryState,
    InteractionGeometry,
    MediaTransform,
    PointF,
    RectF,
    SizeF,
    TimelineGeometryState,
    build_crop_geometry,
    build_timeline_geometry,
)


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        (0, PointF(20.0, 60.0)),
        (90, PointF(140.0, 20.0)),
        (180, PointF(180.0, 140.0)),
        (270, PointF(60.0, 180.0)),
    ],
)
def test_media_transform_rotates_clockwise_and_letterboxes(
    rotation: int, expected: PointF
) -> None:
    transform = MediaTransform(
        source_size=SizeF(100, 50),
        viewport=SizeF(200, 200),
        rotation=rotation,
    )

    assert transform.source_to_widget(PointF(10, 5)) == expected
    restored = transform.widget_to_source(expected)
    assert restored.x == pytest.approx(10.0, abs=1e-9)
    assert restored.y == pytest.approx(5.0, abs=1e-9)


def test_media_transform_honors_pixel_aspect_zoom_pan_and_clamping() -> None:
    aspect = MediaTransform(
        source_size=SizeF(100, 50),
        viewport=SizeF(200, 200),
        pixel_aspect=2.0,
    )
    zoomed = MediaTransform(
        source_size=SizeF(100, 50),
        viewport=SizeF(200, 200),
        zoom=2.0,
        pan=PointF(10, -5),
    )

    assert aspect.source_to_widget(PointF(10, 5)) == PointF(20, 80)
    assert zoomed.content_rect == RectF(-90, -5, 400, 200)
    assert zoomed.source_to_widget(PointF(25, 25)) == PointF(10, 95)
    assert zoomed.clamp_source_point(PointF(-4, 88)) == PointF(0, 50)
    assert zoomed.clamp_source_rect(RectF(-10, 40, 30, 20)) == RectF(0, 30, 30, 20)


def test_crop_geometry_shares_transform_across_every_consumer() -> None:
    state = CropGeometryState(
        source_size=SizeF(100, 50),
        crop=RectF(10, 10, 30, 20),
        screen_origin=PointF(100, 50),
    )

    geometry = build_crop_geometry(
        state=state,
        viewport=SizeF(200, 100),
        dpr=1.5,
    )

    assert geometry.visual["crop"] == RectF(20, 20, 60, 40)
    assert geometry.visual["north_west"] == RectF(16, 16, 8, 8)
    assert geometry.visual["east"] == RectF(76, 36, 8, 8)
    assert geometry.visual["south"] == RectF(46, 56, 8, 8)
    assert geometry.visual["north_west"].center() == geometry.focus[
        "north_west"
    ].center()
    assert geometry.pointer_hit["north_west"].contains(
        geometry.visual["north_west"]
    )
    assert geometry.touch_hit["north_west"] == RectF(0, 0, 44, 44)
    assert geometry.accessible_screen["north_west"] == RectF(150, 75, 66, 66)
    assert geometry.screen_to_source(PointF(180, 105)) == PointF(10, 10)


@pytest.mark.parametrize(
    ("dpr", "expected_size"),
    [(1.0, SizeF(44, 44)), (1.5, SizeF(66, 66)), (2.0, SizeF(88, 88))],
)
def test_accessible_geometry_scales_once_at_common_device_ratios(
    dpr: float, expected_size: SizeF
) -> None:
    state = CropGeometryState(
        source_size=SizeF(100, 100),
        crop=RectF(0, 0, 100, 100),
    )

    geometry = build_crop_geometry(state=state, viewport=SizeF(200, 200), dpr=dpr)

    assert geometry.accessible_screen["south_east"].size() == expected_size


def test_crop_is_clamped_before_all_eight_handle_rectangles_are_derived() -> None:
    state = CropGeometryState(
        source_size=SizeF(100, 50),
        crop=RectF(90, 45, 30, 20),
    )

    geometry = build_crop_geometry(state=state, viewport=SizeF(200, 100), dpr=1)

    assert geometry.visual["crop"] == RectF(140, 60, 60, 40)
    assert set(geometry.visual) == {
        "crop",
        "north_west",
        "north",
        "north_east",
        "east",
        "south_east",
        "south",
        "south_west",
        "west",
    }


def test_every_crop_hit_target_is_effective_and_inside_the_viewport() -> None:
    geometry = build_crop_geometry(
        state=CropGeometryState(
            source_size=SizeF(100, 100),
            crop=RectF(0, 0, 1, 1),
        ),
        viewport=SizeF(100, 100),
        dpr=1,
    )

    for rect in geometry.pointer_hit.values():
        assert rect.width >= 24
        assert rect.height >= 24
        assert RectF(0, 0, 100, 100).contains(rect)
    for rect in geometry.touch_hit.values():
        assert rect.width >= 44
        assert rect.height >= 44
        assert RectF(0, 0, 100, 100).contains(rect)


def test_hit_targets_use_the_full_viewport_when_it_is_below_minimum_size() -> None:
    geometry = build_crop_geometry(
        state=CropGeometryState(
            source_size=SizeF(100, 100),
            crop=RectF(0, 0, 1, 1),
        ),
        viewport=SizeF(20, 10),
        dpr=1,
    )

    assert set(geometry.pointer_hit.values()) == {RectF(0, 0, 20, 10)}
    assert set(geometry.touch_hit.values()) == {RectF(0, 0, 20, 10)}


def test_overlap_priority_is_dragged_then_focused_then_handles_then_region() -> None:
    state = CropGeometryState(
        source_size=SizeF(100, 100),
        crop=RectF(20, 20, 60, 60),
        focused="crop",
        dragged="north_west",
    )
    geometry = build_crop_geometry(state=state, viewport=SizeF(100, 100), dpr=1)

    assert geometry.hit_test(PointF(20, 20)) == "north_west"
    assert geometry.hit_test(PointF(50, 50)) == "crop"


def test_timeline_geometry_maps_range_and_playhead_and_resolves_overlap() -> None:
    state = TimelineGeometryState(
        duration=Decimal("10"),
        start=Decimal("2"),
        end=Decimal("8"),
        playhead=Decimal("2"),
    )
    geometry = build_timeline_geometry(
        state=state,
        viewport=SizeF(1000, 100),
        dpr=2,
    )

    assert geometry.visual["range"] == RectF(212, 0, 576, 100)
    assert geometry.visual["start_handle"].center() == PointF(212, 50)
    assert geometry.visual["end_handle"].center() == PointF(788, 50)
    assert geometry.visual["playhead"].center().x == 212
    assert geometry.hit_test(PointF(212, 50)) == "start_handle"
    assert geometry.source_to_widget(PointF(5, 0)).x == 500
    assert geometry.widget_to_source(PointF(500, 50)).x == pytest.approx(5)

    focused = build_timeline_geometry(
        state=TimelineGeometryState(
            duration=Decimal("10"),
            start=Decimal("2"),
            end=Decimal("8"),
            playhead=Decimal("2"),
            focused="playhead",
        ),
        viewport=SizeF(1000, 100),
        dpr=2,
    )
    assert focused.hit_test(PointF(212, 50)) == "playhead"


def test_timeline_range_retains_rational_values_and_one_frame_minimum() -> None:
    state = TimelineGeometryState(
        duration=Fraction(1),
        start=Fraction(1, 3),
        end=Fraction(11, 30),
        playhead=Fraction(1, 3),
        fps=30,
    )

    geometry = build_timeline_geometry(
        state=state, viewport=SizeF(940, 80), dpr=1
    )

    assert geometry.visual["start_handle"].center().x == pytest.approx(320)
    assert geometry.visual["end_handle"].center().x == pytest.approx(350)

    with pytest.raises(ValueError, match="one output-frame"):
        TimelineGeometryState(
            duration=Fraction(1),
            start=Fraction(1, 3),
            end=Fraction(7, 20),
            playhead=Fraction(1, 3),
            fps=30,
        )


def test_every_timeline_target_is_effective_and_inside_the_viewport() -> None:
    geometry = build_timeline_geometry(
        state=TimelineGeometryState(
            duration=Fraction(1),
            start=Fraction(1, 2),
            end=Fraction(51, 100),
            playhead=Fraction(1, 2),
            fps=240,
        ),
        viewport=SizeF(100, 100),
        dpr=1,
    )

    for rect in geometry.pointer_hit.values():
        assert rect.width >= 24
        assert rect.height >= 24
        assert RectF(0, 0, 100, 100).contains(rect)
    for rect in geometry.touch_hit.values():
        assert rect.width >= 44
        assert rect.height >= 44
        assert RectF(0, 0, 100, 100).contains(rect)


def test_timeline_targets_use_full_viewport_below_minimum_size() -> None:
    geometry = build_timeline_geometry(
        state=TimelineGeometryState(
            duration=Fraction(1),
            start=Fraction(0),
            end=Fraction(1),
            playhead=Fraction(1, 2),
        ),
        viewport=SizeF(20, 10),
        dpr=1,
    )

    assert set(geometry.pointer_hit.values()) == {RectF(0, 0, 20, 10)}
    assert set(geometry.touch_hit.values()) == {RectF(0, 0, 20, 10)}


def test_timeline_requires_frozen_screen_and_viewport_values() -> None:
    with pytest.raises(ValueError, match="screen_origin"):
        TimelineGeometryState(
            duration=Fraction(1),
            start=Fraction(0),
            end=Fraction(1),
            playhead=Fraction(0),
            screen_origin=[0, 0],  # type: ignore[arg-type]
        )

    state = TimelineGeometryState(
        duration=Fraction(1),
        start=Fraction(0),
        end=Fraction(1),
        playhead=Fraction(0),
    )
    with pytest.raises(ValueError, match="viewport"):
        build_timeline_geometry(
            state=state,
            viewport=[100, 100],  # type: ignore[arg-type]
            dpr=1,
        )


def test_geometry_values_and_maps_are_immutable() -> None:
    state = CropGeometryState(
        source_size=SizeF(100, 100),
        crop=RectF(0, 0, 100, 100),
    )
    geometry = build_crop_geometry(state=state, viewport=SizeF(100, 100), dpr=1)

    with pytest.raises(FrozenInstanceError):
        geometry.transform = geometry.transform  # type: ignore[misc]
    with pytest.raises(TypeError):
        geometry.visual["crop"] = RectF(0, 0, 1, 1)  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        geometry.visual._mapping = {}  # type: ignore[misc]


def test_direct_interaction_geometry_copies_mutable_inputs() -> None:
    rectangles = {"target": RectF(1, 2, 24, 24)}
    priority = ["target"]
    transform = MediaTransform(SizeF(100, 100), SizeF(100, 100))

    geometry = InteractionGeometry(
        visual=rectangles,  # type: ignore[arg-type]
        pointer_hit=rectangles,  # type: ignore[arg-type]
        touch_hit=rectangles,  # type: ignore[arg-type]
        focus=rectangles,  # type: ignore[arg-type]
        accessible_screen=rectangles,  # type: ignore[arg-type]
        transform=transform,
        priority=priority,  # type: ignore[arg-type]
    )
    rectangles["target"] = RectF(90, 90, 1, 1)
    priority.clear()

    assert geometry.visual["target"] == RectF(1, 2, 24, 24)
    assert geometry.priority == ("target",)
    assert geometry.hit_test(PointF(2, 3)) == "target"


def test_direct_interaction_geometry_rejects_arbitrary_mutable_transform() -> None:
    rectangles = {"target": RectF(1, 2, 24, 24)}

    with pytest.raises(ValueError, match="transform"):
        InteractionGeometry(
            visual=rectangles,  # type: ignore[arg-type]
            pointer_hit=rectangles,  # type: ignore[arg-type]
            touch_hit=rectangles,  # type: ignore[arg-type]
            focus=rectangles,  # type: ignore[arg-type]
            accessible_screen=rectangles,  # type: ignore[arg-type]
            transform=object(),  # type: ignore[arg-type]
            priority=("target",),
        )


def test_interaction_geometry_rejects_mutable_media_transform_subclass() -> None:
    class MutableMediaTransform(MediaTransform):
        offset = 0.0

        def source_to_widget(self, point: PointF) -> PointF:
            mapped = super().source_to_widget(point)
            return PointF(mapped.x + self.offset, mapped.y)

    transform = MutableMediaTransform(SizeF(100, 100), SizeF(100, 100))
    rectangles = {"target": RectF(1, 2, 24, 24)}

    with pytest.raises(ValueError, match="transform"):
        InteractionGeometry(
            visual=rectangles,  # type: ignore[arg-type]
            pointer_hit=rectangles,  # type: ignore[arg-type]
            touch_hit=rectangles,  # type: ignore[arg-type]
            focus=rectangles,  # type: ignore[arg-type]
            accessible_screen=rectangles,  # type: ignore[arg-type]
            transform=transform,
            priority=("target",),
        )


@pytest.mark.parametrize(
    ("kwargs", "detail"),
    [
        ({"rotation": 45}, "rotation"),
        ({"pixel_aspect": 0.0}, "pixel_aspect"),
        ({"zoom": float("nan")}, "zoom"),
        ({"dpr": float("inf")}, "dpr"),
    ],
)
def test_media_transform_rejects_invalid_finite_geometry(
    kwargs: dict[str, float | int], detail: str
) -> None:
    with pytest.raises(ValueError, match=detail):
        MediaTransform(
            source_size=SizeF(100, 100),
            viewport=SizeF(100, 100),
            **kwargs,
        )


def test_core_geometry_imports_no_qt_modules() -> None:
    import rembggui.core.geometry as geometry_module

    assert all("PySide" not in value for value in geometry_module.__dict__ if value)
