from __future__ import annotations

import pytest
from PIL import Image

from matteloop.core.specs import CropSpec, MismatchMode, ResizeSpec, TransformSpec
from matteloop.core.transform import apply_transform, resolve_resize


def _half_red_half_blue(width: int, height: int) -> Image.Image:
    """An opaque red/blue split frame with one semi-transparent pixel.

    The semi-transparent pixel exercises the premultiply step: an
    implementation that resizes without premultiplying first produces
    different RGB values around it than the house idiom does.
    """
    image = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    half = width // 2
    for x in range(width):
        color = (255, 0, 0, 255) if x < half else (0, 0, 255, 255)
        for y in range(height):
            image.putpixel((x, y), color)
    image.putpixel((1, 1), (255, 0, 0, 40))
    return image


def _reference_resample(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Replicate the house resize idiom verbatim (core/webp.py:1226-1236)."""
    premultiplied = image.convert("RGBa")
    resized = premultiplied.resize(size, Image.Resampling.LANCZOS)
    return resized.convert("RGBA")


def test_identity_transform_returns_the_same_object() -> None:
    source = _half_red_half_blue(128, 128)

    assert apply_transform(source, TransformSpec()) is source


def test_apply_transform_keep_mode_leaves_a_matching_aspect_untouched() -> None:
    source = _half_red_half_blue(128, 128)
    spec = TransformSpec(resize=ResizeSpec(256, 128, MismatchMode.KEEP))

    plan = resolve_resize((128, 128), spec.resize)
    assert plan.scaled == (128, 128)
    assert plan.resample is False

    result = apply_transform(source, spec)

    assert result.size == (128, 128)
    assert result.mode == "RGBA"
    assert list(result.get_flattened_data()) == list(source.get_flattened_data())


def test_apply_transform_stretch_mode_fills_the_exact_canvas() -> None:
    source = _half_red_half_blue(128, 128)
    spec = TransformSpec(resize=ResizeSpec(256, 128, MismatchMode.STRETCH))

    result = apply_transform(source, spec)

    assert result.size == (256, 128)
    assert result.mode == "RGBA"
    expected = _reference_resample(source, (256, 128))
    assert list(result.get_flattened_data()) == list(expected.get_flattened_data())


def test_apply_transform_cover_mode_scales_then_crops_the_middle() -> None:
    source = _half_red_half_blue(128, 128)
    spec = TransformSpec(resize=ResizeSpec(256, 128, MismatchMode.COVER))

    plan = resolve_resize((128, 128), spec.resize)
    assert plan.scaled == (256, 256)
    assert plan.crop_box == (0, 64, 256, 192)

    result = apply_transform(source, spec)

    assert result.size == (256, 128)
    assert result.mode == "RGBA"
    reference = _reference_resample(source, (256, 256)).crop((0, 64, 256, 192))
    assert list(result.get_flattened_data()) == list(reference.get_flattened_data())


def test_apply_transform_pad_mode_pastes_untouched_frame_with_transparent_bars() -> (
    None
):
    source = _half_red_half_blue(128, 128)
    spec = TransformSpec(resize=ResizeSpec(256, 128, MismatchMode.PAD))

    plan = resolve_resize((128, 128), spec.resize)
    assert plan.scaled == (128, 128)
    assert plan.resample is False
    assert plan.offset == (64, 0)

    result = apply_transform(source, spec)

    assert result.size == (256, 128)
    assert result.mode == "RGBA"
    for x in range(0, 64):
        for y in range(128):
            assert result.getpixel((x, y)) == (0, 0, 0, 0)
    for x in range(64, 192):
        for y in range(128):
            assert result.getpixel((x, y)) == source.getpixel((x - 64, y))
    for x in range(192, 256):
        for y in range(128):
            assert result.getpixel((x, y)) == (0, 0, 0, 0)


@pytest.mark.parametrize(
    "mismatch",
    [MismatchMode.KEEP, MismatchMode.STRETCH, MismatchMode.COVER, MismatchMode.PAD],
)
def test_width_only_resize_yields_the_proportional_height_in_every_mode(
    mismatch: MismatchMode,
) -> None:
    source = _half_red_half_blue(256, 128)
    spec = TransformSpec(resize=ResizeSpec(width=256, mismatch=mismatch))

    result = apply_transform(source, spec)

    assert result.size == (256, 128)
    assert result.mode == "RGBA"


def test_resize_to_the_same_size_does_not_resample() -> None:
    source = _half_red_half_blue(128, 128)
    spec = TransformSpec(resize=ResizeSpec(128, 128, MismatchMode.STRETCH))

    plan = resolve_resize((128, 128), spec.resize)
    assert plan.resample is False

    result = apply_transform(source, spec)

    assert result.size == (128, 128)
    assert list(result.get_flattened_data()) == list(source.get_flattened_data())


def test_cover_places_the_extra_odd_pixel_on_the_right() -> None:
    source = _half_red_half_blue(129, 128)
    spec = TransformSpec(resize=ResizeSpec(128, 128, MismatchMode.COVER))

    plan = resolve_resize((129, 128), spec.resize)
    assert plan.scaled == (129, 128)
    assert plan.resample is False
    assert plan.crop_box == (0, 0, 128, 128)

    result = apply_transform(source, spec)

    assert result.size == (128, 128)
    assert list(result.get_flattened_data()) == list(
        source.crop((0, 0, 128, 128)).get_flattened_data()
    )


def test_pad_places_the_extra_odd_pixel_on_the_right() -> None:
    source = _half_red_half_blue(128, 128)
    spec = TransformSpec(resize=ResizeSpec(257, 128, MismatchMode.PAD))

    plan = resolve_resize((128, 128), spec.resize)
    assert plan.scaled == (128, 128)
    assert plan.resample is False
    assert plan.offset == (64, 0)

    result = apply_transform(source, spec)

    assert result.size == (257, 128)
    for x in range(64):
        for y in range(128):
            assert result.getpixel((x, y)) == (0, 0, 0, 0)
    for x in range(64, 192):
        for y in range(128):
            assert result.getpixel((x, y)) == source.getpixel((x - 64, y))
    for x in range(192, 257):
        for y in range(128):
            assert result.getpixel((x, y)) == (0, 0, 0, 0)


def test_apply_transform_crop_only_returns_the_cropped_region_as_rgba() -> None:
    source = _half_red_half_blue(128, 128)
    spec = TransformSpec(crop=CropSpec(x=0, y=0, width=64, height=128))

    result = apply_transform(source, spec)

    assert result.size == (64, 128)
    assert result.mode == "RGBA"
    expected = source.crop((0, 0, 64, 128))
    assert list(result.get_flattened_data()) == list(expected.get_flattened_data())
