from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from matteloop.core.state import (
    AppState,
    DurationChanged,
    PreviewInvalidationReason,
    PreviewRequested,
    PreviewResult,
    PreviewState,
    PreviewSucceeded,
    SourceLoaded,
    SourceLoadRequested,
    reduce,
)


@dataclass(frozen=True)
class Metadata:
    path: Path = Path("source.mp4")
    width: int = 128
    height: int = 128
    duration: Fraction = Fraction(4)
    average_rate: Fraction = Fraction(30)


def _current() -> AppState:
    loading = reduce(AppState(), SourceLoadRequested("source", "load"))
    ready = reduce(loading, SourceLoaded("source", "load", Metadata()))
    running = reduce(ready, PreviewRequested("job", "preview"))
    return reduce(
        running,
        PreviewSucceeded("job", PreviewResult("source", "preview", "cutout")),
    )


def test_editing_duration_moves_end_without_moving_start() -> None:
    state = _current()
    state = reduce(state, DurationChanged(Fraction(3, 2)))

    assert state.timeline is not None
    assert state.timeline.start == Fraction(0)
    assert state.timeline.end == Fraction(3, 2)
    assert state.preview is PreviewState.STALE
    assert state.stale_category is PreviewInvalidationReason.EXPORT_RANGE


def test_duration_edit_stops_at_source_end() -> None:
    state = _current()
    state = reduce(state, DurationChanged(Fraction(10)))

    assert state.timeline is not None
    assert state.timeline.end == Fraction(4)
