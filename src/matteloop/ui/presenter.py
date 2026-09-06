"""Pure mapping from immutable state to visible shell properties."""

from __future__ import annotations

from matteloop.core.state import (
    AppState,
    ArtifactState,
    JobState,
    PreviewState,
    SourceState,
    capabilities,
)
from matteloop.core.tokens import PreviewInvalidationReason
from matteloop.ui.crop_presentation import present_crop
from matteloop.ui.parameter_presentation import present_parameters
from matteloop.ui.presentation_model import (
    PresentationModel,
    preview_invalidation_copy,
)
from matteloop.ui.source_error_copy import source_error_copy
from matteloop.ui.source_presentation import present_source_metadata as s
from matteloop.ui.timeline_presentation import present_timeline

Preview, Source = PreviewState, SourceState


def _failure_message(error: object | None, retry: str) -> str:
    """Say what actually failed, not just that something did."""
    detail = getattr(error, "technical_detail", None)
    if isinstance(detail, str) and detail:
        return f"Preview failed: {detail}"
    return f"Preview failed — {retry}"


def present(state: AppState) -> PresentationModel:
    """Present reducer state without importing Qt, jobs, or runtime services."""
    allowed = capabilities(state)
    stale_category = preview_invalidation_copy(state.stale_category)
    active = state.job.phase is not JobState.IDLE
    ready = state.source is Source.READY
    source_mode = state.source.value
    marker: str | None = None
    message = "Preview this frame to inspect the cutout"
    if state.source is Source.LOADING:
        message = "Reading video…"
    elif state.source is Source.ERROR:
        message = "This video could not be read. Open another video."
    elif state.preview is Preview.ERROR:
        marker = "Preview failed"
        message = _failure_message(state.preview_error, "retry Preview Frame")
    elif state.preview is Preview.RUNNING:
        message = (
            "Current preview — previewing selected frame"
            if state.preview_result is not None
            else "Previewing selected frame"
        )
    elif state.preview is Preview.CURRENT:
        marker = "Current preview"
        message = "Current preview"
    elif state.preview is Preview.STALE:
        if (
            state.preview_attempt_error is not None
            and state.stale_category is PreviewInvalidationReason.PREVIEW_FAILED
        ):
            marker = _failure_message(state.preview_attempt_error, "preview again")
        else:
            marker = "Settings changed — preview again"
        message = marker
    elif not state.model_available:
        message = "Download required"
    result_status_marker: str | None
    if state.edited_cuts and state.preview_result is not None:
        message = "Model preview — rebuild uses edited cut frames"
    result_accessible_description = message
    if state.preview is Preview.STALE and stale_category:
        result_accessible_description = f"{stale_category}: {message}"
    if state.edited_cuts and state.preview_result is not None:
        result_accessible_description = "Model preview — rebuild uses edited cut frames"
    if state.edited_cuts:
        result_status_marker = "Edited cuts changed"
    elif (
        state.preview is Preview.STALE
        and state.preview_attempt_error is not None
        and state.stale_category is PreviewInvalidationReason.PREVIEW_FAILED
    ):
        result_status_marker = marker
    elif marker is not None and stale_category is not None:
        result_status_marker = f"{marker} · {stale_category}"
    else:
        result_status_marker = marker
    result_status = "error" if state.source is Source.ERROR else state.preview.value
    result_checkerboard = state.preview in {Preview.CURRENT, Preview.STALE} or (
        state.preview is Preview.RUNNING and state.preview_result is not None
    )
    source_error_detail = str(state.source_error) if state.source_error else None
    source_error_message = None
    source_error_icon: str | None = None
    if state.source is Source.ERROR:
        source_error_message = source_error_copy(state.source_error)
        source_error_icon = "error"
    result_status_icon = {"current": "preview", "stale": "stale"}.get(result_status)
    artifact = state.artifact_result
    artifact_path = str(artifact.output_path) if artifact else None
    if active or not ready:
        primary: str | None = None
    elif state.preview is Preview.CURRENT and allowed.can_render:
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
        show_stage=state.source is not Source.EMPTY,
        show_timeline=state.source in {Source.LOADING, Source.READY},
        show_success=state.artifact is ArtifactState.VALID,
        show_rebuild=state.edited_cuts,
        editor_locked=not allowed.can_edit,
        choose_enabled=allowed.can_choose_source,
        replace_enabled=allowed.can_replace_source,
        preview_enabled=allowed.can_preview,
        model_available=state.model_available,
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
        stale_category=stale_category,
        source_surface_visible=state.source in {Source.EMPTY, Source.ERROR},
        source_strip_visible=state.source in {Source.LOADING, Source.READY},
        source_error_visible=state.source is Source.ERROR,
        source_surface_heading=(
            "Open another video"
            if state.source is Source.ERROR
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
        **s(state.source_value if ready else None, state.source is Source.LOADING),
        source_frame=state.source_frame,
        crop=present_crop(state),
        crop_enabled=state.crop_enabled,
        parameters=present_parameters(state),
        timeline=present_timeline(state),
        result_frame=(
            state.preview_result.value if state.preview_result is not None else None
        ),
        source_loading=state.source is Source.LOADING,
        inspector_enabled=not active
        and state.source in {Source.EMPTY, Source.READY},
    )
