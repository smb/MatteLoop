"""Immutable timeline values passed from the presenter to the Qt widget."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from matteloop.core.state import AppState
from matteloop.core.timeline import TimelineState


@dataclass(frozen=True, slots=True)
class TimelinePresentation:
    state: TimelineState
    source_id: str
    source: Path
    width: int
    height: int
    source_revision: object | None
    validation_proof: object | None


def present_timeline(state: AppState) -> TimelinePresentation | None:
    """Build the timeline view values without exposing reducer internals to Qt."""
    if state.timeline is None or state.source_id is None:
        return None
    metadata = state.source_value
    source = getattr(metadata, "path", None)
    width = getattr(metadata, "width", None)
    height = getattr(metadata, "height", None)
    if (
        not isinstance(source, Path)
        or type(width) is not int
        or type(height) is not int
    ):
        return None
    return TimelinePresentation(
        state.timeline,
        state.source_id,
        source,
        width,
        height,
        getattr(metadata, "revision", None),
        getattr(metadata, "validation_proof", None),
    )
