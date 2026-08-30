"""Pure mapping from immutable state to visible shell properties."""

from __future__ import annotations

from dataclasses import dataclass

from rembggui.core.state import (
    AppState,
    ArtifactState,
    FocusTarget,
    JobState,
    PreviewState,
    SourceState,
    capabilities,
)


@dataclass(frozen=True)
class PresentationModel:
    source_mode: str
    result_message: str
    result_marker: str | None
    show_stage: bool
    show_timeline: bool
    show_success: bool
    show_rebuild: bool
    editor_locked: bool
    choose_enabled: bool
    replace_enabled: bool
    preview_enabled: bool
    preview_label: str
    render_enabled: bool
    rebuild_enabled: bool
    open_output_enabled: bool
    open_folder_enabled: bool
    primary_action: str | None
    focus_target: FocusTarget
    source_error_message: str | None
    stale_category: str | None


def present(state: AppState) -> PresentationModel:
    """Present reducer state without importing Qt, jobs, or runtime services."""
    allowed = capabilities(state)
    active = state.job.phase is not JobState.IDLE
    ready = state.source is SourceState.READY
    source_mode = state.source.value
    marker: str | None = None
    message = "Preview this frame to inspect the cutout"
    if state.source is SourceState.LOADING:
        message = "Reading video…"
    elif state.source is SourceState.ERROR:
        message = "Choose another video"
    elif not state.model_available:
        message = "Download required"
    elif state.preview is PreviewState.RUNNING:
        message = (
            "Current preview — previewing selected frame"
            if state.preview_result is not None
            else "Previewing selected frame"
        )
    elif state.preview is PreviewState.CURRENT:
        marker = "Current preview"
        message = "Current preview"
    elif state.preview is PreviewState.STALE:
        marker = "Settings changed — preview again"
        message = marker
    elif state.preview is PreviewState.ERROR:
        marker = "Preview failed"
        message = "Preview failed — retry Preview Frame"

    if active or not ready:
        primary: str | None = None
    elif state.preview is PreviewState.CURRENT and allowed.can_render:
        primary = "render"
    elif allowed.can_preview:
        primary = "preview"
    elif allowed.can_render:
        primary = "render"
    else:
        primary = None

    return PresentationModel(
        source_mode=source_mode,
        result_message=message,
        result_marker=marker,
        show_stage=state.source in {SourceState.LOADING, SourceState.READY},
        show_timeline=state.source in {SourceState.LOADING, SourceState.READY},
        show_success=state.artifact is ArtifactState.VALID,
        show_rebuild=state.edited_cuts,
        editor_locked=not allowed.can_edit,
        choose_enabled=allowed.can_choose_source,
        replace_enabled=allowed.can_replace_source,
        preview_enabled=allowed.can_preview,
        preview_label=(
            "Prepare & Preview"
            if ready and not state.model_available
            else "Preview Frame"
        ),
        render_enabled=allowed.can_render,
        rebuild_enabled=allowed.can_rebuild,
        open_output_enabled=allowed.can_open_output,
        open_folder_enabled=allowed.can_open_folder,
        primary_action=primary,
        focus_target=allowed.focus_target,
        source_error_message=(str(state.source_error) if state.source_error else None),
        stale_category=state.stale_category,
    )
