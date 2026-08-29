"""Target-scaled timeline thumbnail requests safe for worker threads."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QSize, QSizeF
from PySide6.QtGui import QImage

from rembggui.core.errors import AppError, ErrorCode
from rembggui.jobs.source import decode_frame

MIN_FILMSTRIP_SAMPLES = 12
MAX_FILMSTRIP_SAMPLES = 48
FILMSTRIP_SAMPLE_SPACING = 90.0


@dataclass(frozen=True, slots=True, init=False)
class ThumbnailRequest:
    """Immutable normalized thumbnail work identity."""

    source_id: str
    timestamp: Fraction
    logical_size: tuple[float, float]
    dpr: float
    generation: int

    def __init__(
        self,
        source_id: str,
        timestamp: Fraction,
        logical_size: QSize | QSizeF | tuple[float, float],
        dpr: float,
        generation: int,
    ) -> None:
        width, height = _logical_dimensions(logical_size)
        normalized_dpr = _finite_number(dpr, "dpr")
        if not isinstance(source_id, str) or not source_id:
            raise _thumbnail_error("source_id must be a non-empty string")
        if not isinstance(timestamp, Fraction):
            raise _thumbnail_error("timestamp must be a Fraction")
        if width <= 0 or height <= 0:
            raise _thumbnail_error("logical dimensions must be positive")
        if normalized_dpr <= 0:
            raise _thumbnail_error("device-pixel ratio must be positive")
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
        ):
            raise _thumbnail_error("generation must be a non-negative integer")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "logical_size", (width, height))
        object.__setattr__(self, "dpr", normalized_dpr)
        object.__setattr__(self, "generation", generation)

    @property
    def physical_size(self) -> QSize:
        """Return physical dimensions using deterministic round-half-up."""
        width, height = self.logical_size
        return QSize(
            _round_positive(width * self.dpr),
            _round_positive(height * self.dpr),
        )


@dataclass(frozen=True, slots=True)
class ThumbnailResult:
    """A worker-produced scaled image; intentionally never a QPixmap."""

    request: ThumbnailRequest
    image: QImage


def generate_thumbnail(
    path: Path | str,
    request: ThumbnailRequest,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> ThumbnailResult:
    """Decode and scale one source frame entirely on the calling worker."""
    if not isinstance(request, ThumbnailRequest):
        raise _thumbnail_error("request must be a ThumbnailRequest")
    if is_cancelled is not None and is_cancelled():
        raise _cancelled_error()
    decoded = decode_frame(
        path,
        request.timestamp,
        request_id=request.generation,
        is_cancelled=is_cancelled,
    )
    if is_cancelled is not None and is_cancelled():
        raise _cancelled_error()
    physical = request.physical_size
    try:
        scaled = decoded.image.resize(
            (physical.width(), physical.height()), Image.Resampling.LANCZOS
        ).convert("RGBA")
        raw = scaled.tobytes("raw", "RGBA")
        # copy() detaches QImage from the temporary Python bytes buffer.
        image = QImage(
            raw,
            scaled.width,
            scaled.height,
            scaled.width * 4,
            QImage.Format.Format_RGBA8888,
        ).copy()
    except (
        OSError,
        TypeError,
        ValueError,
        RuntimeError,
        MemoryError,
        OverflowError,
    ) as error:
        raise AppError(
            ErrorCode.INVALID_THUMBNAIL,
            "thumbnail",
            "thumbnail.scale.failed",
            f"thumbnail could not be scaled to physical target pixels: {error}",
            "retry-thumbnail",
        ) from error
    if image.isNull() or image.size() != physical:
        raise AppError(
            ErrorCode.INVALID_THUMBNAIL,
            "thumbnail",
            "thumbnail.scale.invalid-result",
            "thumbnail scaling did not produce the requested physical dimensions",
            "retry-thumbnail",
        )
    return ThumbnailResult(request, image)


def filmstrip_timestamps(
    duration: Fraction,
    logical_viewport_width: int | float,
    *,
    target_spacing: float = FILMSTRIP_SAMPLE_SPACING,
) -> tuple[Fraction, ...]:
    """Return 12..48 exact, evenly spaced samples over the complete source."""
    if not isinstance(duration, Fraction) or duration <= 0:
        raise _thumbnail_error("duration must be a positive Fraction")
    width = _finite_number(logical_viewport_width, "logical_viewport_width")
    spacing = _finite_number(target_spacing, "target_spacing")
    if width <= 0 or spacing <= 0:
        raise _thumbnail_error("viewport width and target spacing must be positive")
    count = min(
        MAX_FILMSTRIP_SAMPLES,
        max(MIN_FILMSTRIP_SAMPLES, _round_positive(width / spacing)),
    )
    return tuple(duration * index / (count - 1) for index in range(count))


def _logical_dimensions(
    value: QSize | QSizeF | tuple[float, float],
) -> tuple[float, float]:
    if isinstance(value, (QSize, QSizeF)):
        raw_width, raw_height = value.width(), value.height()
    elif isinstance(value, tuple) and len(value) == 2:
        raw_width, raw_height = value
    else:
        raise _thumbnail_error("logical_size must be QSize, QSizeF, or a pair")
    return (
        _finite_number(raw_width, "logical width"),
        _finite_number(raw_height, "logical height"),
    )


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise _thumbnail_error(f"{name} must be finite")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise _thumbnail_error(f"{name} must be finite") from error
    if not math.isfinite(number):
        raise _thumbnail_error(f"{name} must be finite")
    return number


def _round_positive(value: float) -> int:
    rounded = Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return max(1, int(rounded))


def _thumbnail_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.INVALID_THUMBNAIL,
        "thumbnail",
        "thumbnail.request.invalid",
        detail,
        "adjust-thumbnail-request",
    )


def _cancelled_error() -> AppError:
    return AppError(
        ErrorCode.JOB_CANCELLED,
        "thumbnail",
        "thumbnail.cancelled",
        "thumbnail request was cancelled between native operations",
        "none",
    )
