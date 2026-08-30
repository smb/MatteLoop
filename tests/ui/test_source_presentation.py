from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

from rembggui.core.state import AppState, SourceLoaded, SourceLoadRequested, reduce
from rembggui.ui.presenter import present


def _ready(metadata: object) -> AppState:
    return reduce(
        reduce(AppState(), SourceLoadRequested("source", "load")),
        SourceLoaded("source", "load", metadata),
    )


def test_present_formats_fraction_duration_as_centisecond_timecode() -> None:
    model = present(_ready(SimpleNamespace(duration=Fraction(13, 5))))

    assert model.source_duration == "0:02.60"


def test_present_formats_hour_long_duration_with_hours() -> None:
    model = present(_ready(SimpleNamespace(duration=Fraction(7323, 2))))

    assert model.source_duration == "1:01:01.50"


def test_present_formats_fraction_frame_rate_to_two_decimal_places() -> None:
    model = present(_ready(SimpleNamespace(average_rate=Fraction(30000, 1001))))

    assert model.source_frame_rate == "29.97 fps"


def test_present_formats_integer_frame_rate_without_trailing_zeroes() -> None:
    model = present(_ready(SimpleNamespace(average_rate=Fraction(30))))

    assert model.source_frame_rate == "30 fps"


def test_present_middle_elides_long_filename_while_preserving_extension() -> None:
    path = Path("/Users/sb/private/video/" + "source-" + "x" * 60 + ".mp4")
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
