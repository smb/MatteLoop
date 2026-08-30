"""Pure formatting for source metadata shown by the Qt shell."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import TypedDict

_SOURCE_FILENAME_MAX_LENGTH = 40


class SourcePresentation(TypedDict):
    source_filename: str
    source_dimensions: str
    source_duration: str
    source_frame_rate: str
    source_file_size: str
    source_path: str | None


def format_source_filename(path: object) -> str:
    """Return a display filename with a bounded middle-elided basename."""
    if path is None:
        return ""
    filename = Path(str(path)).name
    if not filename:
        return ""
    if len(filename) <= _SOURCE_FILENAME_MAX_LENGTH:
        return filename
    extension = Path(filename).suffix
    available = _SOURCE_FILENAME_MAX_LENGTH - len(extension) - 1
    if available <= 0:
        return filename[-_SOURCE_FILENAME_MAX_LENGTH:]
    prefix_length = (available + 1) // 2
    suffix_length = available - prefix_length
    tail = filename[-(suffix_length + len(extension)) :]
    return f"{filename[:prefix_length]}…{tail}"


def format_source_dimensions(width: object, height: object) -> str:
    """Return oriented source dimensions in the source-strip style."""
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or width <= 0
        or height <= 0
    ):
        return ""
    return f"{width} × {height}"


def format_source_duration(duration: object) -> str:
    """Return a source duration as m:ss.hh or h:mm:ss.hh."""
    if not isinstance(duration, Fraction) or duration < 0:
        return ""
    hundredths = _rounded_hundredths(duration)
    total_seconds, centiseconds = divmod(hundredths, 100)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"
    return f"{minutes}:{seconds:02d}.{centiseconds:02d}"


def format_source_frame_rate(rate: object) -> str:
    """Return a source frame rate rounded to two decimal places."""
    if not isinstance(rate, Fraction) or rate <= 0:
        return ""
    hundredths = _rounded_hundredths(rate)
    whole, decimal = divmod(hundredths, 100)
    value = f"{whole}.{decimal:02d}".rstrip("0").rstrip(".")
    return f"{value} fps"


def format_source_file_size(size: object) -> str:
    """Return a source file size using compact binary units."""
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return ""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return ""


def present_source_metadata(
    metadata: object | None, loading: bool = False
) -> SourcePresentation:
    """Return source-strip text and the unshortened path for accessibility."""
    if metadata is None:
        return {
            "source_filename": "Reading video…" if loading else "",
            "source_dimensions": "",
            "source_duration": "",
            "source_frame_rate": "",
            "source_file_size": "",
            "source_path": None,
        }
    path = getattr(metadata, "path", None)
    full_path = str(path) if path is not None else None
    average_rate = getattr(metadata, "average_rate", None)
    rate = average_rate or getattr(metadata, "peak_rate", None)
    return {
        "source_filename": format_source_filename(path),
        "source_dimensions": format_source_dimensions(
            getattr(metadata, "width", None), getattr(metadata, "height", None)
        ),
        "source_duration": format_source_duration(
            getattr(metadata, "duration", None)
        ),
        "source_frame_rate": format_source_frame_rate(rate),
        "source_file_size": format_source_file_size(
            getattr(metadata, "file_size", None)
        ),
        "source_path": full_path,
    }


def _rounded_hundredths(value: Fraction) -> int:
    rounded = value * 100 + Fraction(1, 2)
    return rounded.numerator // rounded.denominator
