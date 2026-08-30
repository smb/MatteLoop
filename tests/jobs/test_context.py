from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

import pytest

from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.state import JobKind
from rembggui.jobs.context import (
    CancellationState,
    ExclusiveJobScheduler,
    JobContext,
    JobTerminalState,
    ProgressEvent,
)
from rembggui.jobs.protocol import PROTOCOL_VERSION, CancelAck


def test_scheduler_rejects_second_heavy_job(tmp_path: Path) -> None:
    scheduler = ExclusiveJobScheduler()
    with scheduler.claim(JobKind.PREVIEW, "j1", workspace=tmp_path):
        with pytest.raises(AppError) as exc:
            scheduler.claim(JobKind.RENDER, "j2", workspace=tmp_path)
    assert exc.value.code is ErrorCode.JOB_ALREADY_RUNNING
    assert scheduler.active is None


def test_context_emits_validated_progress_and_completes_once(tmp_path: Path) -> None:
    events: list[ProgressEvent] = []
    scheduler = ExclusiveJobScheduler()
    with scheduler.claim(
        JobKind.RENDER, "j1", workspace=tmp_path, progress_sink=events.append
    ) as context:
        event = context.progress("decode", 2, total=5, detail="Frame 2 of 5")
        assert event == events[0]
        assert context.complete()
        assert not context.complete()
    assert context.terminal_state is JobTerminalState.SUCCEEDED
    assert scheduler.active is None


@pytest.mark.parametrize(("completed", "total"), [(-1, None), (2, 1), (True, None)])
def test_progress_rejects_invalid_counts(
    tmp_path: Path, completed: int, total: int | None
) -> None:
    scheduler = ExclusiveJobScheduler()
    with scheduler.claim(JobKind.PREVIEW, "j1", workspace=tmp_path) as context:
        with pytest.raises(ValueError):
            context.progress("decode", completed, total=total)


def test_cancel_is_idempotent_and_only_matching_ack_unlocks(tmp_path: Path) -> None:
    cancellation = CancellationState()
    scheduler = ExclusiveJobScheduler()
    lease = scheduler.claim(
        JobKind.RENDER,
        "j1",
        workspace=tmp_path,
        cancellation=cancellation,
    )
    context = lease.__enter__()

    assert context.request_cancel()
    assert not context.request_cancel()
    assert cancellation.requested
    assert not context.acknowledge_cancel(CancelAck(PROTOCOL_VERSION, "other"))
    assert scheduler.active is context
    assert not context.acknowledge_cancel(CancelAck(PROTOCOL_VERSION + 1, "j1"))
    assert scheduler.active is context

    assert context.acknowledge_cancel(CancelAck(PROTOCOL_VERSION, "j1"))
    assert context.terminal_state is JobTerminalState.CANCELLED
    assert cancellation.acknowledged
    assert scheduler.active is None
    assert not context.acknowledge_cancel(CancelAck(PROTOCOL_VERSION, "j1"))
    lease.__exit__(None, None, None)


def test_cancel_pending_context_manager_exit_remains_claimed_until_ack(
    tmp_path: Path,
) -> None:
    scheduler = ExclusiveJobScheduler()
    with scheduler.claim(JobKind.REBUILD, "j1", workspace=tmp_path) as context:
        context.request_cancel()

    assert scheduler.active is context
    with pytest.raises(AppError) as exc:
        scheduler.claim(JobKind.PREVIEW, "j2", workspace=tmp_path)
    assert exc.value.code is ErrorCode.JOB_ALREADY_RUNNING
    assert context.acknowledge_cancel(CancelAck(PROTOCOL_VERSION, "j1"))
    assert scheduler.active is None


def test_exception_marks_non_cancelled_context_failed(tmp_path: Path) -> None:
    scheduler = ExclusiveJobScheduler()
    with pytest.raises(RuntimeError, match="boom"):
        with scheduler.claim(JobKind.RENDER, "j1", workspace=tmp_path) as context:
            raise RuntimeError("boom")
    assert context.terminal_state is JobTerminalState.FAILED
    assert scheduler.active is None


def test_job_context_supports_the_public_five_argument_constructor(
    tmp_path: Path,
) -> None:
    context = JobContext(
        "direct",
        JobKind.PREVIEW,
        tmp_path,
        lambda _event: None,
        CancellationState(),
    )
    assert context.job_id == "direct"
    assert context.complete()
    assert context.terminal_state is JobTerminalState.SUCCEEDED


