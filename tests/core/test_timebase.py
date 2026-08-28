from __future__ import annotations

from fractions import Fraction

import pytest

from rembggui.core.errors import ErrorCode, ValidationError
from rembggui.core.timebase import sample_times, webp_delays


def test_sampling_is_half_open_and_exact() -> None:
    assert sample_times(Fraction(0), Fraction(1, 10), 30) == (
        Fraction(0),
        Fraction(1, 30),
        Fraction(1, 15),
    )


def test_sampling_includes_an_exact_start_but_excludes_an_exact_end() -> None:
    assert sample_times(Fraction(1, 5), Fraction(7, 10), 4) == (
        Fraction(1, 5),
        Fraction(9, 20),
    )


def test_high_rate_sampling_retains_duplicate_source_selections() -> None:
    sample_grid = sample_times(Fraction(0), Fraction(1, 20), 240)

    source_display_intervals = tuple(
        timestamp // Fraction(1, 24) for timestamp in sample_grid
    )

    assert len(sample_grid) == 12
    assert source_display_intervals == (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1)


@pytest.mark.parametrize(
    ("start", "end", "fps"),
    [
        (Fraction(-1), Fraction(1), 30),
        (Fraction(1), Fraction(1), 30),
        (Fraction(2), Fraction(1), 30),
        (Fraction(0), Fraction(1), 0),
        (Fraction(0), Fraction(1), 241),
        (Fraction(0), Fraction(1), True),
    ],
)
def test_sampling_rejects_invalid_ranges_and_fps(
    start: Fraction, end: Fraction, fps: int
) -> None:
    with pytest.raises(ValidationError) as exc:
        sample_times(start, end, fps)

    assert exc.value.code is ErrorCode.INVALID_SAMPLING


def test_sampling_rejects_non_fraction_boundaries() -> None:
    with pytest.raises(ValidationError) as exc:
        sample_times(0, Fraction(1), 30)  # type: ignore[arg-type]

    assert exc.value.code is ErrorCode.INVALID_SAMPLING


def test_sampling_accepts_exactly_one_hundred_thousand_output_frames() -> None:
    timestamps = sample_times(Fraction(0), Fraction(100_000), 1)

    assert len(timestamps) == 100_000
    assert timestamps[0] == Fraction(0)
    assert timestamps[-1] == Fraction(99_999)


def test_sampling_rejects_more_than_one_hundred_thousand_output_frames() -> None:
    with pytest.raises(ValidationError) as exc:
        sample_times(Fraction(0), Fraction(100_001), 1)

    assert exc.value.code is ErrorCode.INVALID_SAMPLING
    assert exc.value.stage == "timebase"
    assert exc.value.technical_detail == (
        "output frame count must not exceed 100000"
    )


def test_sixty_fps_delays_distribute_rounding() -> None:
    delays = webp_delays(6, 60)

    assert delays == (17, 16, 17, 17, 16, 17)
    assert sum(delays) == 100


def test_single_frame_and_half_millisecond_ties_round_without_floats() -> None:
    assert webp_delays(1, 240) == (4,)
    assert webp_delays(2, 16) == (63, 62)


def test_cumulative_delays_stay_within_half_a_millisecond() -> None:
    delays = webp_delays(997, 240)

    encoded_milliseconds = sum(delays)
    exact_milliseconds = Fraction(997_000, 240)
    assert abs(Fraction(encoded_milliseconds) - exact_milliseconds) <= Fraction(1, 2)


def test_webp_delays_accepts_exactly_one_hundred_thousand_frames() -> None:
    delays = webp_delays(100_000, 1)

    assert len(delays) == 100_000
    assert delays[0] == 1000
    assert delays[-1] == 1000


def test_webp_delays_rejects_more_than_one_hundred_thousand_frames() -> None:
    with pytest.raises(ValidationError) as exc:
        webp_delays(100_001, 1)

    assert exc.value.code is ErrorCode.INVALID_SAMPLING
    assert exc.value.stage == "timebase"
    assert exc.value.technical_detail == (
        "frame_count must be an integer between 1 and 100000"
    )


@pytest.mark.parametrize(
    ("frame_count", "fps"),
    [(0, 30), (-1, 30), (True, 30), (1, 0), (1, 241), (1, True)],
)
def test_webp_delays_reject_invalid_frame_counts_and_fps(
    frame_count: int, fps: int
) -> None:
    with pytest.raises(ValidationError) as exc:
        webp_delays(frame_count, fps)

    assert exc.value.code is ErrorCode.INVALID_SAMPLING
