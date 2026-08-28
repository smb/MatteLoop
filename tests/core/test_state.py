from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from rembggui.core.state import (
    ActiveJob,
    AppState,
    ArtifactResult,
    ArtifactState,
    CancelAcknowledged,
    CancelRequested,
    EditedCutsChanged,
    FocusTarget,
    JobKind,
    JobStageChanged,
    JobState,
    ModelAvailabilityChanged,
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


def preview_result(
    *, source_id: str = "source-1", request_id: str = "request-1"
) -> PreviewResult:
    return PreviewResult(source_id=source_id, request_id=request_id, value="preview")


def running_preview(*, job_id: str) -> AppState:
    return AppState(
        source=SourceState.READY,
        source_id="source-1",
        preview=PreviewState.RUNNING,
        preview_request_id="request-1",
        job=ActiveJob(
            job_id=job_id,
            kind=JobKind.PREVIEW,
            phase=JobState.PREVIEWING,
            stage="Segmentation",
            initiator_focus=FocusTarget.PREVIEW_ACTION,
        ),
    )


def running_render(*, job_id: str) -> AppState:
    return AppState(
        source=SourceState.READY,
        source_id="source-1",
        preview=PreviewState.CURRENT,
        preview_request_id="request-1",
        job=ActiveJob(
            job_id=job_id,
            kind=JobKind.RENDER,
            phase=JobState.RENDERING,
            stage="Encode",
            initiator_focus=FocusTarget.RENDER_ACTION,
        ),
    )


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


def ready_state(**changes: object) -> AppState:
    values: dict[str, object] = {
        "source": SourceState.READY,
        "source_id": "source-1",
        "focus_target": FocusTarget.PREVIEW_ACTION,
    }
    values.update(changes)
    return AppState(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    (
        "name",
        "state",
        "expected",
    ),
    [
        (
            "empty",
            AppState(),
            (
                True,
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                FocusTarget.CHOOSE_VIDEO,
            ),
        ),
        (
            "loading",
            AppState(
                source=SourceState.LOADING,
                source_id="source-1",
                focus_target=FocusTarget.NONE,
            ),
            (
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                FocusTarget.NONE,
            ),
        ),
        (
            "source error",
            AppState(
                source=SourceState.ERROR,
                source_error="unreadable",
                focus_target=FocusTarget.SOURCE_ERROR_HEADING,
            ),
            (
                True,
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                FocusTarget.SOURCE_ERROR_HEADING,
            ),
        ),
        (
            "ready no preview",
            ready_state(),
            (
                False,
                True,
                True,
                True,
                True,
                False,
                False,
                False,
                False,
                FocusTarget.PREVIEW_ACTION,
            ),
        ),
        (
            "model unavailable",
            ready_state(model_available=False),
            (
                False,
                True,
                True,
                True,
                False,
                False,
                False,
                False,
                False,
                FocusTarget.PREVIEW_ACTION,
            ),
        ),
        (
            "preview running",
            running_preview(job_id="j1"),
            (
                False,
                False,
                False,
                False,
                False,
                False,
                True,
                False,
                False,
                FocusTarget.JOB_DIALOG,
            ),
        ),
        (
            "preview current",
            ready_state(
                preview=PreviewState.CURRENT, focus_target=FocusTarget.RESULT_CANVAS
            ),
            (
                False,
                True,
                True,
                True,
                True,
                False,
                False,
                False,
                False,
                FocusTarget.RESULT_CANVAS,
            ),
        ),
        (
            "preview stale",
            ready_state(preview=PreviewState.STALE, stale_category="Framing"),
            (
                False,
                True,
                True,
                True,
                True,
                False,
                False,
                False,
                False,
                FocusTarget.PREVIEW_ACTION,
            ),
        ),
        (
            "preview error",
            ready_state(preview=PreviewState.ERROR, preview_error="failed"),
            (
                False,
                True,
                True,
                True,
                True,
                False,
                False,
                False,
                False,
                FocusTarget.PREVIEW_ACTION,
            ),
        ),
        (
            "render preflight warning",
            ready_state(
                preflight_warning=True,
                focus_target=FocusTarget.PREFLIGHT_DIALOG,
            ),
            (
                False,
                True,
                True,
                True,
                True,
                False,
                False,
                False,
                False,
                FocusTarget.PREFLIGHT_DIALOG,
            ),
        ),
        (
            "render running",
            running_render(job_id="j1"),
            (
                False,
                False,
                False,
                False,
                False,
                False,
                True,
                False,
                False,
                FocusTarget.JOB_DIALOG,
            ),
        ),
        (
            "rebuild running",
            ready_state(
                edited_cuts=True,
                job=ActiveJob(
                    job_id="j1",
                    kind=JobKind.REBUILD,
                    phase=JobState.RENDERING,
                    stage="Validate",
                    initiator_focus=FocusTarget.REBUILD_ACTION,
                ),
                focus_target=FocusTarget.JOB_DIALOG,
            ),
            (
                False,
                False,
                False,
                False,
                False,
                False,
                True,
                False,
                False,
                FocusTarget.JOB_DIALOG,
            ),
        ),
        (
            "cancelling",
            reduce(running_render(job_id="j1"), CancelRequested(job_id="j1")),
            (
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                FocusTarget.JOB_DIALOG,
            ),
        ),
        (
            "render complete",
            ready_state(
                artifact=ArtifactState.VALID,
                artifact_result=ArtifactResult(
                    source_id="source-1",
                    request_id="render-1",
                    value="cutout.webp",
                ),
                focus_target=FocusTarget.SUCCESS_BANNER,
            ),
            (
                False,
                True,
                True,
                True,
                True,
                False,
                False,
                True,
                True,
                FocusTarget.SUCCESS_BANNER,
            ),
        ),
        (
            "edited cuts",
            ready_state(
                artifact=ArtifactState.VALID,
                edited_cuts=True,
                focus_target=FocusTarget.REBUILD_ACTION,
            ),
            (
                False,
                True,
                True,
                True,
                True,
                True,
                False,
                True,
                True,
                FocusTarget.REBUILD_ACTION,
            ),
        ),
    ],
)
def test_state_matrix_derives_widget_capabilities_and_focus(
    name: str,
    state: AppState,
    expected: tuple[bool, bool, bool, bool, bool, bool, bool, bool, bool, FocusTarget],
) -> None:
    del name

    actual = capabilities(state)

    assert (
        actual.can_choose_source,
        actual.can_replace_source,
        actual.can_edit,
        actual.can_preview,
        actual.can_render,
        actual.can_rebuild,
        actual.can_cancel,
        actual.can_open_output,
        actual.can_open_folder,
        actual.focus_target,
    ) == expected


def test_source_results_must_match_the_loading_source_identity() -> None:
    loading = reduce(AppState(), SourceLoadRequested(source_id="new"))

    assert (
        reduce(loading, SourceLoaded(source_id="old", value="old metadata")) is loading
    )

    ready = reduce(loading, SourceLoaded(source_id="new", value="metadata"))

    assert ready.source is SourceState.READY
    assert ready.source_value == "metadata"
    assert capabilities(ready).focus_target is FocusTarget.PREVIEW_ACTION


def test_source_failure_exposes_recovery_focus() -> None:
    loading = reduce(AppState(), SourceLoadRequested(source_id="source-1"))

    failed = reduce(
        loading,
        SourceLoadFailed(source_id="source-1", error="unsupported codec"),
    )

    assert failed.source is SourceState.ERROR
    assert capabilities(failed).focus_target is FocusTarget.SOURCE_ERROR_HEADING


def test_preview_result_must_match_source_request_and_job_identity() -> None:
    running = reduce(
        ready_state(),
        PreviewRequested(job_id="job-1", request_id="request-1"),
    )

    wrong_source = PreviewSucceeded(
        job_id="job-1",
        result=preview_result(source_id="other"),
    )
    wrong_request = PreviewSucceeded(
        job_id="job-1",
        result=preview_result(request_id="other"),
    )

    assert reduce(running, wrong_source) is running
    assert reduce(running, wrong_request) is running

    current = reduce(
        running,
        PreviewSucceeded(job_id="job-1", result=preview_result()),
    )

    assert current.preview is PreviewState.CURRENT
    assert current.job.phase is JobState.IDLE
    assert capabilities(current).focus_target is FocusTarget.RESULT_CANVAS


def test_model_preparation_advances_only_the_active_preview_job() -> None:
    preparing = reduce(
        ready_state(model_available=False),
        PreviewRequested(
            job_id="job-1",
            request_id="request-1",
            requires_model_preparation=True,
        ),
    )

    assert preparing.job.phase is JobState.PREPARING_MODEL
    assert (
        reduce(preparing, ModelAvailabilityChanged(available=True)).job.phase
        is JobState.PREPARING_MODEL
    )

    previewing = reduce(
        reduce(preparing, ModelAvailabilityChanged(available=True)),
        JobStageChanged(job_id="job-1", stage="Segmentation"),
    )

    assert previewing.job.phase is JobState.PREVIEWING


def test_preview_failure_unlocks_only_after_matching_safe_terminal_event() -> None:
    running = reduce(
        ready_state(),
        PreviewRequested(job_id="job-1", request_id="request-1"),
    )
    stale_failure = PreviewFailed(
        job_id="old",
        source_id="source-1",
        request_id="request-1",
        error="crashed",
    )

    assert reduce(running, stale_failure) is running

    failed = reduce(
        running,
        PreviewFailed(
            job_id="job-1",
            source_id="source-1",
            request_id="request-1",
            error="crashed",
        ),
    )

    assert failed.preview is PreviewState.ERROR
    assert failed.job.phase is JobState.IDLE
    assert capabilities(failed).focus_target is FocusTarget.PREVIEW_ACTION


def test_settings_change_marks_a_current_preview_stale_without_deleting_it() -> None:
    result = preview_result()
    current = ready_state(
        preview=PreviewState.CURRENT,
        preview_result=result,
        preview_request_id="request-1",
    )

    stale = reduce(current, PreviewInvalidated(category="Framing"))

    assert stale.preview is PreviewState.STALE
    assert stale.preview_result is result
    assert stale.stale_category == "Framing"


def test_preflight_warning_does_not_start_or_queue_a_render() -> None:
    state = ready_state(preview=PreviewState.STALE)

    warning = reduce(state, RenderPreflightRequested())

    assert warning.job.phase is JobState.IDLE
    assert capabilities(warning).focus_target is FocusTarget.PREFLIGHT_DIALOG

    dismissed = reduce(warning, RenderPreflightDismissed())

    assert dismissed.job.phase is JobState.IDLE
    assert capabilities(dismissed).focus_target is FocusTarget.PREVIEW_ACTION


@pytest.mark.parametrize(
    ("event", "kind", "focus"),
    [
        (
            RenderRequested(job_id="job-1", request_id="render-1"),
            JobKind.RENDER,
            FocusTarget.RENDER_ACTION,
        ),
        (
            RebuildRequested(job_id="job-1", request_id="rebuild-1"),
            JobKind.REBUILD,
            FocusTarget.REBUILD_ACTION,
        ),
    ],
)
def test_render_and_rebuild_are_exclusive_and_restore_initiator_on_cancel(
    event: RenderRequested | RebuildRequested,
    kind: JobKind,
    focus: FocusTarget,
) -> None:
    initial = ready_state(
        preview=PreviewState.STALE,
        artifact=ArtifactState.VALID,
        edited_cuts=True,
    )
    running = reduce(initial, event)

    assert running.job.kind is kind
    assert not capabilities(running).can_edit

    cancelling = reduce(running, CancelRequested(job_id="job-1"))
    idle = reduce(cancelling, CancelAcknowledged(job_id="job-1"))

    assert capabilities(idle).focus_target is focus


def test_render_success_requires_all_three_matching_identities() -> None:
    running = reduce(
        ready_state(),
        RenderRequested(job_id="job-1", request_id="render-1"),
    )
    result = ArtifactResult(
        source_id="source-1",
        request_id="render-1",
        value="cutout.webp",
    )

    assert reduce(running, RenderSucceeded(job_id="old", result=result)) is running
    assert (
        reduce(
            running,
            RenderSucceeded(
                job_id="job-1",
                result=ArtifactResult("source-1", "old", "old.webp"),
            ),
        )
        is running
    )

    completed = reduce(running, RenderSucceeded(job_id="job-1", result=result))

    assert completed.artifact is ArtifactState.VALID
    assert completed.job.phase is JobState.IDLE
    assert capabilities(completed).focus_target is FocusTarget.SUCCESS_BANNER


def test_edited_cut_validation_error_focuses_the_recovery_action() -> None:
    state = reduce(
        ready_state(artifact=ArtifactState.VALID),
        EditedCutsChanged(detected=True, error="frame 0004 hash mismatch"),
    )

    assert not capabilities(state).can_rebuild
    assert capabilities(state).focus_target is FocusTarget.EDITED_CUT_RECOVERY


def test_cancelled_repreview_restores_the_previous_preview_identity() -> None:
    previous = preview_result(request_id="previous")
    current = ready_state(
        preview=PreviewState.CURRENT,
        preview_result=previous,
        preview_request_id="previous",
    )
    running = reduce(
        current,
        PreviewRequested(job_id="job-1", request_id="replacement"),
    )

    restored = reduce(
        reduce(running, CancelRequested(job_id="job-1")),
        CancelAcknowledged(job_id="job-1"),
    )

    assert restored.preview is PreviewState.CURRENT
    assert restored.preview_result is previous
    assert restored.preview_request_id == "previous"


def test_failed_rerender_preserves_access_to_the_previous_valid_artifact() -> None:
    previous = ArtifactResult("source-1", "previous", "previous.webp")
    running = reduce(
        ready_state(
            artifact=ArtifactState.VALID,
            artifact_result=previous,
        ),
        RenderRequested(job_id="job-1", request_id="replacement"),
    )

    failed = reduce(
        running,
        RenderFailed(
            job_id="job-1",
            source_id="source-1",
            request_id="replacement",
            error="encoder crashed",
        ),
    )

    assert failed.artifact is ArtifactState.VALID
    assert failed.artifact_result is previous
    assert capabilities(failed).can_open_output


def test_state_and_nested_job_are_immutable() -> None:
    state = running_preview(job_id="job-1")

    with pytest.raises(FrozenInstanceError):
        state.source = SourceState.EMPTY  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        state.job.stage = "Encode"  # type: ignore[misc]
