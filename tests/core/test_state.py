from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass

import pytest

from rembggui.core.state import (
    AppState,
    ArtifactResult,
    ArtifactState,
    CancelAcknowledged,
    CancelRequested,
    Capabilities,
    EditedCutsChanged,
    EditedCutsScanRequested,
    FocusTarget,
    JobStageChanged,
    JobState,
    ModelAvailabilityChanged,
    ModelPrepared,
    PreviewFailed,
    PreviewInvalidated,
    PreviewRequested,
    PreviewResult,
    PreviewState,
    PreviewSucceeded,
    RebuildRequested,
    RenderFailed,
    RenderPreflightDismissed,
    RenderPreflightRequested,
    RenderRequested,
    RenderSucceeded,
    SourceLoaded,
    SourceLoadFailed,
    SourceLoadRequested,
    SourceState,
    capabilities,
    reduce,
)

SOURCE_ID = "source-1"
SOURCE_REQUEST_ID = "load-1"
SOURCE_VALUE = "metadata"


def loading_state(
    *, source_id: str = SOURCE_ID, request_id: str = SOURCE_REQUEST_ID
) -> AppState:
    return reduce(
        AppState(),
        SourceLoadRequested(source_id=source_id, request_id=request_id),
    )


def ready_state(
    *, model_available: bool = True, model_supports_render: bool = True
) -> AppState:
    state = reduce(
        loading_state(),
        SourceLoaded(
            source_id=SOURCE_ID,
            request_id=SOURCE_REQUEST_ID,
            value=SOURCE_VALUE,
        ),
    )
    if not model_available or not model_supports_render:
        state = reduce(
            state,
            ModelAvailabilityChanged(
                available=model_available,
                supports_render=model_supports_render,
            ),
        )
    return state


def preview_result(
    *, source_id: str = SOURCE_ID, request_id: str = "preview-1"
) -> PreviewResult:
    return PreviewResult(source_id, request_id, "preview")


def running_preview(
    *,
    job_id: str = "preview-job",
    request_id: str = "preview-1",
    state: AppState | None = None,
    requires_model_preparation: bool = False,
) -> AppState:
    return reduce(
        state if state is not None else ready_state(),
        PreviewRequested(
            job_id=job_id,
            request_id=request_id,
            requires_model_preparation=requires_model_preparation,
        ),
    )


def current_preview(
    *, request_id: str = "preview-1", state: AppState | None = None
) -> AppState:
    running = running_preview(request_id=request_id, state=state)
    return reduce(
        running,
        PreviewSucceeded(
            job_id="preview-job",
            result=preview_result(request_id=request_id),
        ),
    )


def stale_preview() -> AppState:
    return reduce(current_preview(), PreviewInvalidated(category="Framing"))


def failed_preview() -> AppState:
    return reduce(
        running_preview(),
        PreviewFailed(
            job_id="preview-job",
            source_id=SOURCE_ID,
            request_id="preview-1",
            error="inference failed",
        ),
    )


def running_render(
    *,
    state: AppState | None = None,
    job_id: str = "render-job",
    request_id: str = "render-1",
) -> AppState:
    return reduce(
        state if state is not None else ready_state(),
        RenderRequested(job_id=job_id, request_id=request_id),
    )


def render_complete(*, state: AppState | None = None) -> AppState:
    running = running_render(state=state)
    return reduce(
        running,
        RenderSucceeded(
            job_id="render-job",
            result=ArtifactResult(SOURCE_ID, "render-1", "cutout.webp"),
        ),
    )


def scan_for_edited_cuts(state: AppState, *, request_id: str = "scan-1") -> AppState:
    return reduce(
        state,
        EditedCutsScanRequested(
            source_id=SOURCE_ID,
            artifact_request_id="render-1",
            request_id=request_id,
        ),
    )


def edited_cuts_state(*, state: AppState | None = None) -> AppState:
    rendered = render_complete(state=state)
    scanning = scan_for_edited_cuts(rendered)
    return reduce(
        scanning,
        EditedCutsChanged(
            source_id=SOURCE_ID,
            artifact_request_id="render-1",
            request_id="scan-1",
            detected=True,
        ),
    )


def running_rebuild() -> AppState:
    return reduce(
        edited_cuts_state(),
        RebuildRequested(job_id="rebuild-job", request_id="rebuild-1"),
    )


