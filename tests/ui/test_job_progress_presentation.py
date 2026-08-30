from __future__ import annotations

from rembggui.ui.source_presentation import (
    JobProgressPresenter,
    format_elapsed,
    format_frame_rate,
)


def test_elapsed_time_uses_minute_and_hour_timecode_shapes() -> None:
    assert format_elapsed(0) == "0:00"
    assert format_elapsed(65.9) == "1:05"
    assert format_elapsed(3665.9) == "1:01:05"


def test_frame_rate_and_estimate_wait_for_a_meaningful_trailing_window() -> None:
    presenter = JobProgressPresenter()
    assert presenter.reset(0.0).rate == ""

    assert presenter.update(0, 40, 0.0).rate == ""
    assert presenter.update(10, 40, 1.0).rate == ""

    metrics = presenter.update(25, 40, 2.0)

    assert metrics.rate == "12.5 fps"
    assert metrics.estimate == "0:01 remaining"


def test_indeterminate_stage_drops_stale_estimate_but_keeps_real_rate() -> None:
    presenter = JobProgressPresenter()
    presenter.reset(0.0)
    presenter.update(0, 40, 0.0)
    presenter.update(20, 40, 2.0)

    metrics = presenter.update(None, None, 3.0)

    assert metrics.rate == "10.0 fps"
    assert metrics.estimate == ""


def test_frame_rate_formatter_accepts_slow_work() -> None:
    assert format_frame_rate(0.5) == "0.5 fps"
