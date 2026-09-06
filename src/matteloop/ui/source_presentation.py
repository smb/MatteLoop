"""Pure formatting for source metadata shown by the Qt shell."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import TypedDict

from PySide6.QtCore import QCoreApplication

from matteloop.core.timeline import format_timecode
from matteloop.ui.i18n import display_locale

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


@dataclass(frozen=True, slots=True)
class JobProgressMetrics:
    """Qt-free text for the changing metrics in the job dialog."""

    elapsed: str
    rate: str
    estimate: str


class JobProgressPresenter:
    """Present elapsed time and smoothed frame metrics for one job."""

    def __init__(self) -> None:
        self._started_at = 0.0
        self._rate_estimator = DownloadRateEstimator()
        self._rate: float | None = None
        self._overall_completed: int | None = None
        self._overall_total: int | None = None

    def reset(self, started_at: float) -> JobProgressMetrics:
        if not math.isfinite(started_at):
            raise ValueError("started_at must be finite")
        self._started_at = started_at
        self._rate_estimator = DownloadRateEstimator()
        self._rate = None
        self._overall_completed = None
        self._overall_total = None
        return self.current(started_at)

    def update(
        self,
        overall_completed: object,
        overall_total: object,
        timestamp: float,
    ) -> JobProgressMetrics:
        if (
            isinstance(overall_completed, int)
            and not isinstance(overall_completed, bool)
            and isinstance(overall_total, int)
            and not isinstance(overall_total, bool)
            and 0 <= overall_completed <= overall_total
        ):
            rate = self._rate_estimator.update(overall_completed, timestamp)
            if rate is not None:
                self._rate = rate
            self._overall_completed = overall_completed
            self._overall_total = overall_total
        else:
            self._overall_completed = None
            self._overall_total = None
        return self.current(timestamp)

    def current(self, timestamp: float) -> JobProgressMetrics:
        if not math.isfinite(timestamp):
            timestamp = self._started_at
        elapsed = max(0.0, timestamp - self._started_at)
        estimate = ""
        if (
            self._rate is not None
            and self._overall_completed is not None
            and self._overall_total is not None
        ):
            remaining = self._overall_total - self._overall_completed
            estimate = QCoreApplication.translate(
                "SourcePresentation", "%s remaining"
            ) % format_elapsed(remaining / self._rate)
        return JobProgressMetrics(
            format_elapsed(elapsed),
            format_frame_rate(self._rate),
            estimate,
        )


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
    """Return a source duration using the shared millisecond timecode format."""
    if not isinstance(duration, Fraction) or duration < 0:
        return ""
    return _localized_timecode(duration)


def format_source_frame_rate(rate: object) -> str:
    """Return a source frame rate rounded to two decimal places."""
    if not isinstance(rate, Fraction) or rate <= 0:
        return ""
    hundredths = _rounded_hundredths(rate)
    whole, decimal = divmod(hundredths, 100)
    value = _localized_decimal(whole, decimal, 2)
    return QCoreApplication.translate("SourcePresentation", "%s fps") % value


def format_source_file_size(size: object) -> str:
    """Return a source file size using compact binary units."""
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return ""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{display_locale().toString(value, 'f', 1)} {unit}"
        value /= 1024
    return ""


def format_elapsed(seconds: object) -> str:
    """Return whole elapsed seconds using the shared timecode formatter."""
    if (
        not isinstance(seconds, (int, float))
        or isinstance(seconds, bool)
        or not math.isfinite(float(seconds))
        or seconds < 0
    ):
        return ""
    return _localized_timecode(Fraction(math.floor(float(seconds)))).removesuffix(
        display_locale().decimalPoint() + "000"
    )


def format_frame_rate(frames_per_second: object) -> str:
    """Return a readable positive frame throughput."""
    if (
        not isinstance(frames_per_second, (int, float))
        or isinstance(frames_per_second, bool)
        or not math.isfinite(float(frames_per_second))
        or frames_per_second <= 0
    ):
        return ""
    value = display_locale().toString(float(frames_per_second), "f", 1)
    return QCoreApplication.translate("SourcePresentation", "%s fps") % value


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
    return (
        QCoreApplication.translate("SourcePresentation", "%s/s") % value
        if value
        else ""
    )


def format_model_download_progress(completed: object, total: object) -> str:
    """Return human-readable transferred and total download sizes."""
    completed_size = format_source_file_size(completed)
    total_size = format_source_file_size(total)
    if not completed_size or not total_size:
        return ""
    return QCoreApplication.translate("SourcePresentation", "%s of %s") % (
        completed_size,
        total_size,
    )


def format_model_download_detail(
    model_name: object,
    completed: object | None = None,
    total: object | None = None,
    bytes_per_second: object | None = None,
) -> str:
    """Return the model download detail shown below the current stage."""
    if not isinstance(model_name, str) or not model_name:
        return ""
    detail = QCoreApplication.translate("SourcePresentation", "Downloading %s") % (
        model_name
    )
    progress = format_model_download_progress(completed, total)
    if progress:
        detail += QCoreApplication.translate("SourcePresentation", " — %s") % progress
    speed = format_download_speed(bytes_per_second)
    if speed:
        detail += QCoreApplication.translate("SourcePresentation", " · %s") % speed
    return detail


def present_source_metadata(
    metadata: object | None, loading: bool = False
) -> SourcePresentation:
    """Return source-strip text and the unshortened path for accessibility."""
    if metadata is None:
        return {
            "source_filename": (
                QCoreApplication.translate("SourcePresentation", "Reading video…")
                if loading
                else ""
            ),
            "source_dimensions": "",
            "source_duration": "",
            "source_frame_rate": "",
            "source_file_size": "",
            "source_path": None,
        }
    path = getattr(metadata, "path", None)
    full_path = str(path) if path is not None else None
    average_rate = getattr(metadata, "average_rate", None)
    rate = average_rate or getattr(metadata, "sustained_rate", None)
    return {
        "source_filename": format_source_filename(path),
        "source_dimensions": format_source_dimensions(
            getattr(metadata, "width", None), getattr(metadata, "height", None)
        ),
        "source_duration": format_source_duration(getattr(metadata, "duration", None)),
        "source_frame_rate": format_source_frame_rate(rate),
        "source_file_size": format_source_file_size(
            getattr(metadata, "file_size", None)
        ),
        "source_path": full_path,
    }


def _rounded_hundredths(value: Fraction) -> int:
    rounded = value * 100 + Fraction(1, 2)
    return rounded.numerator // rounded.denominator


def _localized_decimal(whole: int, fraction: int, places: int) -> str:
    value = f"{whole}.{fraction:0{places}d}"
    decimal_point = display_locale().decimalPoint()
    return value.replace(".", decimal_point).rstrip("0").rstrip(decimal_point)


def _localized_timecode(value: Fraction) -> str:
    return format_timecode(value).replace(".", display_locale().decimalPoint())


def format_localized_timecode(value: Fraction) -> str:
    """Format a timeline value using the selected interface locale."""
    return _localized_timecode(value)
