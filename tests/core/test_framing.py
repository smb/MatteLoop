from __future__ import annotations

import gc
import weakref
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from decimal import Decimal, Inexact, localcontext

import pytest
from PIL import Image

from matteloop.core.errors import ErrorCode, ValidationError
from matteloop.core.geometry import (
    FramingPlan,
    PixelBounds,
    alpha_bounds,
    apply_framing,
    apply_source_crop,
    solve_proportional_scale,
    union_alpha_bounds,
)


def rgba(width: int, height: int) -> Image.Image:
    return Image.new("RGBA", (width, height), (0, 0, 0, 0))


def framing_plan(
    width: int,
    height: int,
    *,
    global_bounds: PixelBounds | None = None,
    padding: int = 0,
    stretch_x: Decimal | float | int = Decimal("1"),
) -> FramingPlan:
    return FramingPlan(
        source_size=(width, height),
        global_bounds=global_bounds,
        padding=padding,
        stretch_x=stretch_x,
    )


def reference_framing(image: Image.Image, plan: FramingPlan) -> Image.Image:
    bounds = plan.content_bounds
    cropped = image.crop((bounds.left, bounds.top, bounds.right, bounds.bottom))
    padded = Image.new(
        "RGBA",
        plan.padded_size,
        (0, 0, 0, 0),
    )
    padded.alpha_composite(cropped, (plan.padding, plan.padding))
    return padded.resize(plan.output_size, Image.Resampling.BICUBIC)


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


def test_union_consumes_a_one_shot_iterable_once_without_retaining_frames() -> None:
    references: list[weakref.ReferenceType[Image.Image]] = []
    iterations = 0
    maximum_live_inputs = 0

    class OneShotFrames:
        def __iter__(self) -> Iterator[Image.Image]:
            nonlocal iterations, maximum_live_inputs
            iterations += 1
            if iterations > 1:
                raise AssertionError("frame iterable was restarted")
            for x in (2, 4, 6, 8, 10, 11):
                image = rgba(16, 12)
                image.putpixel((x, 4), (255, 255, 255, 255))
                references.append(weakref.ref(image))
                maximum_live_inputs = max(
                    maximum_live_inputs,
                    sum(reference() is not None for reference in references),
                )
                yield image
                del image
                gc.collect()

    bounds = union_alpha_bounds(OneShotFrames(), Decimal("2"))
    gc.collect()

    assert bounds == PixelBounds(2, 4, 12, 5)
    assert iterations == 1
    assert maximum_live_inputs <= 2
    assert all(reference() is None for reference in references)


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

    plan = framing_plan(
        200,
        180,
        global_bounds=PixelBounds(2, 3, 130, 132),
        padding=1,
        stretch_x=Decimal("1.5"),
    )
    framed = [apply_framing(frame, plan) for frame in (first, second)]

    assert [image.size for image in framed] == [(195, 131), (195, 131)]
    assert framed[0].mode == "RGBA"
    assert framed[1].mode == "RGBA"


def test_framing_without_global_trim_preserves_canvas_before_padding() -> None:
    plan = framing_plan(128, 128, padding=2)
    framed = apply_framing(rgba(128, 128), plan)

    assert framed.size == (132, 132)


def test_framing_plan_copies_source_size_and_is_frozen() -> None:
    source_size = [128, 128]
    plan = FramingPlan(source_size=source_size)  # type: ignore[arg-type]
    source_size[0] = 999

    assert plan.source_size == (128, 128)
    with pytest.raises(FrozenInstanceError):
        plan.padding = 10  # type: ignore[misc]


def test_global_crop_precedes_equal_padding() -> None:
    image = rgba(132, 132)
    image.putpixel((2, 3), (200, 100, 50, 255))

    plan = framing_plan(
        132,
        132,
        global_bounds=PixelBounds(2, 3, 130, 131),
        padding=1,
        stretch_x=Decimal("1"),
    )
    framed = apply_framing(image, plan)

    assert framed.size == (130, 130)
    assert framed.getpixel((0, 0)) == (0, 0, 0, 0)
    assert framed.getpixel((1, 1)) == (200, 100, 50, 255)


