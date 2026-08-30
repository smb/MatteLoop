"""Exclusive heavy-job ownership, progress, and cancellation acknowledgement."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import Lock, RLock
from types import TracebackType

from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.state import JobKind
from rembggui.jobs.protocol import PROTOCOL_VERSION, CancelAck


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    job_id: str
    stage: str
    completed: int
    total: int | None = None
    detail: str = ""
    overall_completed: int | None = None
    overall_total: int | None = None


class JobTerminalState(StrEnum):
    RUNNING = "running"
    CANCEL_PENDING = "cancel_pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CancellationState:
    """Thread-safe cooperative cancellation state with one-way transitions."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._requested = False
        self._acknowledged = False

    @property
    def requested(self) -> bool:
        with self._lock:
            return self._requested

    @property
    def acknowledged(self) -> bool:
        with self._lock:
            return self._acknowledged

    def request(self) -> bool:
        with self._lock:
            if self._requested:
                return False
            self._requested = True
            return True

    def _acknowledge(self) -> bool:
        with self._lock:
            if not self._requested or self._acknowledged:
                return False
            self._acknowledged = True
            return True


class JobContext:
    """All mutable execution state belonging to one exclusive heavy job."""

    def __init__(
        self,
        job_id: str,
        kind: JobKind,
        workspace: Path,
        progress_sink: Callable[[ProgressEvent], None],
        cancellation: CancellationState,
        *,
        _terminal_sink: Callable[[JobContext], None] | None = None,
    ) -> None:
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job_id must be a non-empty string")
        if not isinstance(kind, JobKind):
            raise ValueError("kind must be a JobKind")
        if not isinstance(workspace, Path):
            raise ValueError("workspace must be a Path")
        self.job_id = job_id
        self.kind = kind
        self.workspace = workspace
        self.progress_sink = progress_sink
        self.cancellation = cancellation
        self._terminal_sink = _terminal_sink
        # Publication and terminal transitions share one reentrant lock.  The
        # sink may inspect or cancel this context synchronously, while another
        # thread cannot publish a terminal transition until the callback ends.
        self._lock = RLock()
        self._terminal_state = JobTerminalState.RUNNING
        self._overall_progress: tuple[int, int] | None = None

    @property
    def terminal_state(self) -> JobTerminalState:
        with self._lock:
            return self._terminal_state

    @property
    def overall_progress(self) -> tuple[int, int] | None:
        with self._lock:
            return self._overall_progress

    def progress(
        self,
        stage: str,
        completed: int,
        *,
        total: int | None = None,
        detail: str = "",
        overall_completed: int | None = None,
        overall_total: int | None = None,
    ) -> ProgressEvent:
        if not isinstance(stage, str) or not stage:
            raise ValueError("stage must be a non-empty string")
        if (
            not isinstance(completed, int)
            or isinstance(completed, bool)
            or completed < 0
        ):
            raise ValueError("completed must be a non-negative integer")
        if total is not None and (
            not isinstance(total, int) or isinstance(total, bool) or total < completed
        ):
            raise ValueError("total must be an integer at least completed")
        if not isinstance(detail, str):
            raise ValueError("detail must be a string")
        if (overall_completed is None) != (overall_total is None):
            raise ValueError(
                "overall_completed and overall_total must be supplied together"
            )
        if overall_completed is not None and (
            not isinstance(overall_completed, int)
            or isinstance(overall_completed, bool)
            or overall_completed < 0
            or overall_total is None
            or not isinstance(overall_total, int)
            or isinstance(overall_total, bool)
            or overall_total < overall_completed
        ):
            raise ValueError(
                "overall counts must be integers with total at least completed"
            )
        event = ProgressEvent(
            self.job_id,
            stage,
            completed,
            total,
            detail,
            overall_completed,
            overall_total,
        )
        with self._lock:
            if self._terminal_state not in {
                JobTerminalState.RUNNING,
                JobTerminalState.CANCEL_PENDING,
            }:
                raise RuntimeError("terminal jobs cannot report progress")
            if overall_completed is not None and overall_total is not None:
                self._overall_progress = (overall_completed, overall_total)
            self.progress_sink(event)
        return event

    def frame_progress(
        self,
        stage: str,
        completed: int,
        total: int,
        *,
        overall: tuple[int, int] | None = None,
    ) -> ProgressEvent:
        """Publish a counted frame event with the standard detail wording."""
        detail = f"{stage} frame {completed} of {total}"
        if overall is None:
            return self.progress(stage, completed, total=total, detail=detail)
        return self.progress(
            stage,
            completed,
            total=total,
            detail=detail,
            overall_completed=overall[0] + completed,
            overall_total=overall[1],
        )

    def request_cancel(self) -> bool:
        with self._lock:
            if self._terminal_state is not JobTerminalState.RUNNING:
                return False
            if not self.cancellation.request():
                return False
            self._terminal_state = JobTerminalState.CANCEL_PENDING
            return True

    def checkpoint(self, stage: str) -> None:
        """Acknowledge a local cancellation at a cooperative safe boundary."""
        if not isinstance(stage, str) or not stage:
            raise ValueError("stage must be a non-empty string")
        notify = False
        with self._lock:
            if self._terminal_state is JobTerminalState.RUNNING:
                return
            if self._terminal_state is JobTerminalState.CANCEL_PENDING:
                if not self.cancellation._acknowledge():
                    raise RuntimeError("pending cancellation could not be acknowledged")
                self._terminal_state = JobTerminalState.CANCELLED
                notify = True
            elif self._terminal_state is not JobTerminalState.CANCELLED:
                raise RuntimeError("terminal jobs have no cancellation checkpoints")
        if notify and self._terminal_sink is not None:
            self._terminal_sink(self)
        raise AppError(
            ErrorCode.JOB_CANCELLED,
            stage,
            "error.job.cancelled",
            f"job cancelled at the {stage} safe point",
            "retry-job",
            self.job_id,
        )

    def commit_if_not_cancelled[T](self, commit: Callable[[], T]) -> T:
        """Linearize the final irreversible commit against cancellation."""
        if not callable(commit):
            raise TypeError("commit must be callable")
        cancelled = False
        with self._lock:
            if self._terminal_state is JobTerminalState.CANCEL_PENDING:
                if not self.cancellation._acknowledge():
                    raise RuntimeError("pending cancellation could not be acknowledged")
                self._terminal_state = JobTerminalState.CANCELLED
                cancelled = True
            elif self._terminal_state is not JobTerminalState.RUNNING:
                raise RuntimeError("only running jobs may commit output")
            else:
                result = commit()
                self._terminal_state = JobTerminalState.SUCCEEDED
        if self._terminal_sink is not None:
            self._terminal_sink(self)
        if cancelled:
            raise AppError(
                ErrorCode.JOB_CANCELLED,
                "publish",
                "error.job.cancelled",
                "job cancelled before output publication committed",
                "retry-job",
                self.job_id,
            )
        return result

    def acknowledge_cancel(self, acknowledgement: CancelAck) -> bool:
        if (
            not isinstance(acknowledgement, CancelAck)
            or type(acknowledgement.protocol_version) is not int
            or acknowledgement.protocol_version != PROTOCOL_VERSION
            or acknowledgement.job_id != self.job_id
        ):
            return False
        with self._lock:
            if self._terminal_state is not JobTerminalState.CANCEL_PENDING:
                return False
            if not self.cancellation._acknowledge():
                return False
            self._terminal_state = JobTerminalState.CANCELLED
        if self._terminal_sink is not None:
            self._terminal_sink(self)
        return True

    def complete(self) -> bool:
        return self._finish(JobTerminalState.SUCCEEDED)

    def fail(self) -> bool:
        return self._finish(JobTerminalState.FAILED)

    def _finish(self, state: JobTerminalState) -> bool:
        with self._lock:
            if self._terminal_state is not JobTerminalState.RUNNING:
                return False
            self._terminal_state = state
        if self._terminal_sink is not None:
            self._terminal_sink(self)
        return True


