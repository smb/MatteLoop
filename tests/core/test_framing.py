from __future__ import annotations

from decimal import Decimal

import pytest
from PIL import Image

from rembggui.core.errors import ErrorCode, ValidationError
from rembggui.core.geometry import (
    PixelBounds,
    alpha_bounds,
    apply_framing,
    apply_source_crop,
    solve_proportional_scale,
    union_alpha_bounds,
)


def rgba(width: int, height: int) -> Image.Image:
    return Image.new("RGBA", (width, height), (0, 0, 0, 0))


def test_source_crop_uses_exact_half_open_pixel_bounds() -> None:
    image = rgba(4, 4)
    image.putpixel((1, 1), (255, 0, 0, 255))
    image.putpixel((2, 2), (0, 255, 0, 255))

    cropped = apply_source_crop(image, PixelBounds(1, 1, 3, 3))

    assert cropped.size == (2, 2)
    assert cropped.getpixel((0, 0)) == (255, 0, 0, 255)
    assert cropped.getpixel((1, 1)) == (0, 255, 0, 255)


def test_alpha_bounds_are_half_open_and_threshold_percent_is_strict() -> None:
    image = rgba(8, 7)
    image.putpixel((2, 3), (1, 2, 3, 1))
    image.putpixel((5, 4), (1, 2, 3, 128))

    assert alpha_bounds(image, Decimal("0")) == PixelBounds(2, 3, 6, 5)
    assert alpha_bounds(image, Decimal("50")) == PixelBounds(5, 4, 6, 5)
    assert alpha_bounds(image, Decimal("100")) is None


def test_union_keeps_transparent_frames_and_unites_visible_extents() -> None:
    first = rgba(16, 12)
    second = rgba(16, 12)
    third = rgba(16, 12)
    first.putpixel((2, 3), (1, 1, 1, 255))
    second.putpixel((11, 8), (1, 1, 1, 255))

    assert union_alpha_bounds((first, second, third), Decimal("2")) == PixelBounds(
        2, 3, 12, 9
    )


def test_empty_range_wide_alpha_union_is_structured_failure() -> None:
    with pytest.raises(ValidationError) as exc:
        union_alpha_bounds((rgba(128, 128), rgba(128, 128)), Decimal("100"))

    assert exc.value.code is ErrorCode.INVALID_FRAMING
    assert exc.value.stage == "framing"
    assert "no visible pixels" in exc.value.technical_detail


def test_foreground_touching_every_edge_produces_the_full_frame_union() -> None:
    image = rgba(128, 128)
    for point in ((0, 0), (127, 0), (0, 127), (127, 127)):
        image.putpixel(point, (255, 255, 255, 255))

    assert union_alpha_bounds((image,), Decimal("2")) == PixelBounds(
        0, 0, 128, 128
    )


def test_union_rejects_different_frame_canvases() -> None:
    with pytest.raises(ValidationError) as exc:
        union_alpha_bounds((rgba(128, 128), rgba(129, 128)), Decimal("0"))

    assert exc.value.code is ErrorCode.INVALID_FRAMING


def test_union_trim_padding_and_stretch_keep_one_canvas_for_every_frame() -> None:
    first = rgba(200, 180)
    second = rgba(200, 180)
    first.putpixel((2, 3), (255, 0, 0, 255))
    second.putpixel((129, 131), (0, 255, 0, 255))

    framed = apply_framing(
        (first, second),
        global_bounds=PixelBounds(2, 3, 130, 132),
        padding=1,
        stretch_x=Decimal("1.5"),
    )

    assert [image.size for image in framed] == [(195, 131), (195, 131)]
    assert framed[0].mode == "RGBA"
    assert framed[1].mode == "RGBA"


def test_framing_without_global_trim_preserves_canvas_before_padding() -> None:
    framed = apply_framing(
        (rgba(128, 128),),
        global_bounds=None,
        padding=2,
        stretch_x=Decimal("1.0"),
    )

    assert framed[0].size == (132, 132)


def test_global_crop_precedes_equal_padding() -> None:
    image = rgba(132, 132)
    image.putpixel((2, 3), (200, 100, 50, 255))

    framed = apply_framing(
        (image,),
        global_bounds=PixelBounds(2, 3, 130, 131),
        padding=1,
        stretch_x=Decimal("1"),
    )

    assert framed[0].size == (130, 130)
    assert framed[0].getpixel((0, 0)) == (0, 0, 0, 0)
    assert framed[0].getpixel((1, 1)) == (200, 100, 50, 255)