def test_progress_sink_is_reentrant_through_publication_lock(
    tmp_path: Path,
) -> None:
    callback_finished = Event()
    holder: list[JobContext] = []

    def sink(_event: ProgressEvent) -> None:
        context = holder[0]
        assert context.terminal_state is JobTerminalState.RUNNING
        assert context.request_cancel()
        callback_finished.set()

    context = JobContext(
        "direct",
        JobKind.RENDER,
        tmp_path,
        sink,
        CancellationState(),
    )
    holder.append(context)
    thread = Thread(target=lambda: context.progress("segment", 0))
    thread.start()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert callback_finished.is_set()
    assert context.terminal_state is JobTerminalState.CANCEL_PENDING


def test_terminal_transition_waits_for_inflight_progress_publication(
    tmp_path: Path,
) -> None:
    sink_entered = Event()
    release_sink = Event()
    terminal_finished = Event()
    observed: list[str] = []

    def sink(_event: ProgressEvent) -> None:
        sink_entered.set()
        assert release_sink.wait(timeout=1)
        observed.append("progress")

    context = JobContext(
        "direct",
        JobKind.RENDER,
        tmp_path,
        sink,
        CancellationState(),
    )
    progress_thread = Thread(target=lambda: context.progress("segment", 1))

    def complete() -> None:
        assert context.complete()
        observed.append("terminal")
        terminal_finished.set()

    terminal_thread = Thread(target=complete)
    progress_thread.start()
    assert sink_entered.wait(timeout=1)
    terminal_thread.start()
    assert not terminal_finished.is_set()
    release_sink.set()
    progress_thread.join(timeout=1)
    terminal_thread.join(timeout=1)
    assert observed == ["progress", "terminal"]
    with pytest.raises(RuntimeError, match="terminal"):
        context.progress("late", 2)


def test_cancel_ack_rejects_bool_protocol_version(tmp_path: Path) -> None:
    scheduler = ExclusiveJobScheduler()
    lease = scheduler.claim(JobKind.RENDER, "j1", workspace=tmp_path)
    context = lease.__enter__()
    assert context.request_cancel()
    assert not context.acknowledge_cancel(CancelAck(True, "j1"))
    assert scheduler.active is context
    assert context.acknowledge_cancel(CancelAck(PROTOCOL_VERSION, "j1"))
    lease.__exit__(None, None, None)


def test_local_checkpoint_acknowledges_cancel_and_raises_structured_error(
    tmp_path: Path,
) -> None:
    scheduler = ExclusiveJobScheduler()
    lease = scheduler.claim(JobKind.RENDER, "j1", workspace=tmp_path)
    context = lease.__enter__()
    assert context.request_cancel()

    with pytest.raises(AppError) as exc:
        context.checkpoint("after-segment")

    assert exc.value.code is ErrorCode.JOB_CANCELLED
    assert context.terminal_state is JobTerminalState.CANCELLED
    assert context.cancellation.acknowledged
    assert scheduler.active is None
    lease.__exit__(AppError, exc.value, None)


def test_commit_if_not_cancelled_linearizes_publish_before_late_cancel(
    tmp_path: Path,
) -> None:
    context = JobContext(
        "j1",
        JobKind.RENDER,
        tmp_path,
        lambda _event: None,
        CancellationState(),
    )
    commit_entered = Event()
    release_commit = Event()
    cancel_results: list[bool] = []

    def commit() -> str:
        commit_entered.set()
        assert release_commit.wait(timeout=1)
        return "published"

    committed: list[str] = []
    commit_thread = Thread(
        target=lambda: committed.append(context.commit_if_not_cancelled(commit))
    )
    commit_thread.start()
    assert commit_entered.wait(timeout=1)
    cancel_thread = Thread(
        target=lambda: cancel_results.append(context.request_cancel())
    )
    cancel_thread.start()
    release_commit.set()
    commit_thread.join(timeout=1)
    cancel_thread.join(timeout=1)

    assert committed == ["published"]
    assert cancel_results == [False]
    assert context.terminal_state is JobTerminalState.SUCCEEDED


def test_cancel_wins_before_commit_and_publish_is_never_called(tmp_path: Path) -> None:
    context = JobContext(
        "j1",
        JobKind.RENDER,
        tmp_path,
        lambda _event: None,
        CancellationState(),
    )
    called = False

    def commit() -> None:
        nonlocal called
        called = True

    assert context.request_cancel()
    with pytest.raises(AppError) as exc:
        context.commit_if_not_cancelled(commit)

    assert exc.value.code is ErrorCode.JOB_CANCELLED
    assert not called
    assert context.terminal_state is JobTerminalState.CANCELLED
