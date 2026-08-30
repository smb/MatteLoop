"""Output encoding helpers kept separate from the frozen render orchestration."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.rgba import RgbaOwnershipTracker
from rembggui.core.webp import EncodeSummary, fit_webp_to_size, validate_webp
from rembggui.jobs.context import JobContext


def auto_fit_webp(
    frame_paths: tuple[Path, ...],
    delays_ms: tuple[int, ...],
    destination: Path,
    work_dir: Path,
    max_bytes: int,
    context: JobContext,
    ownership: RgbaOwnershipTracker,
    frame_progress: Callable[[int, int], None],
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
