"""Pure immutable application state, reducer events, and derived capabilities."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum


class SourceState(StrEnum):
    EMPTY = "empty"
    LOADING = "loading"
    READY = "ready"
    ERROR = "error"


class PreviewState(StrEnum):
    NONE = "none"
    RUNNING = "running"
    CURRENT = "current"
    STALE = "stale"
    ERROR = "error"


class JobState(StrEnum):
    IDLE = "idle"
    PREPARING_MODEL = "preparing_model"
    PREVIEWING = "previewing"
    RENDERING = "rendering"
    CANCELLING = "cancelling"


class ArtifactState(StrEnum):
    NONE = "none"
    VALID = "valid"
    ERROR = "error"


class JobKind(StrEnum):
    PREVIEW = "preview"
    RENDER = "render"
    REBUILD = "rebuild"


class FocusTarget(StrEnum):
    NONE = "none"
    CHOOSE_VIDEO = "choose_video"
    SOURCE_ERROR_HEADING = "source_error_heading"
    PREVIEW_ACTION = "preview_action"
    RENDER_ACTION = "render_action"
    REBUILD_ACTION = "rebuild_action"
    RESULT_CANVAS = "result_canvas"
    JOB_DIALOG = "job_dialog"
    PREFLIGHT_DIALOG = "preflight_dialog"
    SUCCESS_BANNER = "success_banner"
    EDITED_CUT_RECOVERY = "edited_cut_recovery"


@dataclass(frozen=True)
class PreviewResult:
    """A preview payload bound to the source and request that produced it."""

    source_id: str
    request_id: str
    value: object


@dataclass(frozen=True)
class ArtifactResult:
    """A rendered artifact bound to the source and request that produced it."""

    source_id: str
    request_id: str
    value: object


@dataclass(frozen=True)
class ActiveJob:
    """Exclusive job identity retained unchanged while cancellation is pending."""

    job_id: str | None = None
    kind: JobKind | None = None
    phase: JobState = JobState.IDLE
    stage: str = ""
    initiator_focus: FocusTarget = FocusTarget.NONE


@dataclass(frozen=True)
class AppState:
    source: SourceState = SourceState.EMPTY
    source_id: str | None = None
    source_value: object | None = None
    source_error: object | None = None
    model_available: bool = True
    model_supports_render: bool = True
    preview: PreviewState = PreviewState.NONE
    preview_result: PreviewResult | None = None
    preview_request_id: str | None = None
    preview_error: object | None = None
    stale_category: str | None = None
    preview_before_job: PreviewState = PreviewState.NONE
    preview_request_before_job: str | None = None
    job: ActiveJob = field(default_factory=ActiveJob)
    job_request_id: str | None = None
    artifact: ArtifactState = ArtifactState.NONE
    artifact_result: ArtifactResult | None = None
    artifact_error: object | None = None
    preflight_warning: bool = False
    edited_cuts: bool = False
    edited_cuts_error: object | None = None
    focus_target: FocusTarget = FocusTarget.CHOOSE_VIDEO


@dataclass(frozen=True)
class Capabilities:
    can_choose_source: bool
    can_replace_source: bool
    can_edit: bool
    can_preview: bool
    can_render: bool
    can_rebuild: bool
    can_cancel: bool
    can_open_output: bool
    can_open_folder: bool
    focus_target: FocusTarget


@dataclass(frozen=True)
class SourceLoadRequested:
    source_id: str


@dataclass(frozen=True)
class SourceLoaded:
    source_id: str
    value: object


@dataclass(frozen=True)
class SourceLoadFailed:
    source_id: str
    error: object


@dataclass(frozen=True)
class ModelAvailabilityChanged:
    available: bool
    supports_render: bool = True


@dataclass(frozen=True)
class PreviewRequested:
    job_id: str
    request_id: str
    requires_model_preparation: bool = False
    initiator_focus: FocusTarget = FocusTarget.PREVIEW_ACTION


@dataclass(frozen=True)
class ModelPrepared:
    job_id: str
    stage: str = "Segmentation"


@dataclass(frozen=True)
class JobStageChanged:
    job_id: str
    stage: str


@dataclass(frozen=True)
class PreviewSucceeded:
    job_id: str
    result: PreviewResult


@dataclass(frozen=True)
class PreviewFailed:
    job_id: str
    source_id: str
    request_id: str
    error: object


@dataclass(frozen=True)
class PreviewInvalidated:
    category: str


@dataclass(frozen=True)
class RenderPreflightRequested:
    pass


@dataclass(frozen=True)
class RenderPreflightDismissed:
    pass


@dataclass(frozen=True)
class RenderRequested:
    job_id: str
    request_id: str
    initiator_focus: FocusTarget = FocusTarget.RENDER_ACTION


@dataclass(frozen=True)
class RebuildRequested:
    job_id: str
    request_id: str
    initiator_focus: FocusTarget = FocusTarget.REBUILD_ACTION


@dataclass(frozen=True)
class RenderSucceeded:
    job_id: str
    result: ArtifactResult


@dataclass(frozen=True)
class RenderFailed:
    job_id: str
    source_id: str
    request_id: str
    error: object


@dataclass(frozen=True)
class CancelRequested:
    job_id: str


@dataclass(frozen=True)
class CancelAcknowledged:
    job_id: str


@dataclass(frozen=True)
class EditedCutsChanged:
    detected: bool
    error: object | None = None


type Event = (
    SourceLoadRequested
    | SourceLoaded
    | SourceLoadFailed
    | ModelAvailabilityChanged
    | PreviewRequested
    | ModelPrepared
    | JobStageChanged
    | PreviewSucceeded
    | PreviewFailed
    | PreviewInvalidated
    | RenderPreflightRequested
    | RenderPreflightDismissed
    | RenderRequested
    | RebuildRequested
    | RenderSucceeded
    | RenderFailed
    | CancelRequested
    | CancelAcknowledged
    | EditedCutsChanged
)


# Qt events -> pure reducer -> immutable AppState -> capability/focus intent -> widgets
def reduce(state: AppState, event: Event) -> AppState:
    """Return the state produced by *event*, preserving identity when ignored."""
    if isinstance(event, SourceLoadRequested):
        if state.job.phase is not JobState.IDLE:
            return state
        return AppState(
            source=SourceState.LOADING,
            source_id=event.source_id,
            model_available=state.model_available,
            model_supports_render=state.model_supports_render,
            focus_target=FocusTarget.NONE,
        )

    if isinstance(event, SourceLoaded):
        if (
            state.source is not SourceState.LOADING
            or event.source_id != state.source_id
        ):
            return state
        return replace(
            state,
            source=SourceState.READY,
            source_value=event.value,
            source_error=None,
            focus_target=FocusTarget.PREVIEW_ACTION,
        )

    if isinstance(event, SourceLoadFailed):
        if (
            state.source is not SourceState.LOADING
            or event.source_id != state.source_id
        ):
            return state
        return replace(
            state,
            source=SourceState.ERROR,
            source_value=None,
            source_error=event.error,
            focus_target=FocusTarget.SOURCE_ERROR_HEADING,
        )

    if isinstance(event, ModelAvailabilityChanged):
        return replace(
            state,
            model_available=event.available,
            model_supports_render=event.supports_render,
        )

    if isinstance(event, PreviewRequested):
        if not capabilities(state).can_preview:
            return state
        preparing = event.requires_model_preparation or not state.model_available
        phase = JobState.PREPARING_MODEL if preparing else JobState.PREVIEWING
        stage = "Preparing model" if preparing else "Segmentation"
        return replace(
            state,
            preview=PreviewState.RUNNING,
            preview_error=None,
            preview_before_job=state.preview,
            preview_request_before_job=state.preview_request_id,
            preview_request_id=event.request_id,
            job=ActiveJob(
                event.job_id,
                JobKind.PREVIEW,
                phase,
                stage,
                event.initiator_focus,
            ),
            job_request_id=event.request_id,
            preflight_warning=False,
            focus_target=FocusTarget.JOB_DIALOG,
        )

    if isinstance(event, ModelPrepared):
        if not _matches_job(state, event.job_id, JobKind.PREVIEW):
            return state
        if state.job.phase is not JobState.PREPARING_MODEL:
            return state
        return replace(
            state,
            model_available=True,
            job=replace(
                state.job,
                phase=JobState.PREVIEWING,
                stage=event.stage,
            ),
        )

    if isinstance(event, JobStageChanged):
        if event.job_id != state.job.job_id or state.job.phase in {
            JobState.IDLE,
            JobState.CANCELLING,
        }:
            return state
        phase = state.job.phase
        if phase is JobState.PREPARING_MODEL and event.stage == "Segmentation":
            phase = JobState.PREVIEWING
        return replace(state, job=replace(state.job, phase=phase, stage=event.stage))

    if isinstance(event, PreviewSucceeded):
        if not _matches_result(
            state,
            event.job_id,
            JobKind.PREVIEW,
            event.result.source_id,
            event.result.request_id,
        ):
            return state
        return replace(
            state,
            preview=PreviewState.CURRENT,
            preview_result=event.result,
            preview_error=None,
            stale_category=None,
            preview_before_job=PreviewState.NONE,
            preview_request_before_job=None,
            job=ActiveJob(),
            job_request_id=None,
            focus_target=FocusTarget.RESULT_CANVAS,
        )

    if isinstance(event, PreviewFailed):
        if not _matches_result(
            state,
            event.job_id,
            JobKind.PREVIEW,
            event.source_id,
            event.request_id,
        ):
            return state
        return replace(
            state,
            preview=PreviewState.ERROR,
            preview_error=event.error,
            stale_category=None,
            preview_before_job=PreviewState.NONE,
            preview_request_before_job=None,
            job=ActiveJob(),
            job_request_id=None,
            focus_target=FocusTarget.PREVIEW_ACTION,
        )

    if isinstance(event, PreviewInvalidated):
        if state.preview is not PreviewState.CURRENT:
            return state
        return replace(
            state,
            preview=PreviewState.STALE,
            stale_category=event.category,
            focus_target=FocusTarget.PREVIEW_ACTION,
        )

    if isinstance(event, RenderPreflightRequested):
        if not capabilities(state).can_render:
            return state
        return replace(
            state,
            preflight_warning=True,
            focus_target=FocusTarget.PREFLIGHT_DIALOG,
        )

    if isinstance(event, RenderPreflightDismissed):
        if not state.preflight_warning:
            return state
        return replace(
            state,
            preflight_warning=False,
            focus_target=_editor_action_focus(state),
        )

    if isinstance(event, RenderRequested):
        if not capabilities(state).can_render:
            return state
        return _start_render(
            state,
            event.job_id,
            event.request_id,
            JobKind.RENDER,
            event.initiator_focus,
        )

    if isinstance(event, RebuildRequested):
        if not capabilities(state).can_rebuild:
            return state
        return _start_render(
            state,
            event.job_id,
            event.request_id,
            JobKind.REBUILD,
            event.initiator_focus,
        )

    if isinstance(event, RenderSucceeded):
        if not _matches_render_result(
            state,
            event.job_id,
            event.result.source_id,
            event.result.request_id,
        ):
            return state
        return replace(
            state,
            job=ActiveJob(),
            job_request_id=None,
            artifact=ArtifactState.VALID,
            artifact_result=event.result,
            artifact_error=None,
            preflight_warning=False,
            focus_target=FocusTarget.SUCCESS_BANNER,
        )

    if isinstance(event, RenderFailed):
        if not _matches_render_result(
            state,
            event.job_id,
            event.source_id,
            event.request_id,
        ):
            return state
        focus = state.job.initiator_focus
        artifact = (
            ArtifactState.VALID
            if state.artifact_result is not None
            else ArtifactState.ERROR
        )
        return replace(
            state,
            job=ActiveJob(),
            job_request_id=None,
            artifact=artifact,
            artifact_error=event.error,
            preflight_warning=False,
            focus_target=focus,
        )

    if isinstance(event, CancelRequested):
        if event.job_id != state.job.job_id or state.job.phase in {
            JobState.IDLE,
            JobState.CANCELLING,
        }:
            return state
        return replace(
            state,
            job=replace(state.job, phase=JobState.CANCELLING, stage="Cancelling"),
            focus_target=FocusTarget.JOB_DIALOG,
        )

    if isinstance(event, CancelAcknowledged):
        if (
            event.job_id != state.job.job_id
            or state.job.phase is not JobState.CANCELLING
        ):
            return state
        focus = state.job.initiator_focus
        preview = state.preview
        preview_request_id = state.preview_request_id
        if state.job.kind is JobKind.PREVIEW:
            preview = state.preview_before_job
            preview_request_id = state.preview_request_before_job
        return replace(
            state,
            preview=preview,
            preview_request_id=preview_request_id,
            preview_before_job=PreviewState.NONE,
            preview_request_before_job=None,
            job=ActiveJob(),
            job_request_id=None,
            focus_target=focus,
        )

    if isinstance(event, EditedCutsChanged):
        focus = state.focus_target
        if event.error is not None:
            focus = FocusTarget.EDITED_CUT_RECOVERY
        elif event.detected:
            focus = FocusTarget.REBUILD_ACTION
        return replace(
            state,
            edited_cuts=event.detected,
            edited_cuts_error=event.error,
            focus_target=focus,
        )

    return state


def capabilities(state: AppState) -> Capabilities:
    """Derive every widget enablement flag and focus intent from application state."""
    idle = state.job.phase is JobState.IDLE
    ready = state.source is SourceState.READY
    editable = idle and ready
    has_artifact = idle and state.artifact is ArtifactState.VALID
    active = not idle
    return Capabilities(
        can_choose_source=(
            idle and state.source in {SourceState.EMPTY, SourceState.ERROR}
        ),
        can_replace_source=editable,
        can_edit=editable,
        can_preview=editable,
        can_render=(editable and state.model_available and state.model_supports_render),
        can_rebuild=(
            editable
            and state.artifact is ArtifactState.VALID
            and state.edited_cuts
            and state.edited_cuts_error is None
        ),
        can_cancel=active and state.job.phase is not JobState.CANCELLING,
        can_open_output=has_artifact,
        can_open_folder=has_artifact,
        focus_target=FocusTarget.JOB_DIALOG if active else state.focus_target,
    )


def _start_render(
    state: AppState,
    job_id: str,
    request_id: str,
    kind: JobKind,
    initiator_focus: FocusTarget,
) -> AppState:
    return replace(
        state,
        job=ActiveJob(
            job_id,
            kind,
            JobState.RENDERING,
            "Decode",
            initiator_focus,
        ),
        job_request_id=request_id,
        preflight_warning=False,
        focus_target=FocusTarget.JOB_DIALOG,
    )


def _matches_job(state: AppState, job_id: str, kind: JobKind) -> bool:
    return state.job.job_id == job_id and state.job.kind is kind


def _matches_result(
    state: AppState,
    job_id: str,
    kind: JobKind,
    source_id: str,
    request_id: str,
) -> bool:
    return (
        _matches_job(state, job_id, kind)
        and state.source_id == source_id
        and state.job_request_id == request_id
    )


def _matches_render_result(
    state: AppState,
    job_id: str,
    source_id: str,
    request_id: str,
) -> bool:
    return (
        state.job.kind in {JobKind.RENDER, JobKind.REBUILD}
        and state.job.job_id == job_id
        and state.source_id == source_id
        and state.job_request_id == request_id
    )


def _editor_action_focus(state: AppState) -> FocusTarget:
    if state.preview is PreviewState.CURRENT:
        return FocusTarget.RENDER_ACTION
    return FocusTarget.PREVIEW_ACTION
