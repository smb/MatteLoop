"""Reducer adapter for timeline events, kept separate from the state model."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from matteloop.core.timeline import (
    SourceFrameDecoded,
    TimelineEvent,
    update_timeline,
)

if TYPE_CHECKING:
    from matteloop.core.state import AppState


def reduce_timeline(
    state: AppState, event: TimelineEvent | SourceFrameDecoded
) -> AppState:
    """Apply an editable timeline event and reuse the normal stale path."""
    from matteloop.core.state import (
        PreviewInvalidated,
        SourceState,
        capabilities,
        reduce,
    )

    if isinstance(event, SourceFrameDecoded):
        if (
            state.source is not SourceState.READY
            or state.source_id != event.source_id
            or state.timeline is None
            or state.timeline.generation != event.generation
        ):
            return state
        return replace(state, source_frame=event.frame)
    timeline = state.timeline
    if timeline is None or not capabilities(state).can_edit:
        return state
    update = update_timeline(timeline, event)
    if update is None:
        return state
    updated, category = update
    return reduce(replace(state, timeline=updated), PreviewInvalidated(category))