@dataclass(frozen=True)
class CapabilityExpectation:
    focus: FocusTarget
    choose_source: bool = False
    replace_source: bool = False
    edit: bool = False
    preview: bool = False
    render: bool = False
    rebuild: bool = False
    cancel: bool = False
    open_output: bool = False
    open_folder: bool = False


def assert_capabilities(actual: Capabilities, expected: CapabilityExpectation) -> None:
    assert actual.can_choose_source is expected.choose_source
    assert actual.can_replace_source is expected.replace_source
    assert actual.can_edit is expected.edit
    assert actual.can_preview is expected.preview
    assert actual.can_render is expected.render
    assert actual.can_rebuild is expected.rebuild
    assert actual.can_cancel is expected.cancel
    assert actual.can_open_output is expected.open_output
    assert actual.can_open_folder is expected.open_folder
    assert actual.focus_target is expected.focus


def matrix_cases() -> list[pytest.ParamSpecArg]:
    source_error = reduce(
        loading_state(),
        SourceLoadFailed(
            source_id=SOURCE_ID,
            request_id=SOURCE_REQUEST_ID,
            error="unsupported codec",
        ),
    )
    preflight = reduce(stale_preview(), RenderPreflightRequested())
    cancelling = reduce(running_render(), CancelRequested(job_id="render-job"))
    rendered = render_complete()
    edited = edited_cuts_state(state=current_preview())
    return [
        pytest.param(
            AppState(),
            CapabilityExpectation(
                focus=FocusTarget.CHOOSE_VIDEO,
                choose_source=True,
            ),
            id="empty",
        ),
        pytest.param(
            loading_state(),
            CapabilityExpectation(focus=FocusTarget.NONE),
            id="loading",
        ),
        pytest.param(
            source_error,
            CapabilityExpectation(
                focus=FocusTarget.SOURCE_ERROR_HEADING,
                choose_source=True,
            ),
            id="source-error",
        ),
        pytest.param(
            ready_state(),
            CapabilityExpectation(
                focus=FocusTarget.PREVIEW_ACTION,
                replace_source=True,
                edit=True,
                preview=True,
                render=True,
            ),
            id="ready-no-preview",
        ),
        pytest.param(
            ready_state(model_available=False),
            CapabilityExpectation(
                focus=FocusTarget.PREVIEW_ACTION,
                replace_source=True,
                edit=True,
                preview=True,
            ),
            id="model-unavailable",
        ),
        pytest.param(
            running_preview(),
            CapabilityExpectation(focus=FocusTarget.JOB_DIALOG, cancel=True),
            id="preview-running",
        ),
        pytest.param(
            current_preview(),
            CapabilityExpectation(
                focus=FocusTarget.RESULT_CANVAS,
                replace_source=True,
                edit=True,
                preview=True,
                render=True,
            ),
            id="preview-current",
        ),
        pytest.param(
            stale_preview(),
            CapabilityExpectation(
                focus=FocusTarget.PREVIEW_ACTION,
                replace_source=True,
                edit=True,
                preview=True,
                render=True,
            ),
            id="preview-stale",
        ),
        pytest.param(
            failed_preview(),
            CapabilityExpectation(
                focus=FocusTarget.PREVIEW_ACTION,
                replace_source=True,
                edit=True,
                preview=True,
                render=True,
            ),
            id="preview-error",
        ),
        pytest.param(
            preflight,
            CapabilityExpectation(
                focus=FocusTarget.PREFLIGHT_DIALOG,
                replace_source=True,
                edit=True,
                preview=True,
                render=True,
            ),
            id="render-preflight-warning",
        ),
        pytest.param(
            running_render(),
            CapabilityExpectation(focus=FocusTarget.JOB_DIALOG, cancel=True),
            id="render-running",
        ),
        pytest.param(
            running_rebuild(),
            CapabilityExpectation(focus=FocusTarget.JOB_DIALOG, cancel=True),
            id="rebuild-running",
        ),
        pytest.param(
            cancelling,
            CapabilityExpectation(focus=FocusTarget.JOB_DIALOG),
            id="cancelling",
        ),
        pytest.param(
            rendered,
            CapabilityExpectation(
                focus=FocusTarget.SUCCESS_BANNER,
                replace_source=True,
                edit=True,
                preview=True,
                render=True,
                open_output=True,
                open_folder=True,
            ),
            id="render-complete",
        ),
        pytest.param(
            edited,
            CapabilityExpectation(
                focus=FocusTarget.REBUILD_ACTION,
                replace_source=True,
                edit=True,
                preview=True,
                render=True,
                rebuild=True,
                open_output=True,
                open_folder=True,
            ),
            id="edited-cuts",
        ),
    ]


