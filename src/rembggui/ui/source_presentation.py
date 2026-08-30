"""Pure formatting for source metadata shown by the Qt shell."""

from __future__ import annotations

import math
from collections import deque
from fractions import Fraction
from pathlib import Path
from typing import TypedDict

_SOURCE_FILENAME_MAX_LENGTH = 40
# The download callback fires per 256 KiB chunk — about 60 times a second at
# 15 MiB/s — so a sample-count window spans milliseconds and the figure is
# unreadable. Average over wall-clock time instead.
_DOWNLOAD_RATE_WINDOW_SECONDS = 5.0
_DOWNLOAD_RATE_MINIMUM_SECONDS = 1.5
# progress() fires per read chunk — roughly 1000 times a second at 15 MiB/s with
# 16 KiB reads — so store at most one sample per interval. Capping by sample
# count instead would shrink the window below the minimum span and the rate
# would never be reported at all.
_DOWNLOAD_RATE_SAMPLE_INTERVAL_SECONDS = 0.05
_DOWNLOAD_RATE_MAX_SAMPLES = 256


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


class DownloadRateEstimator:
    """Estimate download speed from a short, timestamped sample window."""

    def __init__(self) -> None:
        self._samples: deque[tuple[float, int]] = deque(
            maxlen=_DOWNLOAD_RATE_MAX_SAMPLES
        )

    def update(self, completed: int, timestamp: float) -> float | None:
        """Return the average rate over the trailing wall-clock window."""
        if not math.isfinite(timestamp):
            return None
        if self._samples:
            previous_timestamp, previous_completed = self._samples[-1]
            if timestamp < previous_timestamp or completed < previous_completed:
                return None
        if (
            not self._samples
            or timestamp - self._samples[-1][0]
            >= _DOWNLOAD_RATE_SAMPLE_INTERVAL_SECONDS
        ):
            self._samples.append((timestamp, completed))
        cutoff = timestamp - _DOWNLOAD_RATE_WINDOW_SECONDS
        while len(self._samples) > 1 and self._samples[0][0] < cutoff:
            self._samples.popleft()
        first_timestamp, first_completed = self._samples[0]
        elapsed = timestamp - first_timestamp
        transferred = completed - first_completed
        if elapsed < _DOWNLOAD_RATE_MINIMUM_SECONDS or transferred <= 0:
            return None
        return transferred / elapsed


def format_download_speed(bytes_per_second: object) -> str:
    """Return a positive download rate using the source binary-size format."""
    if (
        not isinstance(bytes_per_second, (int, float))
        or isinstance(bytes_per_second, bool)
        or not math.isfinite(float(bytes_per_second))
        or bytes_per_second < 1
    ):
        return ""
    value = format_source_file_size(int(bytes_per_second))
    return f"{value}/s" if value else ""


def format_model_download_progress(completed: object, total: object) -> str:
    """Return human-readable transferred and total download sizes."""
    completed_size = format_source_file_size(completed)
    total_size = format_source_file_size(total)
    if not completed_size or not total_size:
        return ""
    return f"{completed_size} of {total_size}"


def format_model_download_detail(
    model_name: object,
    completed: object | None = None,
    total: object | None = None,
    bytes_per_second: object | None = None,
) -> str:
    """Return the model download detail shown below the current stage."""
    if not isinstance(model_name, str) or not model_name:
        return ""
    detail = f"Downloading {model_name}"
    progress = format_model_download_progress(completed, total)
    if progress:
        detail += f" — {progress}"
    speed = format_download_speed(bytes_per_second)
    if speed:
        detail += f" · {speed}"
    return detail


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
