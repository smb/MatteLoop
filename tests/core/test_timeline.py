from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from rembggui.core.specs import CropSpec
from rembggui.core.state import (
    AppState,
    CropChanged,
    CropToggleChanged,
    PreviewRequested,
    PreviewResult,
    PreviewState,
    PreviewSucceeded,
    ResetCrop,
    SourceLoaded,
    SourceLoadRequested,
    reduce,
)
from rembggui.core.timeline import (
    EndChanged,
    PlayheadChanged,
    StartChanged,
    StepFrame,
    TimelineState,
    absolute_frame_number,
    format_timecode,
    timeline_from_metadata,
)


@dataclass(frozen=True)
class Metadata:
    duration: Fraction = Fraction(4)
    average_rate: Fraction = Fraction(30)
    path: Path = Path("source.mp4")
    width: int = 128
    height: int = 128


def test_timeline_defaults_to_full_source_with_exact_sampling_values() -> None:
    timeline = timeline_from_metadata(Metadata())

    assert timeline is not None
    assert timeline.start == Fraction(0)
    assert timeline.end == Fraction(4)
    assert timeline.playhead == Fraction(0)
    assert timeline.source_fps == Fraction(30)
    assert timeline.source_frame_interval == Fraction(1, 30)


def test_range_handles_clamp_without_crossing_or_losing_output_frame() -> None:
    timeline = TimelineState(
        Fraction(4), Fraction(1), Fraction(3), Fraction(2), source_fps=Fraction(30)
    )

    assert timeline.set_start(Fraction(4)) == TimelineState(
        Fraction(4),
        Fraction(3) - Fraction(1, 15),
        Fraction(3),
        Fraction(2),
        source_fps=Fraction(30),
    )
    assert timeline.set_end(Fraction(0)).end == Fraction(1) + Fraction(1, 15)


def test_frame_step_and_timecode_preserve_source_frame_cadence() -> None:
    timeline = TimelineState(
        Fraction(4), Fraction(0), Fraction(4), Fraction(1), source_fps=Fraction(30)
    )

    stepped = timeline.step(2)

    assert stepped.playhead == Fraction(16, 15)
    assert format_timecode(Fraction(16, 15)) == "0:01.067"
    assert absolute_frame_number(Fraction(16, 15), Fraction(30)) == 33


def test_playhead_clamps_to_the_last_decodable_frame_interval() -> None:
    timeline = TimelineState(
        Fraction(4), Fraction(0), Fraction(4), Fraction(0), source_fps=Fraction(30)
    )

    assert timeline.move_playhead(Fraction(4)).playhead == Fraction(119, 30)


def test_reset_range_restores_the_complete_source_interval() -> None:
    timeline = TimelineState(
        Fraction(4), Fraction(1), Fraction(3), Fraction(2), source_fps=Fraction(30)
    )

    reset = timeline.reset_range()

    assert (reset.start, reset.end) == (Fraction(0), Fraction(4))


def test_timecode_widens_to_hours_only_at_one_hour() -> None:
    assert format_timecode(Fraction(3599, 2)) == "29:59.500"
    assert format_timecode(Fraction(3600)) == "1:00:00.000"


def test_playhead_and_range_changes_stale_the_current_preview_by_category() -> None:
    metadata = Metadata()
    state = reduce(
        reduce(AppState(), SourceLoadRequested("source", "load")),
        SourceLoaded("source", "load", metadata),
    )
    running = reduce(state, PreviewRequested("job", "preview"))
    current = reduce(
        running,
        PreviewSucceeded("job", PreviewResult("source", "preview", "frame")),
    )

    moved = reduce(current, PlayheadChanged(Fraction(1)))
    ranged = reduce(moved, StartChanged(Fraction(1, 2)))

    assert moved.preview is PreviewState.STALE
    assert moved.stale_category == "Playhead"
    assert ranged.preview is PreviewState.STALE
    assert ranged.stale_category == "Playhead"

    current_again = reduce(current, EndChanged(Fraction(3)))
    assert current_again.preview is PreviewState.STALE
    assert current_again.stale_category == "Export range"


def test_crop_changes_stale_current_preview_and_keep_oriented_bounds() -> None:
    metadata = Metadata(width=128, height=64)
    state = reduce(
        reduce(AppState(), SourceLoadRequested("source", "load")),
        SourceLoaded("source", "load", metadata),
    )
    running = reduce(state, PreviewRequested("job", "preview"))
    current = reduce(
        running,
        PreviewSucceeded("job", PreviewResult("source", "preview", "frame")),
    )

    cropped = reduce(current, CropChanged(CropSpec(4, 5, 40, 30)))

    assert cropped.crop == CropSpec(4, 5, 40, 30)
    assert cropped.preview is PreviewState.STALE
    assert cropped.stale_category == "Crop"


def test_reset_crop_restores_the_oriented_source_frame() -> None:
    metadata = Metadata(width=128, height=64)
    loaded = reduce(
        reduce(AppState(), SourceLoadRequested("source", "load")),
        SourceLoaded("source", "load", metadata),
    )
    cropped = reduce(loaded, CropChanged(CropSpec(4, 5, 40, 30)))

    reset = reduce(cropped, ResetCrop())

    assert reset.crop == CropSpec(0, 0, 128, 64)


def test_crop_overlay_visibility_is_reducer_owned_without_staling_preview() -> None:
    metadata = Metadata()
    loaded = reduce(
        reduce(AppState(), SourceLoadRequested("source", "load")),
        SourceLoaded("source", "load", metadata),
    )

    hidden = reduce(loaded, CropToggleChanged(False))

    assert hidden.crop_enabled is False
    assert hidden.preview is PreviewState.NONE


def test_timeline_reducer_preserves_identity_when_a_frame_step_is_a_noop() -> None:
    timeline = TimelineState(
        Fraction(4), Fraction(0), Fraction(4), Fraction(0), source_fps=Fraction(30)
    )
    state = AppState(timeline=timeline)

    assert reduce(state, StepFrame(0)) is state