@pytest.mark.parametrize(("state", "expected"), matrix_cases())
def test_user_visible_state_matrix_derives_capabilities_and_focus(
    state: AppState, expected: CapabilityExpectation
) -> None:
    assert_capabilities(capabilities(state), expected)


def test_late_job_result_is_ignored() -> None:
    state = running_preview(job_id="new")

    assert (
        reduce(
            state,
            PreviewSucceeded(job_id="old", result=preview_result()),
        )
        is state
    )


def test_cancel_keeps_editor_locked_until_ack() -> None:
    cancelling = reduce(running_render(job_id="j1"), CancelRequested(job_id="j1"))

    assert cancelling.job.phase is JobState.CANCELLING
    assert not capabilities(cancelling).can_edit

    idle = reduce(cancelling, CancelAcknowledged(job_id="j1"))

    assert idle.job.phase is JobState.IDLE


def test_same_source_probe_result_requires_the_current_load_request() -> None:
    loading = loading_state(request_id="load-new")

    assert (
        reduce(
            loading,
            SourceLoaded(SOURCE_ID, "load-old", "stale metadata"),
        )
        is loading
    )
    assert (
        reduce(
            loading,
            SourceLoadFailed(SOURCE_ID, "load-old", "stale error"),
        )
        is loading
    )

    ready = reduce(
        loading,
        SourceLoaded(SOURCE_ID, "load-new", "current metadata"),
    )

    assert ready.source is SourceState.READY
    assert ready.source_value == "current metadata"


def test_matching_source_failure_exposes_recovery_focus() -> None:
    failed = reduce(
        loading_state(),
        SourceLoadFailed(SOURCE_ID, SOURCE_REQUEST_ID, "unsupported codec"),
    )

    assert failed.source is SourceState.ERROR
    assert capabilities(failed).focus_target is FocusTarget.SOURCE_ERROR_HEADING


def test_preview_result_requires_source_request_and_job_identities() -> None:
    running = running_preview(job_id="job-1")

    for stale in (
        PreviewSucceeded("job-old", preview_result()),
        PreviewSucceeded("job-1", preview_result(source_id="source-old")),
        PreviewSucceeded("job-1", preview_result(request_id="preview-old")),
    ):
        assert reduce(running, stale) is running

    current = reduce(
        running,
        PreviewSucceeded("job-1", preview_result()),
    )

    assert current.preview is PreviewState.CURRENT
    assert current.job.phase is JobState.IDLE
    assert capabilities(current).focus_target is FocusTarget.RESULT_CANVAS


@pytest.mark.parametrize(
    ("job_id", "source_id", "request_id"),
    [
        ("job-old", SOURCE_ID, "preview-1"),
        ("job-1", "source-old", "preview-1"),
        ("job-1", SOURCE_ID, "preview-old"),
    ],
)
def test_model_prepared_requires_all_current_identity_tokens(
    job_id: str, source_id: str, request_id: str
) -> None:
    preparing = running_preview(
        job_id="job-1",
        state=ready_state(model_available=False),
        requires_model_preparation=True,
    )

    stale = ModelPrepared(job_id, source_id, request_id)

    assert reduce(preparing, stale) is preparing


def test_matching_model_prepared_advances_the_current_preview() -> None:
    preparing = running_preview(
        job_id="job-1",
        state=ready_state(model_available=False),
        requires_model_preparation=True,
    )

    previewing = reduce(
        preparing,
        ModelPrepared("job-1", SOURCE_ID, "preview-1"),
    )

    assert previewing.job.phase is JobState.PREVIEWING


@pytest.mark.parametrize(
    ("job_id", "source_id", "request_id"),
    [
        ("job-old", SOURCE_ID, "preview-1"),
        ("job-1", "source-old", "preview-1"),
        ("job-1", SOURCE_ID, "preview-old"),
    ],
)
def test_job_progress_requires_all_current_identity_tokens(
    job_id: str, source_id: str, request_id: str
) -> None:
    running = running_preview(job_id="job-1")

    stale = JobStageChanged(job_id, source_id, request_id, "Post-process")

    assert reduce(running, stale) is running