@pytest.mark.parametrize(
    ("content_width", "stretch_x"),
    [(256, Decimal("0.5")), (128, Decimal("1.5"))],
)
def test_framing_matches_isolated_premultiplied_reference_for_resize(
    content_width: int, stretch_x: Decimal
) -> None:
    source = Image.new("RGBA", (content_width + 4, 132), (255, 0, 0, 255))
    bounds = PixelBounds(2, 2, content_width + 2, 130)
    source.paste((20, 40, 200, 128), (2, 2, content_width + 2, 130))
    plan = framing_plan(
        *source.size,
        global_bounds=bounds,
        padding=2,
        stretch_x=stretch_x,
    )

    actual = apply_framing(source, plan)
    expected = reference_framing(source, plan)

    assert actual.size == expected.size
    assert actual.tobytes() == expected.tobytes()
    assert all(
        alpha == 0 or red < 100
        for y in range(actual.height)
        for x in range(actual.width)
        for red, _green, _blue, alpha in (actual.getpixel((x, y)),)
    )
    alpha = actual.getchannel("A")
    middle_row = [alpha.getpixel((x, actual.height // 2)) for x in range(actual.width)]
    assert middle_row == list(reversed(middle_row))


def test_no_stretch_crop_and_padding_have_exact_identity_placement() -> None:
    source = Image.new("RGBA", (132, 132), (255, 0, 0, 255))
    source.paste((10, 80, 220, 128), (2, 2, 130, 130))
    plan = framing_plan(
        132,
        132,
        global_bounds=PixelBounds(2, 2, 130, 130),
        padding=2,
    )

    actual = apply_framing(source, plan)

    assert actual.size == (132, 132)
    assert actual.getpixel((0, 0)) == (0, 0, 0, 0)
    assert actual.getpixel((1, 1)) == (0, 0, 0, 0)
    assert actual.getpixel((2, 2)) == (10, 80, 220, 128)
    assert actual.getpixel((129, 129)) == (10, 80, 220, 128)
    assert actual.getpixel((130, 130)) == (0, 0, 0, 0)


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
        framing_plan(
            width,
            height,
            global_bounds=bounds,
            padding=padding,
            stretch_x=stretch,
        )

    assert exc.value.code is ErrorCode.INVALID_FINAL_DIMENSIONS


def test_stretch_uses_decimal_half_up_width_and_checks_max_before_allocating() -> None:
    rounded_up = framing_plan(128, 128, stretch_x=Decimal("1.00390625"))
    below_tie = framing_plan(128, 128, stretch_x=Decimal("1.003906249"))
    assert rounded_up.output_size == (129, 128)
    assert below_tie.output_size == (128, 128)

    with pytest.raises(ValidationError) as exc:
        framing_plan(
            128,
            128,
            padding=20_000,
            stretch_x=Decimal("1"),
        )
    assert exc.value.code is ErrorCode.INVALID_FINAL_DIMENSIONS


def test_apply_framing_rejects_non_rgba_or_wrong_canvas() -> None:
    plan = framing_plan(128, 128)
    with pytest.raises(ValidationError, match="RGBA"):
        apply_framing(Image.new("RGB", (128, 128)), plan)
    with pytest.raises(ValidationError, match="source canvas"):
        apply_framing(rgba(129, 128), plan)


def test_framing_does_not_accumulate_outputs_or_retain_inputs() -> None:
    plan = framing_plan(128, 128)
    input_references: list[weakref.ReferenceType[Image.Image]] = []
    output_references: list[weakref.ReferenceType[Image.Image]] = []

    for _ in range(6):
        frame = rgba(128, 128)
        input_references.append(weakref.ref(frame))
        output = apply_framing(frame, plan)
        output_references.append(weakref.ref(output))
        del frame
        del output

    gc.collect()

    assert all(reference() is None for reference in input_references)
    assert all(reference() is None for reference in output_references)


def test_framing_allocates_only_preflighted_working_and_output_canvases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = rgba(16_000, 128)
    plan = framing_plan(
        16_000,
        128,
        padding=100,
        stretch_x=Decimal("0.01"),
    )
    allocated_sizes: list[tuple[int, int]] = []
    resize_targets: list[tuple[int, int]] = []
    original_new = Image.new
    original_resize = Image.Image.resize

    def observed_new(
        mode: str,
        size: tuple[int, int],
        color: object = 0,
    ) -> Image.Image:
        allocated_sizes.append(size)
        return original_new(mode, size, color)

    def observed_resize(
        image: Image.Image,
        size: tuple[int, int],
        *args: object,
        **kwargs: object,
    ) -> Image.Image:
        resize_targets.append(size)
        return original_resize(image, size, *args, **kwargs)

    monkeypatch.setattr(Image, "new", observed_new)
    monkeypatch.setattr(Image.Image, "resize", observed_resize)

    framed = apply_framing(frame, plan)

    assert plan.output_size == (162, 328)
    assert framed.size == (162, 328)
    assert (16_200, 328) in allocated_sizes
    assert resize_targets == [(162, 328)]


def test_final_rgba_convert_releases_the_padded_predecessor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = rgba(128, 128)
    plan = framing_plan(128, 128, padding=1, stretch_x=Decimal("1.5"))
    padded_reference: weakref.ReferenceType[Image.Image] | None = None
    live_full_image_counts: list[int] = []
    original_new = Image.new
    original_convert = Image.Image.convert

    def observed_new(
        mode: str,
        size: tuple[int, int],
        color: object = 0,
    ) -> Image.Image:
        nonlocal padded_reference
        created = original_new(mode, size, color)
        if mode == "RGBa" and size == (130, 130):
            padded_reference = weakref.ref(created)
        return created

    def observed_convert(
        image: Image.Image,
        mode: str | None = None,
        *args: object,
        **kwargs: object,
    ) -> Image.Image:
        converted = original_convert(image, mode, *args, **kwargs)
        if image.mode == "RGBa" and mode == "RGBA" and image.size == (195, 130):
            gc.collect()
            assert padded_reference is not None
            live_full_image_counts.append(
                1  # caller-owned source
                + int(padded_reference() is not None)
                + 1  # resized RGBa input
                + 1  # converted RGBA result
            )
        return converted

    monkeypatch.setattr(Image, "new", observed_new)
    monkeypatch.setattr(Image.Image, "convert", observed_convert)

    result = apply_framing(frame, plan)

    assert result.size == (195, 130)
    assert live_full_image_counts == [3]


def test_apply_framing_rejects_plan_subclasses_before_property_or_pillow_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    property_accesses = 0
    pillow_allocations = 0

    class OversizedMutablePlan(FramingPlan):
        @property
        def content_bounds(self) -> PixelBounds:
            nonlocal property_accesses
            property_accesses += 1
            return PixelBounds(0, 0, 128, 128)

        @property
        def padded_size(self) -> tuple[int, int]:
            nonlocal property_accesses
            property_accesses += 1
            return (268_435_456, 2)

    unsafe_plan = OversizedMutablePlan(
        source_size=(128, 128),
        padding=1,
        stretch_x=Decimal("1.5"),
    )
    frame = rgba(128, 128)

    def counted(*args: object, **kwargs: object) -> object:
        nonlocal pillow_allocations
        pillow_allocations += 1
        raise AssertionError("Pillow allocation occurred before plan type validation")

    monkeypatch.setattr(Image.Image, "crop", counted)
    monkeypatch.setattr(Image, "new", counted)
    monkeypatch.setattr(Image.Image, "resize", counted)
    monkeypatch.setattr(Image.Image, "convert", counted)

    with pytest.raises(ValidationError) as exc:
        apply_framing(frame, unsafe_plan)

    assert exc.value.code is ErrorCode.INVALID_FRAMING
    assert property_accesses == 0
    assert pillow_allocations == 0


def test_invalid_plan_performs_no_pillow_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allocations = 0

    def counted(*args: object, **kwargs: object) -> object:
        nonlocal allocations
        allocations += 1
        raise AssertionError("Pillow was called before preflight completed")

    monkeypatch.setattr(Image, "new", counted)
    monkeypatch.setattr(Image.Image, "resize", counted)
    monkeypatch.setattr(Image.Image, "transform", counted)

    with pytest.raises(ValidationError) as exc:
        framing_plan(128, 128, padding=20_000, stretch_x=Decimal("0.01"))

    assert exc.value.code is ErrorCode.INVALID_FINAL_DIMENSIONS
    assert allocations == 0


def test_plan_accepts_large_source_axis_when_byte_budget_and_output_are_safe() -> None:
    plan = framing_plan(
        16_384,
        1,
        global_bounds=PixelBounds(0, 0, 128, 1),
        padding=64,
    )

    assert plan.source_size == (16_384, 1)
    assert plan.output_size == (256, 129)


def test_alpha_bounds_accepts_large_axis_when_mask_bytes_are_safe() -> None:
    image = rgba(16_384, 1)
    image.putpixel((16_383, 0), (255, 255, 255, 255))

    assert alpha_bounds(image, Decimal("2")) == PixelBounds(
        16_383, 0, 16_384, 1
    )


def test_plan_rejects_excessive_source_bytes_without_allocating_an_image() -> None:
    with pytest.raises(ValidationError) as exc:
        framing_plan(
            268_435_457,
            1,
            global_bounds=PixelBounds(0, 0, 128, 1),
            padding=64,
        )

    assert exc.value.code is ErrorCode.INVALID_FINAL_DIMENSIONS
    assert "byte budget" in exc.value.technical_detail


def test_plan_rejects_excessive_padded_working_bytes_before_output_math() -> None:
    with pytest.raises(ValidationError) as exc:
        framing_plan(
            200_000_000,
            1,
            padding=64,
            stretch_x=Decimal("0.000001"),
        )

    assert exc.value.code is ErrorCode.INVALID_FINAL_DIMENSIONS
    assert "padded working image" in exc.value.technical_detail
    assert "byte budget" in exc.value.technical_detail


def test_final_output_still_enforces_webp_axis_cap_independently() -> None:
    with pytest.raises(ValidationError) as exc:
        framing_plan(128, 128, stretch_x=Decimal("128"))

    assert exc.value.code is ErrorCode.INVALID_FINAL_DIMENSIONS
    assert exc.value.technical_detail == (
        "final dimensions must each be between 128 and 16383 pixels"
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
        framing_plan(128, 128, stretch_x=stretch)
    assert exc.value.code is ErrorCode.INVALID_FRAMING


def test_threshold_and_stretch_ignore_hostile_decimal_context() -> None:
    image = rgba(128, 128)
    image.putpixel((4, 5), (1, 2, 3, 85))

    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        bounds = alpha_bounds(image, Decimal("33.333333333"))
        plan = framing_plan(
            128, 128, stretch_x=Decimal("1.003906249")
        )

    assert bounds == PixelBounds(4, 5, 5, 6)
    assert plan.output_size == (128, 128)


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


def test_proportional_scale_is_deterministic_under_hostile_decimal_context() -> None:
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = True
        result = solve_proportional_scale(
            1000,
            1000,
            current_scale=Decimal("1"),
            target_bytes=500_000,
            current_bytes=1_000_000,
        )

    assert result == Decimal(
        "0.68556546004010441249358714490848489604606434610013262754851081856785171151368170"
    )


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
