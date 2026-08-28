"""Exact timestamp grids and integer-millisecond WebP frame delays."""

from __future__ import annotations

from fractions import Fraction

from rembggui.core.errors import ErrorCode, ValidationError
from rembggui.core.specs import MAX_FPS, MIN_FPS

MAX_OUTPUT_FRAMES = 100_000


def sample_times(
    start: Fraction, end: Fraction, fps: int
) -> tuple[Fraction, ...]:
    """Return the exact output timestamp grid over the half-open range."""
    _validate_sampling_inputs(start, end, fps)
    frame_step = Fraction(1, fps)
    frame_count = _ceiling((end - start) * fps)
    if frame_count > MAX_OUTPUT_FRAMES:
        raise ValidationError(
            ErrorCode.INVALID_SAMPLING,
            "timebase",
            "output frame count must not exceed 100000",
        )
    return tuple(start + index * frame_step for index in range(frame_count))


def webp_delays(frame_count: int, fps: int) -> tuple[int, ...]:
    """Distribute cumulative rounding into integer-millisecond frame delays."""
    if (
        not isinstance(frame_count, int)
        or isinstance(frame_count, bool)
        or not 1 <= frame_count <= MAX_OUTPUT_FRAMES
    ):
        raise ValidationError(
            ErrorCode.INVALID_SAMPLING,
            "timebase",
            "frame_count must be an integer between 1 and 100000",
        )
    _validate_fps(fps)

    return tuple(
        _round_positive_fraction(Fraction((index + 1) * 1000, fps))
        - _round_positive_fraction(Fraction(index * 1000, fps))
        for index in range(frame_count)
    )


def _validate_sampling_inputs(start: Fraction, end: Fraction, fps: int) -> None:
    if not isinstance(start, Fraction) or not isinstance(end, Fraction):
        raise ValidationError(
            ErrorCode.INVALID_SAMPLING,
            "timebase",
            "start and end must be Fraction values",
        )
    if start < 0 or end <= start:
        raise ValidationError(
            ErrorCode.INVALID_SAMPLING,
            "timebase",
            "sampling interval must satisfy 0 <= start < end",
        )
    _validate_fps(fps)


def _validate_fps(fps: int) -> None:
    if (
        not isinstance(fps, int)
        or isinstance(fps, bool)
        or not MIN_FPS <= fps <= MAX_FPS
    ):
        raise ValidationError(
            ErrorCode.INVALID_SAMPLING,
            "timebase",
            "fps must be an integer between 1 and 240",
        )


def _ceiling(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def _round_positive_fraction(value: Fraction) -> int:
    quotient, remainder = divmod(value.numerator, value.denominator)
    return quotient + int(remainder * 2 >= value.denominator)