def test_matching_job_progress_updates_the_stage() -> None:
    running = running_preview(job_id="job-1")

    progressed = reduce(
        running,
        JobStageChanged("job-1", SOURCE_ID, "preview-1", "Post-process"),
    )

    assert progressed.job.stage == "Post-process"


def test_preview_failure_unlocks_only_for_matching_safe_terminal_event() -> None:
    running = running_preview(job_id="job-1")
    stale = PreviewFailed("job-old", SOURCE_ID, "preview-1", "stale failure")

    assert reduce(running, stale) is running

    failed = reduce(
        running,
        PreviewFailed("job-1", SOURCE_ID, "preview-1", "current failure"),
    )

    assert failed.preview is PreviewState.ERROR
    assert failed.job.phase is JobState.IDLE
    assert capabilities(failed).focus_target is FocusTarget.PREVIEW_ACTION


def test_failed_repreview_restores_old_result_identity_as_stale() -> None:
    previous = current_preview(request_id="preview-old")
    running = running_preview(
        state=previous,
        job_id="replacement-job",
        request_id="preview-new",
    )

    failed = reduce(
        running,
        PreviewFailed("replacement-job", SOURCE_ID, "preview-new", "inference failed"),
    )

    assert failed.preview is PreviewState.STALE
    assert failed.preview_result == preview_result(request_id="preview-old")
    assert failed.preview_request_id == "preview-old"
    assert failed.preview_attempt_error == "inference failed"


def test_failed_first_preview_has_no_stale_result_identity() -> None:
    failed = failed_preview()

    assert failed.preview is PreviewState.ERROR
    assert failed.preview_result is None
    assert failed.preview_request_id == "preview-1"
    assert failed.preview_attempt_error == "inference failed"


def test_cancelled_repreview_restores_the_complete_previous_preview() -> None:
    previous = current_preview(request_id="preview-old")
    running = running_preview(
        state=previous,
        job_id="replacement-job",
        request_id="preview-new",
    )

    restored = reduce(
        reduce(running, CancelRequested(job_id="replacement-job")),
        CancelAcknowledged(job_id="replacement-job"),
    )

    assert restored.preview is PreviewState.CURRENT
    assert restored.preview_result == preview_result(request_id="preview-old")
    assert restored.preview_request_id == "preview-old"
    assert restored.preview_attempt_error is None


def test_settings_change_stales_current_preview_without_deleting_it() -> None:
    current = current_preview()
    result = current.preview_result

    stale = reduce(current, PreviewInvalidated(category="Framing"))

    assert stale.preview is PreviewState.STALE
    assert stale.preview_result is result
    assert stale.stale_category == "Framing"


def test_preflight_warning_does_not_start_or_queue_a_render() -> None:
    warning = reduce(stale_preview(), RenderPreflightRequested())

    assert warning.job.phase is JobState.IDLE
    assert capabilities(warning).focus_target is FocusTarget.PREFLIGHT_DIALOG

    dismissed = reduce(warning, RenderPreflightDismissed())

    assert dismissed.job.phase is JobState.IDLE
    assert capabilities(dismissed).focus_target is FocusTarget.PREVIEW_ACTION


@pytest.mark.parametrize(
    ("running", "job_id", "focus"),
    [
        (running_render(job_id="job-1"), "job-1", FocusTarget.RENDER_ACTION),
        (running_rebuild(), "rebuild-job", FocusTarget.REBUILD_ACTION),
    ],
)
def test_cancel_ack_restores_the_initiating_action_focus(
    running: AppState, job_id: str, focus: FocusTarget
) -> None:
    idle = reduce(
        reduce(running, CancelRequested(job_id)),
        CancelAcknowledged(job_id),
    )

    assert capabilities(idle).focus_target is focus


def test_render_success_requires_source_request_and_job_identities() -> None:
    running = running_render(job_id="job-1")
    result = ArtifactResult(SOURCE_ID, "render-1", "cutout.webp")

    assert reduce(running, RenderSucceeded("job-old", result)) is running
    assert (
        reduce(
            running,
            RenderSucceeded(
                "job-1", ArtifactResult(SOURCE_ID, "render-old", "old.webp")
            ),
        )
        is running
    )

    completed = reduce(running, RenderSucceeded("job-1", result))

    assert completed.artifact is ArtifactState.VALID
    assert completed.job.phase is JobState.IDLE
    assert capabilities(completed).focus_target is FocusTarget.SUCCESS_BANNER


