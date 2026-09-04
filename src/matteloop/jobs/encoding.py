"""Output encoding helpers kept separate from the frozen render orchestration."""

from __future__ import annotations

import errno
from collections.abc import Callable
from pathlib import Path

from matteloop.core.errors import AppError, ErrorCode
from matteloop.core.rgba import RgbaOwnershipTracker
from matteloop.core.webp import EncodeSummary, fit_webp_to_size, validate_webp
from matteloop.jobs.context import JobContext


def auto_fit_webp(
    frame_paths: tuple[Path, ...],
    delays_ms: tuple[int, ...],
    destination: Path,
    work_dir: Path,
    max_bytes: int,
    context: JobContext,
    ownership: RgbaOwnershipTracker,
    frame_progress: Callable[[int, int], None],
    attempt_progress: Callable[[int, int], None] | None = None,
) -> EncodeSummary:
    """Fit, validate, and summarize one bounded-size WebP output."""
    fit_summaries: list[EncodeSummary] = []
    try:
        fit_webp_to_size(
            frame_paths,
            delays_ms,
            max_bytes,
            work_dir,
            destination,
            is_cancelled=lambda: context.cancellation.requested,
            rgba_ownership_tracker=ownership,
            summary_out=fit_summaries,
            progress=frame_progress,
            attempt_progress=attempt_progress,
        )
    except AppError as error:
        if error.code is ErrorCode.JOB_CANCELLED:
            context.checkpoint("auto-fit")
        raise
    if len(fit_summaries) != 1:
        raise AppError(
            ErrorCode.INVALID_OUTPUT,
            "output",
            "error.output.failed",
            "auto-fit did not return its final summary",
            "retry-output",
        )
    fitted_summary = fit_summaries[0]
    info = validate_webp(
        destination,
        fitted_summary.frames,
        sum(delays_ms) if len(frame_paths) > 1 else 0,
        rgba_ownership_tracker=ownership,
    )
    return EncodeSummary(
        destination,
        info.width,
        info.height,
        info.frames,
        info.duration_ms,
        info.file_size,
    )


def auto_fit_progress(
    context: JobContext, frame_total: int
) -> tuple[Callable[[int, int], None], Callable[[int, int], None]]:
    """Build frame and attempt callbacks for an indeterminate auto-fit job."""
    stage = "Auto-fit"

    def report_attempt(attempt: int, maximum: int) -> None:
        nonlocal stage
        stage = f"Auto-fit, attempt {attempt} of at most {maximum}"
        context.frame_progress(
            stage,
            0,
            frame_total,
            overall_indeterminate=True,
        )

    def report_frame(completed: int, total: int) -> None:
        context.frame_progress(
            stage,
            completed,
            total,
            overall_indeterminate=True,
        )

    return report_frame, report_attempt


def _map_output_os_error(error: OSError, detail: str) -> AppError:
    if error.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}:
        suffix = "disk quota or free space exhausted"
        action = "free-disk-space"
    elif error.errno in {errno.EACCES, errno.EPERM}:
        suffix = "output location is not writable"
        action = "choose-writable-output"
    elif error.errno == getattr(errno, "EROFS", -1):
        suffix = "output filesystem is read-only"
        action = "choose-writable-output"
    elif error.errno == errno.EEXIST:
        suffix = "output target already exists"
        action = "choose-collision-policy"
    else:
        suffix = f"{type(error).__name__}: {error}"
        action = "retry-output"
    return AppError(
        ErrorCode.INVALID_OUTPUT,
        "output",
        "error.output.failed",
        f"{detail}: {suffix}",
        action,
    )


def _output_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.INVALID_OUTPUT,
        "output",
        "error.output.failed",
        detail,
        "retry-output",
    )