@pytest.mark.parametrize(
    ("width", "height", "bounds", "padding", "stretch"),
    [
        (128, 128, None, 0, Decimal("0.99")),
        (128, 128, PixelBounds(0, 0, 127, 128), 0, Decimal("1")),
        (128, 128, PixelBounds(0, 0, 128, 127), 0, Decimal("1")),
    ],
)
def test_framing_rejects_sub_128_results_without_upscale_or_invented_padding(
    width: int,
    height: int,
    bounds: PixelBounds | None,
    padding: int,
    stretch: Decimal,
) -> None:
    with pytest.raises(ValidationError) as exc:
        apply_framing(
            (rgba(width, height),),
            global_bounds=bounds,
            padding=padding,
            stretch_x=stretch,
        )

    assert exc.value.code is ErrorCode.INVALID_FINAL_DIMENSIONS


def test_stretch_uses_decimal_half_up_width_and_checks_max_before_allocating() -> None:
    framed = apply_framing(
        (rgba(128, 128),),
        global_bounds=None,
        padding=0,
        stretch_x=Decimal("1.00390625"),
    )
    assert framed[0].size == (129, 128)

    with pytest.raises(ValidationError) as exc:
        apply_framing(
            (rgba(128, 128),),
            global_bounds=None,
            padding=20_000,
            stretch_x=Decimal("1"),
        )
    assert exc.value.code is ErrorCode.INVALID_FINAL_DIMENSIONS


def test_apply_framing_rejects_non_rgba_or_empty_inputs() -> None:
    with pytest.raises(ValidationError, match="RGBA"):
        apply_framing(
            (Image.new("RGB", (128, 128)),),
            global_bounds=None,
            padding=0,
            stretch_x=Decimal("1"),
        )
    with pytest.raises(ValidationError, match="at least one"):
        apply_framing(
            (), global_bounds=None, padding=0, stretch_x=Decimal("1")
        )


@pytest.mark.parametrize(
    "threshold",
    [Decimal("-0.001"), Decimal("100.001"), float("nan"), float("inf")],
)
def test_alpha_bounds_rejects_out_of_range_or_non_finite_thresholds(
    threshold: Decimal | float,
) -> None:
    with pytest.raises(ValidationError) as exc:
        alpha_bounds(rgba(128, 128), threshold)
    assert exc.value.code is ErrorCode.INVALID_FRAMING


@pytest.mark.parametrize("stretch", [0, -1, float("nan"), float("inf")])
def test_framing_rejects_non_positive_or_non_finite_stretch(
    stretch: float | int,
) -> None:
    with pytest.raises(ValidationError) as exc:
        apply_framing(
            (rgba(128, 128),),
            global_bounds=None,
            padding=0,
            stretch_x=stretch,
        )
    assert exc.value.code is ErrorCode.INVALID_FRAMING


def test_proportional_scale_uses_headroom_cap_and_min_dimension_clamp() -> None:
    assert solve_proportional_scale(
        1000,
        1000,
        current_scale=Decimal("1"),
        target_bytes=250_000,
        current_bytes=940_000,
    ) == Decimal("0.5")
    assert solve_proportional_scale(
        1000,
        1000,
        current_scale=Decimal("1"),
        target_bytes=1_001_000,
        current_bytes=1_000_000,
    ) == Decimal("0.97")
    assert solve_proportional_scale(
        400,
        200,
        current_scale=Decimal("0.8"),
        target_bytes=10,
        current_bytes=1_000_000,
    ) == Decimal("0.64")


@pytest.mark.parametrize(
    ("target", "current"),
    [(0, 1), (1, 0), (True, 1), (1, True)],
)
def test_proportional_scale_rejects_invalid_byte_counts(
    target: int, current: int
) -> None:
    with pytest.raises(ValidationError) as exc:
        solve_proportional_scale(
            400,
            200,
            current_scale=Decimal("1"),
            target_bytes=target,
            current_bytes=current,
        )
    assert exc.value.code is ErrorCode.IMPOSSIBLE_SIZE


def test_pixel_bounds_reject_bool_negative_empty_and_inverted_values() -> None:
    for values in [
        (True, 0, 1, 1),
        (-1, 0, 1, 1),
        (0, 0, 0, 1),
        (0, 2, 1, 1),
    ]:
        with pytest.raises(ValueError):
            PixelBounds(*values)