def test_matching_success_can_safely_terminate_a_cancelling_preview() -> None:
    cancelling = reduce(running_preview(job_id="job-1"), CancelRequested("job-1"))

    completed = reduce(
        cancelling,
        PreviewSucceeded("job-1", preview_result()),
    )

    assert completed.preview is PreviewState.CURRENT
    assert completed.job.phase is JobState.IDLE


def test_matching_failure_can_safely_terminate_a_cancelling_render() -> None:
    cancelling = reduce(running_render(job_id="job-1"), CancelRequested("job-1"))

    failed = reduce(
        cancelling,
        RenderFailed("job-1", SOURCE_ID, "render-1", "encoder failed"),
    )

    assert failed.artifact is ArtifactState.ERROR
    assert failed.job.phase is JobState.IDLE


def test_failed_rerender_preserves_the_previous_valid_artifact() -> None:
    previous = render_complete()
    running = running_render(
        state=previous,
        job_id="replacement-job",
        request_id="replacement-render",
    )

    failed = reduce(
        running,
        RenderFailed(
            "replacement-job",
            SOURCE_ID,
            "replacement-render",
            "encoder failed",
        ),
    )

    assert failed.artifact is ArtifactState.VALID
    assert failed.artifact_result == ArtifactResult(
        SOURCE_ID, "render-1", "cutout.webp"
    )
    assert capabilities(failed).can_open_output


def test_edited_cut_result_requires_source_artifact_and_scan_identities() -> None:
    scanning = scan_for_edited_cuts(render_complete(), request_id="scan-new")

    for stale in (
        EditedCutsChanged("source-old", "render-1", "scan-new", detected=True),
        EditedCutsChanged(SOURCE_ID, "render-old", "scan-new", detected=True),
        EditedCutsChanged(SOURCE_ID, "render-1", "scan-old", detected=True),
    ):
        assert reduce(scanning, stale) is scanning

    detected = reduce(
        scanning,
        EditedCutsChanged(SOURCE_ID, "render-1", "scan-new", detected=True),
    )

    assert detected.edited_cuts


def test_detected_edits_stale_current_preview_without_discarding_result() -> None:
    current = current_preview()
    result = current.preview_result
    detected = edited_cuts_state(state=current)

    assert detected.preview is PreviewState.STALE
    assert detected.preview_result is result
    assert detected.stale_category == "Edited cuts"
    assert capabilities(detected).can_rebuild
    assert capabilities(detected).focus_target is FocusTarget.REBUILD_ACTION


def test_cleared_edits_move_focus_off_the_disabled_rebuild_action() -> None:
    detected = edited_cuts_state()
    scanning = scan_for_edited_cuts(detected, request_id="scan-2")

    cleared = reduce(
        scanning,
        EditedCutsChanged(SOURCE_ID, "render-1", "scan-2", detected=False),
    )

    assert not capabilities(cleared).can_rebuild
    assert capabilities(cleared).focus_target is FocusTarget.PREVIEW_ACTION


def test_edited_cut_error_focuses_recovery_and_disables_rebuild() -> None:
    scanning = scan_for_edited_cuts(render_complete())

    failed = reduce(
        scanning,
        EditedCutsChanged(
            SOURCE_ID,
            "render-1",
            "scan-1",
            detected=True,
            error="frame 0004 hash mismatch",
        ),
    )

    assert not capabilities(failed).can_rebuild
    assert capabilities(failed).focus_target is FocusTarget.EDITED_CUT_RECOVERY


@pytest.mark.parametrize(
    "values",
    [
        {"source": SourceState.READY},
        {
            "artifact": ArtifactState.VALID,
            "source": SourceState.READY,
            "source_id": SOURCE_ID,
            "source_value": SOURCE_VALUE,
        },
    ],
)
def test_incomplete_public_state_shells_are_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        AppState(**values)  # type: ignore[arg-type]


def test_state_and_nested_job_are_immutable() -> None:
    state = running_preview(job_id="job-1")

    with pytest.raises(FrozenInstanceError):
        state.source = SourceState.EMPTY  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        state.job.stage = "Encode"  # type: ignore[misc]
