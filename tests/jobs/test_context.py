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


def test_progress_sink_is_reentrant_and_runs_outside_context_lock(
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
