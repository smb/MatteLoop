from __future__ import annotations

import gc
import weakref
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from decimal import Decimal, Inexact, localcontext

import pytest
from PIL import Image

from rembggui.core.errors import ErrorCode, ValidationError
from rembggui.core.geometry import (
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


def test_framing_fuses_large_padding_and_stretch_without_intermediate_allocation(
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
    original_new = Image.new

    def observed_new(
        mode: str,
        size: tuple[int, int],
        color: object = 0,
    ) -> Image.Image:
        allocated_sizes.append(size)
        return original_new(mode, size, color)

    def forbidden_resize(*args: object, **kwargs: object) -> object:
        raise AssertionError("resize intermediate was allocated")

    monkeypatch.setattr(Image, "new", observed_new)
    monkeypatch.setattr(Image.Image, "resize", forbidden_resize)

    framed = apply_framing(frame, plan)

    assert plan.output_size == (162, 328)
    assert framed.size == (162, 328)
    assert allocated_sizes == [(162, 328)]


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


def test_plan_rejects_oversized_source_even_when_global_crop_is_small() -> None:
    with pytest.raises(ValidationError) as exc:
        framing_plan(
            16_384,
            128,
            global_bounds=PixelBounds(0, 0, 128, 128),
        )

    assert exc.value.code is ErrorCode.INVALID_FINAL_DIMENSIONS


def test_alpha_bounds_preflights_mask_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = rgba(16_384, 1)
    allocations = 0

    def counted(*args: object, **kwargs: object) -> object:
        nonlocal allocations
        allocations += 1
        raise AssertionError("alpha channel allocated before preflight")

    monkeypatch.setattr(Image.Image, "getchannel", counted)

    with pytest.raises(ValidationError) as exc:
        alpha_bounds(image, Decimal("2"))

    assert exc.value.code is ErrorCode.INVALID_FINAL_DIMENSIONS
    assert allocations == 0


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
