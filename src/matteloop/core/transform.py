"""Crop and resize a finished cut's frames, Qt-free, never touching stored PNGs.

Both the render/rebuild path and the result player call these two functions
after ``geometry.apply_framing`` so encoder and preview stay pixel-identical
(design decision D2). The resize idiom mirrors ``core/webp.py:1226-1236``
exactly: convert to premultiplied ``RGBa``, resample with Lanczos, convert
back to straight ``RGBA``.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from PIL import Image

from matteloop.core import geometry
from matteloop.core.specs import CropSpec, MismatchMode, ResizeSpec, TransformSpec


@dataclass(frozen=True)
class ResizePlan:
    """The concrete pixel operations one ``resolve_resize`` call resolves to."""

    scaled: tuple[int, int]
    canvas: tuple[int, int]
    offset: tuple[int, int]
    crop_box: tuple[int, int, int, int] | None
    resample: bool


def resolve_resize(source_size: tuple[int, int], resize: ResizeSpec) -> ResizePlan:
    """Resolve *resize* against *source_size* into exact integer pixel ops."""
    width, height = source_size
    target_width, target_height = resize.width, resize.height
    if target_width is not None and target_height is None:
        return _single_axis(width, height, target_width, vertical=False)
    if target_height is not None and target_width is None:
        return _single_axis(width, height, target_height, vertical=True)
    assert target_width is not None and target_height is not None
    target = (target_width, target_height)
    if width * target_height == height * target_width:
        return _plan(target, target, source_size)
    if resize.mismatch is MismatchMode.STRETCH:
        return _plan(target, target, source_size)
    if resize.mismatch is MismatchMode.COVER:
        return _cover(width, height, target_width, target_height)
    if resize.mismatch is MismatchMode.PAD:
        return _pad(width, height, target_width, target_height)
    return _keep(width, height, target_width, target_height)


def transformed_size(
    framed_size: tuple[int, int], spec: TransformSpec
) -> tuple[int, int]:
    """Return the final output size for *spec*; pure arithmetic, no bounds check."""
    size = (
        (spec.crop.width, spec.crop.height) if spec.crop is not None else framed_size
    )
    if spec.resize is None:
        return size
    return resolve_resize(size, spec.resize).canvas


def apply_transform(image: Image.Image, spec: TransformSpec) -> Image.Image:
    """Apply *spec* to *image*. Never mutates its input; always returns RGBA.

    An identity spec returns ``image`` itself so an unmodified cut is never
    re-saved (AC 1 byte identity).
    """
    if spec.is_identity:
        return image
    working = _crop(image, spec.crop)
    if spec.resize is None:
        return working
    plan = resolve_resize(working.size, spec.resize)
    return _place(_resample(working, plan), plan)


def _single_axis(width: int, height: int, target: int, *, vertical: bool) -> ResizePlan:
    if vertical:
        scale = Fraction(target, height)
        scaled = (_rhu(width * scale), target)
    else:
        scale = Fraction(target, width)
        scaled = (target, _rhu(height * scale))
    return _plan(scaled, scaled, (width, height))


def _keep(width: int, height: int, target_width: int, target_height: int) -> ResizePlan:
    scale = min(Fraction(target_width, width), Fraction(target_height, height))
    scaled = (_rhu(width * scale), _rhu(height * scale))
    return _plan(scaled, scaled, (width, height))


def _cover(
    width: int, height: int, target_width: int, target_height: int
) -> ResizePlan:
    scale = max(Fraction(target_width, width), Fraction(target_height, height))
    scaled = (_rhu(width * scale), _rhu(height * scale))
    left = (scaled[0] - target_width) // 2
    top = (scaled[1] - target_height) // 2
    crop_box = (left, top, left + target_width, top + target_height)
    target = (target_width, target_height)
    return _plan(scaled, target, (width, height), crop_box=crop_box)


def _pad(
    width: int, height: int, target_width: int, target_height: int
) -> ResizePlan:
    scale = min(Fraction(target_width, width), Fraction(target_height, height))
    scaled = (_rhu(width * scale), _rhu(height * scale))
    offset = ((target_width - scaled[0]) // 2, (target_height - scaled[1]) // 2)
    target = (target_width, target_height)
    return _plan(scaled, target, (width, height), offset=offset)


def _plan(
    scaled: tuple[int, int],
    canvas: tuple[int, int],
    source_size: tuple[int, int],
    *,
    offset: tuple[int, int] = (0, 0),
    crop_box: tuple[int, int, int, int] | None = None,
) -> ResizePlan:
    resample = scaled != source_size
    if resample:
        geometry._validate_allocation_budget(scaled, 4, "transform resample")
    return ResizePlan(
        scaled=scaled,
        canvas=canvas,
        offset=offset,
        crop_box=crop_box,
        resample=resample,
    )


def _rhu(value: Fraction) -> int:
    """Floor of ``value + 1/2`` on an exact Fraction (house rounding)."""
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def _crop(image: Image.Image, crop: CropSpec | None) -> Image.Image:
    if crop is None:
        return _ensure_rgba(image)
    box = (crop.x, crop.y, crop.x + crop.width, crop.y + crop.height)
    return _ensure_rgba(image.crop(box))


def _ensure_rgba(image: Image.Image) -> Image.Image:
    return image if image.mode == "RGBA" else image.convert("RGBA")


def _resample(image: Image.Image, plan: ResizePlan) -> Image.Image:
    if not plan.resample:
        return image
    premultiplied = image.convert("RGBa")
    resized = premultiplied.resize(plan.scaled, Image.Resampling.LANCZOS)
    return resized.convert("RGBA")


def _place(image: Image.Image, plan: ResizePlan) -> Image.Image:
    if plan.crop_box is not None:
        return image.crop(plan.crop_box)
    if plan.canvas != image.size:
        canvas = Image.new("RGBA", plan.canvas, (0, 0, 0, 0))
        canvas.paste(image, plan.offset)
        return canvas
    return image
