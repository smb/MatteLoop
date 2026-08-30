"""Pure immutable application state, reducer events, and derived capabilities."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum

from rembggui.core import crop_state as _crop_state
from rembggui.core import parameters as _parameters
from rembggui.core import specs
from rembggui.core.parameters import ParameterState
from rembggui.core.timeline import (
    DurationChanged,
    SourceFrameDecoded,
    TimelineEvent,
    TimelineState,
    timeline_from_metadata,
)
from rembggui.core.timeline_reducer import reduce_timeline


def __getattr__(name: str) -> object:
    if hasattr(_crop_state, name):
        return getattr(_crop_state, name)
    return getattr(_parameters, name)


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
class PreviewSnapshot:
    """Preview state preserved while an exclusive replacement attempt runs."""

    phase: PreviewState
    result: PreviewResult | None
    request_id: str | None
    error: object | None
    attempt_error: object | None
    stale_category: str | None

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
    source_request_id: str | None = None
    source_value: object | None = None
    source_frame: object | None = None
    timeline: TimelineState | None = None
    parameters: ParameterState = field(default_factory=ParameterState)
    crop: specs.CropSpec | None = None
    crop_enabled: bool = True
    source_error: object | None = None
    model_available: bool = True
    model_supports_render: bool = True
    preview: PreviewState = PreviewState.NONE
    preview_result: PreviewResult | None = None
    preview_request_id: str | None = None
    preview_error: object | None = None
    preview_attempt_error: object | None = None
    stale_category: str | None = None
    preview_before_job: PreviewSnapshot | None = None
    job: ActiveJob = field(default_factory=ActiveJob)
    job_request_id: str | None = None
    artifact: ArtifactState = ArtifactState.NONE
    artifact_result: ArtifactResult | None = None
    artifact_error: object | None = None
    preflight_warning: bool = False
    edited_cuts: bool = False
    edited_cuts_error: object | None = None
    edited_cuts_request_id: str | None = None
    focus_target: FocusTarget = FocusTarget.CHOOSE_VIDEO

    def __post_init__(self) -> None:
        if self.source is SourceState.LOADING and (
            self.source_id is None or self.source_request_id is None
        ):
            raise ValueError("loading source requires source and request identities")
        if self.source is SourceState.READY and (
            self.source_id is None or self.source_value is None
        ):
            raise ValueError("ready source requires identity and metadata")
        if self.source is SourceState.ERROR and (
            self.source_id is None or self.source_request_id is None
            or self.source_error is None
        ):
            raise ValueError("source error requires identities and an error")
        if self.preview in {PreviewState.CURRENT, PreviewState.STALE}:
            if (
                self.preview_result is None
                or self.preview_request_id != self.preview_result.request_id
                or self.source_id != self.preview_result.source_id
            ):
                raise ValueError("visible preview requires a matching result identity")
        if self.preview is PreviewState.ERROR and (
            self.preview_error is None or self.preview_request_id is None
        ):
            raise ValueError("preview error requires an error and request identity")
        if self.job.phase is JobState.IDLE:
            if (
                self.job.job_id is not None
                or self.job.kind is not None
                or self.job_request_id is not None
            ):
                raise ValueError("idle job cannot retain active identities")
        elif (
            self.job.job_id is None
            or self.job.kind is None
            or self.job_request_id is None
            or self.source is not SourceState.READY
        ):
            raise ValueError("active job requires job, source, and request identities")
        if (
            self.preview is PreviewState.RUNNING
            and self.job.kind is not JobKind.PREVIEW
        ):
            raise ValueError("running preview requires an active preview job")
        if self.artifact is ArtifactState.VALID and (
            self.source is not SourceState.READY
            or self.artifact_result is None
            or self.source_id != self.artifact_result.source_id
        ):
            raise ValueError(
                "valid artifact requires a ready source and matching result"
            )
        if self.edited_cuts and self.artifact is not ArtifactState.VALID:
            raise ValueError("edited cuts require a valid artifact")

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
    request_id: str


@dataclass(frozen=True)
class SourceLoaded:
    source_id: str
    request_id: str
    value: object
    frame: object | None = None


@dataclass(frozen=True)
class SourceLoadFailed:
    source_id: str
    request_id: str
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
    source_id: str
    request_id: str
    stage: str = "Segmentation"


@dataclass(frozen=True)
class JobStageChanged:
    job_id: str
    source_id: str
    request_id: str
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
class EditedCutsScanRequested:
    source_id: str
    artifact_request_id: str
    request_id: str


@dataclass(frozen=True)
class EditedCutsChanged:
    source_id: str
    artifact_request_id: str
    request_id: str
    detected: bool
    error: object | None = None


type Event = (
    SourceLoadRequested
    | SourceLoaded
    | SourceFrameDecoded
    | SourceLoadFailed
    | ModelAvailabilityChanged
    | _parameters.ParameterEvent
    | DurationChanged
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
    | EditedCutsScanRequested
    | EditedCutsChanged | _crop_state.CropEvent | TimelineEvent
)


# Qt events -> pure reducer -> immutable AppState -> capability/focus intent -> widgets
def reduce(state: AppState, event: Event) -> AppState:
    """Return the state produced by *event*, preserving identity when ignored."""
    if isinstance(event, SourceLoadRequested):
        if state.job.phase is not JobState.IDLE:
            return state
        return AppState(
            source=SourceState.LOADING, source_id=event.source_id,
            source_request_id=event.request_id,
            parameters=state.parameters,
            model_available=state.model_available,
            model_supports_render=state.model_supports_render,
            focus_target=FocusTarget.NONE,
        )
    if isinstance(event, SourceLoaded):
        if not _matches_source_request(state, event.source_id, event.request_id):
            return state
        return replace(
            state,
            source=SourceState.READY,
            source_value=event.value,
            source_frame=event.frame,
            timeline=timeline_from_metadata(event.value, state.parameters.fps),
            crop=_crop_state.default_crop_for_source(event.value), source_error=None,
            focus_target=FocusTarget.PREVIEW_ACTION,
        )
    if isinstance(event, _crop_state.CropEvent):
        return _crop_state.reduce_crop(state, event)
    if isinstance(event, _parameters.ParameterEvent):
        return _parameters.reduce_parameters(state, event)
    if isinstance(event, (SourceFrameDecoded, TimelineEvent)):
        return reduce_timeline(state, event)
    if isinstance(event, SourceLoadFailed):
        if not _matches_source_request(state, event.source_id, event.request_id):
            return state
        return replace(
            state,
            source=SourceState.ERROR,
            source_value=None,
            source_frame=None,
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
            preview_attempt_error=None,
            preview_before_job=_preview_snapshot(state),
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
        if not _matches_job_notification(
            state,
            event.job_id,
            event.source_id,
            event.request_id,
            {JobKind.PREVIEW},
        ):
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
        if (
            not _matches_job_notification(
                state,
                event.job_id,
                event.source_id,
                event.request_id,
                {JobKind.PREVIEW, JobKind.RENDER, JobKind.REBUILD},
            )
            or state.job.phase is JobState.CANCELLING
        ):
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
            preview_attempt_error=None,
            stale_category=None,
            preview_before_job=None,
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
        previous = state.preview_before_job
        if previous is not None and previous.result is not None:
            return replace(
                state,
                preview=PreviewState.STALE,
                preview_result=previous.result,
                preview_request_id=previous.request_id,
                preview_error=None,
                preview_attempt_error=event.error,
                stale_category="Preview failed",
                preview_before_job=None,
                job=ActiveJob(),
                job_request_id=None,
                focus_target=FocusTarget.PREVIEW_ACTION,
            )
        return replace(
            state,
            preview=PreviewState.ERROR,
            preview_result=None,
            preview_request_id=event.request_id,
            preview_error=event.error,
            preview_attempt_error=event.error,
            stale_category=None,
            preview_before_job=None,
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
            edited_cuts=False,
            edited_cuts_error=None,
            edited_cuts_request_id=None,
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
        previous = state.preview_before_job
        if state.job.kind is JobKind.PREVIEW and previous is not None:
            return replace(
                state,
                preview=previous.phase,
                preview_result=previous.result,
                preview_request_id=previous.request_id,
                preview_error=previous.error,
                preview_attempt_error=previous.attempt_error,
                stale_category=previous.stale_category,
                preview_before_job=None,
                job=ActiveJob(),
                job_request_id=None,
                focus_target=focus,
            )
        return replace(
            state,
            preview_before_job=None,
            job=ActiveJob(),
            job_request_id=None,
            focus_target=focus,
        )
    if isinstance(event, EditedCutsScanRequested):
        if not _matches_artifact_identity(
            state, event.source_id, event.artifact_request_id
        ):
            return state
        focus = state.focus_target
        if focus is FocusTarget.REBUILD_ACTION:
            focus = _editor_action_focus(state)
        return replace(
            state,
            edited_cuts_request_id=event.request_id,
            focus_target=focus,
        )
    if isinstance(event, EditedCutsChanged):
        if not _matches_edited_cuts_result(state, event):
            return state
        preview = state.preview
        stale_category = state.stale_category
        if event.detected and preview is PreviewState.CURRENT:
            preview = PreviewState.STALE
            stale_category = "Edited cuts"
        if event.error is not None:
            focus = FocusTarget.EDITED_CUT_RECOVERY
        elif event.detected:
            focus = FocusTarget.REBUILD_ACTION
        else:
            focus = _editor_action_focus(state)
        return replace(
            state,
            preview=preview,
            stale_category=stale_category,
            edited_cuts=event.detected,
            edited_cuts_error=event.error,
            edited_cuts_request_id=None,
            focus_target=focus,
        )
    return state


def capabilities(state: AppState) -> Capabilities:
    """Derive every widget enablement flag and focus intent from application state."""
    idle = state.job.phase is JobState.IDLE
    ready = (
        state.source is SourceState.READY
        and state.source_id is not None
        and state.source_value is not None
    )
    editable = idle and ready
    has_artifact = (
        idle
        and ready
        and state.artifact is ArtifactState.VALID
        and state.artifact_result is not None
        and state.artifact_result.source_id == state.source_id
    )
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
            has_artifact
            and state.edited_cuts
            and state.edited_cuts_error is None
            and state.edited_cuts_request_id is None
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


def _matches_source_request(state: AppState, source_id: str, request_id: str) -> bool:
    return (
        state.source is SourceState.LOADING
        and state.source_id == source_id
        and state.source_request_id == request_id
    )


def _matches_job_notification(
    state: AppState,
    job_id: str,
    source_id: str,
    request_id: str,
    kinds: set[JobKind],
) -> bool:
    return (
        state.job.phase is not JobState.IDLE
        and state.job.job_id == job_id
        and state.job.kind in kinds
        and state.source_id == source_id
        and state.job_request_id == request_id
    )


def _matches_result(
    state: AppState,
    job_id: str,
    kind: JobKind,
    source_id: str,
    request_id: str,
) -> bool:
    return _matches_job_notification(
        state,
        job_id,
        source_id,
        request_id,
        {kind},
    )


def _matches_render_result(
    state: AppState,
    job_id: str,
    source_id: str,
    request_id: str,
) -> bool:
    return _matches_job_notification(
        state,
        job_id,
        source_id,
        request_id,
        {JobKind.RENDER, JobKind.REBUILD},
    )


def _matches_artifact_identity(
    state: AppState, source_id: str, artifact_request_id: str
) -> bool:
    return (
        state.artifact is ArtifactState.VALID
        and state.artifact_result is not None
        and state.source_id == source_id
        and state.artifact_result.source_id == source_id
        and state.artifact_result.request_id == artifact_request_id
    )


def _matches_edited_cuts_result(state: AppState, event: EditedCutsChanged) -> bool:
    return (
        _matches_artifact_identity(state, event.source_id, event.artifact_request_id)
        and state.edited_cuts_request_id == event.request_id
    )


def _preview_snapshot(state: AppState) -> PreviewSnapshot:
    return PreviewSnapshot(
        phase=state.preview,
        result=state.preview_result,
        request_id=state.preview_request_id,
        error=state.preview_error,
        attempt_error=state.preview_attempt_error,
        stale_category=state.stale_category,
    )


def _editor_action_focus(state: AppState) -> FocusTarget:
    if state.preview is PreviewState.CURRENT:
        return FocusTarget.RENDER_ACTION
    return FocusTarget.PREVIEW_ACTION