class _JobLease(AbstractContextManager[JobContext]):
    def __init__(self, context: JobContext) -> None:
        self._context = context

    def __enter__(self) -> JobContext:
        return self._context

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        if exc_type is None:
            self._context.complete()
        else:
            self._context.fail()
        return None


def _discard_progress(_event: ProgressEvent) -> None:
    return


class ExclusiveJobScheduler:
    """Own exactly one modal heavy job until its safe terminal transition."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._active: JobContext | None = None

    @property
    def active(self) -> JobContext | None:
        with self._lock:
            return self._active

    def claim(
        self,
        kind: JobKind,
        job_id: str,
        *,
        workspace: Path | None = None,
        progress_sink: Callable[[ProgressEvent], None] = _discard_progress,
        cancellation: CancellationState | None = None,
    ) -> AbstractContextManager[JobContext]:
        with self._lock:
            if self._active is not None:
                raise AppError(
                    ErrorCode.JOB_ALREADY_RUNNING,
                    "job-scheduling",
                    "error.job.already-running",
                    f"job {self._active.job_id!r} is still active",
                    "wait-for-active-job",
                    job_id,
                )
            context = JobContext(
                job_id=job_id,
                kind=kind,
                workspace=workspace if workspace is not None else Path.cwd(),
                progress_sink=progress_sink,
                cancellation=(
                    cancellation if cancellation is not None else CancellationState()
                ),
                _terminal_sink=self._release,
            )
            self._active = context
        return _JobLease(context)

    def _release(self, context: JobContext) -> None:
        with self._lock:
            if self._active is context:
                self._active = None
