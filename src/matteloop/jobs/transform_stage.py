"""Frame a cut and hand it to the encoder — the shared stage the result player
also calls (design decision D2), so encoder and preview stay pixel-identical.

``stage_encoder_frames`` validates and applies the request's ``TransformSpec``
(task A5): ``validate_for`` runs before the framed directory is created, only
the frames in ``transform.kept_range`` are staged, and the encoder's delays
are *sliced* from the full grid rather than recomputed — ``webp_delays``
(``core/timebase.py``) distributes rounding non-uniformly, so a recomputed
grid over the kept count would not equal the kept slice of the full grid.
``core.transform.apply_transform`` runs on each frame after ``apply_framing``;
for an identity spec it returns the same object, so an unmodified frame is
never re-saved with different bytes than before this stage existed.
"""

from __future__ import annotations

import dataclasses
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from matteloop.core.crop import clamp_crop
from matteloop.core.errors import ErrorCode, ValidationError
from matteloop.core.geometry import FramingPlan, PixelBounds, apply_framing
from matteloop.core.rgba import RgbaOwnershipTracker
from matteloop.core.specs import FramingSpec, TransformSpec
from matteloop.core.transform import apply_transform
from matteloop.jobs.context import JobContext
from matteloop.jobs.encoding import _map_output_os_error, _output_error


def framing_plan(
    source_size: tuple[int, int], union: PixelBounds | None, framing: FramingSpec
) -> FramingPlan:
    """Build the one immutable ``FramingPlan`` the encoder and the player share."""
    if framing.trim and union is None:
        raise ValidationError(
            ErrorCode.INVALID_FRAMING,
            "framing",
            "range-wide alpha union contains no visible pixels at this threshold",
        )
    return FramingPlan(
        source_size,
        global_bounds=union if framing.trim else None,
        padding=framing.padding,
        stretch_x=framing.stretch_x,
    )


def stage_encoder_frames(
    read_cut: Callable[[int, RgbaOwnershipTracker], Image.Image],
    frame_count: int,
    plan: FramingPlan,
    transform: TransformSpec,
    delays: tuple[int, ...],
    framed_directory: Path,
    tracker: RgbaOwnershipTracker,
    context: JobContext,
    overall: tuple[int, int],
) -> tuple[tuple[Path, ...], tuple[int, ...]]:
    """Frame, crop, and resize the kept cut frames and persist them.

    Validates *transform* against the cut before any file is created. Only
    the frames in ``transform.kept_range(frame_count)`` are staged; the
    returned delays are ``transform.select_kept(delays)`` — a slice of the
    full grid, never a recomputation.

    A crop stored while the framed size was larger — more padding, or a cut
    set promoted from a different video — is clamped to the current framed
    size here rather than rejected: this is the same ``clamp_crop`` the UI
    applies once cache facts arrive, so the encoded output matches what the
    UI later shows, and it covers rebuild, plain render, and trim paths that
    a UI-side clamp alone cannot reach.
    """
    if transform.crop is not None:
        transform = dataclasses.replace(
            transform, crop=clamp_crop(transform.crop, *plan.output_size)
        )
    final_size = transform.validate_for(frame_count, plan.output_size)
    tracker.include_size(final_size)
    try:
        framed_directory.mkdir(exist_ok=False)
    except OSError as error:
        raise _map_output_os_error(
            error, "cannot create framed input directory"
        ) from error
    kept = transform.kept_range(frame_count)
    kept_count = len(kept)
    framed_paths: list[Path] = []
    for position, index in enumerate(kept):
        context.set_frame_context(position + 1, kept_count, overall=overall)
        context.checkpoint("framing")
        framed_paths.append(
            _stage_frame(
                read_cut, index, position, plan, transform, framed_directory, tracker
            )
        )
        context.progress(
            "Framing",
            position + 1,
            total=kept_count,
            detail=f"Frame {position + 1} of {kept_count}",
            overall_completed=overall[0] + position + 1,
            overall_total=overall[1],
        )
    return tuple(framed_paths), transform.select_kept(delays)


def _stage_frame(
    read_cut: Callable[[int, RgbaOwnershipTracker], Image.Image],
    index: int,
    position: int,
    plan: FramingPlan,
    transform: TransformSpec,
    framed_directory: Path,
    tracker: RgbaOwnershipTracker,
) -> Path:
    cut = read_cut(index, tracker)
    try:
        framed = apply_framing(cut, plan)
        tracker.register(framed)
    finally:
        cut.close()
        del cut
    try:
        transformed = apply_transform(framed, transform)
        if transformed is not framed:
            tracker.register(transformed)
        try:
            path = framed_directory / f"frame-{position:06d}.png"
            _persist_framed_png(path, transformed)
            return path
        finally:
            if transformed is not framed:
                transformed.close()
                del transformed
    finally:
        framed.close()
        del framed


def _persist_framed_png(path: Path, image: Image.Image) -> None:
    temporary: Path | None = None
    primary: BaseException | None = None
    try:
        descriptor, raw = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        os.close(descriptor)
        temporary = Path(raw)
        image.save(temporary, format="PNG")
        with temporary.open("rb+") as encoded:  # Windows _commit needs write
            os.fsync(encoded.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as error:
        wrapped = _map_output_os_error(error, "cannot persist framed PNG")
        primary = wrapped
        raise wrapped from error
    except ValueError as error:
        wrapped = _output_error(f"cannot persist framed PNG: {error}")
        primary = wrapped
        raise wrapped from error
    except BaseException as error:
        primary = error
        raise
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError as error:
                if primary is not None:
                    primary.add_note(f"additional framed-PNG cleanup failure: {error}")
