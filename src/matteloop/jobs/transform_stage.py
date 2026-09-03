"""Frame a cut and hand it to the encoder — the shared stage the result player
also calls (design decision D2), so encoder and preview stay pixel-identical.

``stage_encoder_frames`` accepts a ``TransformSpec`` for contract compliance
(``docs/superpowers/plans/2026-09-04-issue-25-transform-stage.md`` section
4.3), but this module (task A4) only implements the identity path: the loop
still enumerates every stored frame and returns the delays unchanged. Slicing
to the transform's kept range and calling ``core.transform.apply_transform``
is task A5.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from matteloop.core.errors import ErrorCode, ValidationError
from matteloop.core.geometry import FramingPlan, PixelBounds, apply_framing
from matteloop.core.rgba import RgbaOwnershipTracker
from matteloop.core.specs import FramingSpec, TransformSpec
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
) -> tuple[tuple[Path, ...], tuple[int, ...]]:
    """Frame every stored cut frame and persist it for the encoder.

    Task A4 moves the loop verbatim; ``transform`` is accepted but not yet
    applied (task A5), so the kept range is always the full cut and the
    delays returned are the ones passed in, unchanged.
    """
    try:
        framed_directory.mkdir(exist_ok=False)
    except OSError as error:
        raise _map_output_os_error(
            error, "cannot create framed input directory"
        ) from error
    framed_paths: list[Path] = []
    for index in range(frame_count):
        context.set_frame_context(
            index + 1, frame_count, overall=context.overall_progress
        )
        context.checkpoint("framing")
        framed_paths.append(
            _stage_frame(
                read_cut, index, index, plan, transform, framed_directory, tracker
            )
        )
        context.progress(
            "Framing",
            index + 1,
            total=frame_count,
            detail=f"Frame {index + 1} of {frame_count}",
        )
    return tuple(framed_paths), delays


def _stage_frame(
    read_cut: Callable[[int, RgbaOwnershipTracker], Image.Image],
    index: int,
    position: int,
    plan: FramingPlan,
    transform: TransformSpec,
    framed_directory: Path,
    tracker: RgbaOwnershipTracker,
) -> Path:
    del transform  # applied starting in task A5
    cut = read_cut(index, tracker)
    try:
        framed = apply_framing(cut, plan)
        tracker.register(framed)
    finally:
        cut.close()
        del cut
    try:
        path = framed_directory / f"frame-{position:06d}.png"
        _persist_framed_png(path, framed)
        return path
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
