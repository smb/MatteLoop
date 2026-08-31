from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

from matteloop.core.state import AppState, SourceLoaded, SourceLoadRequested, reduce
from matteloop.ui.presenter import present
from matteloop.ui.source_presentation import (
    DownloadRateEstimator,
    format_download_speed,
    format_model_download_detail,
    format_model_download_progress,
)


def _ready(metadata: object) -> AppState:
    return reduce(
        reduce(AppState(), SourceLoadRequested("source", "load")),
        SourceLoaded("source", "load", metadata),
    )


def test_present_formats_fraction_duration_as_millisecond_timecode() -> None:
    model = present(_ready(SimpleNamespace(duration=Fraction(13, 5))))

    assert model.source_duration == "0:02.600"


def test_present_formats_hour_long_duration_with_hours() -> None:
    model = present(_ready(SimpleNamespace(duration=Fraction(7323, 2))))

    assert model.source_duration == "1:01:01.500"


def test_present_formats_fraction_frame_rate_to_two_decimal_places() -> None:
    model = present(_ready(SimpleNamespace(average_rate=Fraction(30000, 1001))))

    assert model.source_frame_rate == "29.97 fps"


def test_present_formats_integer_frame_rate_without_trailing_zeroes() -> None:
    model = present(_ready(SimpleNamespace(average_rate=Fraction(30))))

    assert model.source_frame_rate == "30 fps"


def test_present_middle_elides_long_filename_while_preserving_extension() -> None:
    path = Path("/tmp/matteloop-fixtures/video/" + "source-" + "x" * 60 + ".mp4")
    model = present(_ready(SimpleNamespace(path=path)))

    assert model.source_filename.endswith(".mp4")
    assert "…" in model.source_filename
    assert "/" not in model.source_filename
    assert model.source_path == str(path)


def test_present_formats_dimensions_and_binary_file_size() -> None:
    model = present(
        _ready(
            SimpleNamespace(
                width=1280,
                height=240,
                file_size=1_572_864,
            )
        )
    )

    assert model.source_dimensions == "1280 × 240"
    assert model.source_file_size == "1.5 MiB"


def test_present_leaves_missing_source_metadata_values_blank() -> None:
    model = present(AppState())
    incomplete = present(
        _ready(
            SimpleNamespace(
                path=None,
                width=None,
                height=None,
                duration=None,
                average_rate=None,
                peak_rate=None,
                file_size=None,
            )
        )
    )

    assert (
        model.source_filename,
        model.source_dimensions,
        model.source_duration,
        model.source_frame_rate,
        model.source_file_size,
        model.source_path,
    ) == ("", "", "", "", "", None)
    assert (
        incomplete.source_filename,
        incomplete.source_dimensions,
        incomplete.source_duration,
        incomplete.source_frame_rate,
        incomplete.source_file_size,
        incomplete.source_path,
    ) == ("", "", "", "", "", None)


def test_model_download_detail_names_model_and_formats_human_progress() -> None:
    detail = format_model_download_detail(
        "BiRefNet Portrait",
        int(412.3 * 1024**2),
        int(927.6 * 1024**2),
        int(12.4 * 1024**2),
    )

    assert detail == (
        "Downloading BiRefNet Portrait — 412.3 MiB of 927.6 MiB · 12.4 MiB/s"
    )
    assert format_model_download_progress(
        int(412.3 * 1024**2), int(927.6 * 1024**2)
    ) == "412.3 MiB of 927.6 MiB"


def test_download_speed_is_absent_until_the_rate_has_enough_samples() -> None:
    estimator = DownloadRateEstimator()

    assert estimator.update(0, 0.0) is None
    assert estimator.update(10 * 1024**2, 1.0) is None
    assert "MiB/s" not in format_model_download_detail(
        "BiRefNet Portrait", 10 * 1024**2, 100 * 1024**2, None
    )


def test_download_speed_uses_a_smoothed_recent_rate() -> None:
    estimator = DownloadRateEstimator()

    estimator.update(0, 0.0)
    estimator.update(10 * 1024**2, 1.0)
    speed = estimator.update(30 * 1024**2, 2.0)

    assert speed is not None
    assert format_download_speed(speed) == "15.0 MiB/s"


def test_download_speed_averages_only_the_trailing_window() -> None:
    """A slowdown must take over the figure once it fills the window."""
    estimator = DownloadRateEstimator()
    completed = 0
    timestamp = 0.0
    fast = 20 * 1024**2
    slow = 5 * 1024**2

    for _ in range(120):  # 6 s at 20 MiB/s, 60 callbacks per second
        timestamp += 1 / 20
        completed += fast // 20
        speed = estimator.update(completed, timestamp)
    assert speed is not None
    assert format_download_speed(speed) == "20.0 MiB/s"

    for _ in range(120):  # 6 s at 5 MiB/s — long enough to age the fast samples out
        timestamp += 1 / 20
        completed += slow // 20
        speed = estimator.update(completed, timestamp)
    assert speed is not None
    assert format_download_speed(speed) == "5.0 MiB/s"


def test_download_speed_survives_a_single_stalled_callback() -> None:
    """One slow chunk must not collapse the figure the way an instant rate would."""
    estimator = DownloadRateEstimator()
    completed = 0
    timestamp = 0.0
    for _ in range(60):
        timestamp += 1 / 20
        completed += (20 * 1024**2) // 20
        estimator.update(completed, timestamp)

    timestamp += 0.5  # a stall: half a second for a single small chunk
    completed += 1024
    speed = estimator.update(completed, timestamp)

    assert speed is not None
    assert speed > 15 * 1024**2


def test_download_speed_appears_at_the_real_callback_rate() -> None:
    """progress() fires ~1000x/s per 16 KiB read; the rate must still be reported."""
    estimator = DownloadRateEstimator()
    chunk = 16 * 1024
    per_second = (15 * 1024**2) // chunk
    completed = 0
    timestamp = 0.0
    speed = None

    for _ in range(per_second * 3):  # three seconds of real-world callbacks
        timestamp += 1 / per_second
        completed += chunk
        speed = estimator.update(completed, timestamp)

    assert speed is not None, "speed never became available at the real callback rate"
    assert format_download_speed(speed) == "15.0 MiB/s"
