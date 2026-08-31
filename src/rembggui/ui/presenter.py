"""Pure mapping from immutable state to visible shell properties."""

from __future__ import annotations

from rembggui.core.state import (
    AppState,
    ArtifactState,
    JobState,
    PreviewState,
    SourceState,
    capabilities,
)
from rembggui.ui.crop_presentation import present_crop
from rembggui.ui.parameter_presentation import present_parameters
from rembggui.ui.presentation_model import PresentationModel
from rembggui.ui.source_error_copy import source_error_copy
from rembggui.ui.source_presentation import present_source_metadata as s
from rembggui.ui.timeline_presentation import present_timeline


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
        message = "This video could not be read. Open another video."
    elif state.preview is PreviewState.ERROR:
        marker = "Preview failed"
        message = "Preview failed — retry Preview Frame"
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
        if (
            state.preview_attempt_error is not None
            and state.stale_category == "Preview failed"
        ):
            marker = "Preview failed — preview again"
        else:
            marker = "Settings changed — preview again"
        message = marker
    elif not state.model_available:
        message = "Download required"
    result_status_marker: str | None
    if state.edited_cuts and state.preview_result is not None:
        message = "Model preview — rebuild uses edited cut frames"
    result_accessible_description = message
    if state.preview is PreviewState.STALE and state.stale_category:
        result_accessible_description = f"{state.stale_category}: {message}"
    if state.edited_cuts and state.preview_result is not None:
        result_accessible_description = "Model preview — rebuild uses edited cut frames"
    if state.edited_cuts:
        result_status_marker = "Edited cuts changed"
    elif (
        state.preview is PreviewState.STALE
        and state.preview_attempt_error is not None
        and state.stale_category == "Preview failed"
    ):
        result_status_marker = marker
    elif marker is not None and state.stale_category is not None:
        result_status_marker = f"{marker} · {state.stale_category}"
    else:
        result_status_marker = marker
    result_status = (
        "error" if state.source is SourceState.ERROR else state.preview.value
    )
    result_checkerboard = state.preview in {
        PreviewState.CURRENT, PreviewState.STALE
    } or (state.preview is PreviewState.RUNNING and state.preview_result is not None)
    source_error_detail = str(state.source_error) if state.source_error else None
    source_error_message = None
    source_error_icon: str | None = None
    if state.source is SourceState.ERROR:
        source_error_message = source_error_copy(state.source_error)
        source_error_icon = "error"
    result_status_icon = (
        "preview"
        if state.preview is PreviewState.CURRENT
        else "stale"
        if state.preview is PreviewState.STALE
        else None
    )
    artifact = state.artifact_result
    artifact_path = str(artifact.output_path) if artifact else None
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
        show_stage=state.source is not SourceState.EMPTY,
        show_timeline=state.source in {SourceState.LOADING, SourceState.READY},
        show_success=state.artifact is ArtifactState.VALID,
        show_rebuild=state.edited_cuts,
        editor_locked=not allowed.can_edit,
        choose_enabled=allowed.can_choose_source,
        replace_enabled=allowed.can_replace_source,
        preview_enabled=allowed.can_preview,
        preview_label=(
            "Prepare & Preview"
            if not state.model_available
            else "Preview Frame"
        ),
        render_enabled=allowed.can_render,
        rebuild_enabled=allowed.can_rebuild,
        open_output_enabled=allowed.can_open_output,
        open_folder_enabled=allowed.can_open_folder,
        primary_action=primary,
        focus_target=allowed.focus_target,
        source_error_message=source_error_message,
        source_error_icon=source_error_icon,
        source_error_detail=source_error_detail,
        stale_category=state.stale_category,
        source_surface_visible=state.source in {SourceState.EMPTY, SourceState.ERROR},
        source_strip_visible=state.source in {SourceState.LOADING, SourceState.READY},
        source_error_visible=state.source is SourceState.ERROR,
        source_surface_heading=(
            "Open another video"
            if state.source is SourceState.ERROR
            else "Drop a video here"
        ),
        result_status=result_status,
        result_checkerboard=result_checkerboard,
        result_accessible_name="Background-removed result",
        result_accessible_description=result_accessible_description,
        result_status_marker=result_status_marker,
        result_status_icon=result_status_icon,
        recovery_visible=state.edited_cuts_error is not None,
        recovery_label=(
            "Retry Rebuild"
            if state.edited_cuts_error is not None
            else "Rebuild from edited cuts"
        ),
        artifact_path=artifact_path,
        success_label="Render complete",
        success_accessible_description=artifact_path or "Render complete",
        workspace_attention=state.edited_cuts or state.edited_cuts_error is not None,
        workspace_open=state.edited_cuts or state.edited_cuts_error is not None,
        **s(state.source_value if ready else None, state.source is SourceState.LOADING),
        source_frame=state.source_frame,
        crop=present_crop(state),
        crop_enabled=state.crop_enabled,
        parameters=present_parameters(state),
        timeline=present_timeline(state),
        result_frame=(
            state.preview_result.value if state.preview_result is not None else None
        ),
        source_loading=state.source is SourceState.LOADING,
        inspector_enabled=not active
        and state.source in {SourceState.EMPTY, SourceState.READY},
    )
