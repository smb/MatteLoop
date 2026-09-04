"""Behaviour of the shared framing/staging functions used by encoder and player."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from PIL import Image

from matteloop.core.errors import ErrorCode, ValidationError
from matteloop.core.geometry import FramingPlan, PixelBounds
from matteloop.core.rgba import RgbaOwnershipTracker
from matteloop.core.specs import CropSpec, FramingSpec, TransformSpec
from matteloop.core.state import JobKind
from matteloop.jobs.context import CancellationState, JobContext
from matteloop.jobs.transform_stage import framing_plan, stage_encoder_frames
from tests.jobs.render_support import job


def _colour_frame(colour: tuple[int, int, int, int]) -> Image.Image:
    return Image.new("RGBA", (128, 128), colour)


def test_framing_plan_matches_direct_construction_for_the_identity_framing() -> None:
    framing = FramingSpec(False, Decimal("2"), 0, Decimal("1"))
    plan = framing_plan((200, 180), None, framing)
    assert plan == FramingPlan(
        (200, 180), global_bounds=None, padding=0, stretch_x=Fraction(1)
    )


def test_framing_plan_rejects_trim_without_a_union() -> None:
    framing = FramingSpec(True, Decimal("2"), 0, Decimal("1"))
    with pytest.raises(ValidationError) as exc:
        framing_plan((200, 180), None, framing)
    assert exc.value.code is ErrorCode.INVALID_FRAMING


def test_framing_plan_accepts_trim_with_a_union() -> None:
    framing = FramingSpec(True, Decimal("2"), 0, Decimal("1"))
    union = PixelBounds(10, 10, 138, 138)
    plan = framing_plan((200, 180), union, framing)
    assert plan.global_bounds == union


def test_stage_encoder_frames_writes_the_framed_pixels_and_keeps_the_delays(
    tmp_path: Path,
) -> None:
    colours = [
        (255, 0, 0, 255),
        (0, 255, 0, 255),
        (0, 0, 255, 255),
    ]
    frames = [_colour_frame(colour) for colour in colours]

    def read_cut(index: int, tracker: RgbaOwnershipTracker) -> Image.Image:
        image = frames[index].copy()
        tracker.register(image)
        return image

    plan = FramingPlan((128, 128), global_bounds=None, padding=0, stretch_x=Fraction(1))
    delays = (100, 100, 100)
    tracker = RgbaOwnershipTracker((128, 128))
    events = []
    context = JobContext(
        "job-1",
        JobKind.RENDER,
        tmp_path / "job-work",
        events.append,
        CancellationState(),
    )

    paths, returned_delays = stage_encoder_frames(
        read_cut,
        3,
        plan,
        TransformSpec(),
        delays,
        tmp_path / "framed-inputs",
        tracker,
        context,
        (0, 6),
    )

    assert paths == tuple(
        tmp_path / "framed-inputs" / f"frame-{index:06d}.png" for index in range(3)
    )
    for path, frame in zip(paths, frames, strict=True):
        with Image.open(path) as saved:
            saved.load()
            assert saved.mode == "RGBA"
            assert saved.size == (128, 128)
            assert saved.tobytes() == frame.tobytes()
    assert returned_delays == delays
    assert [event.overall_completed for event in events] == [1, 2, 3]
    assert all(event.overall_total == 6 for event in events)


def test_stage_encoder_frames_clamps_a_crop_stored_against_a_larger_frame(
    tmp_path: Path,
) -> None:
    """A crop saved while the framed size was bigger (e.g. more padding, or a
    different cut set entirely) must rebuild against the *current* framed
    size instead of raising INVALID_TRANSFORM -- the UI-side clamp in
    ``restore_for`` only ever runs once cache facts have arrived, so a crop
    can reach the encoder unclamped on a rebuild, a plain render after a
    padding change, or a cut-set switch.
    """
    frames = [_colour_frame((255, 0, 0, 255))]

    def read_cut(index: int, tracker: RgbaOwnershipTracker) -> Image.Image:
        image = frames[index].copy()
        tracker.register(image)
        return image

    plan = FramingPlan((128, 128), global_bounds=None, padding=0, stretch_x=Fraction(1))
    tracker = RgbaOwnershipTracker((128, 128))
    context = job(tmp_path, "job-1", JobKind.RENDER)
    oversized_crop = CropSpec(0, 0, 200, 200)
    transform = TransformSpec(crop=oversized_crop)

    paths, _ = stage_encoder_frames(
        read_cut,
        1,
        plan,
        transform,
        (100,),
        tmp_path / "framed-inputs",
        tracker,
        context,
        (0, 2),
    )

    with Image.open(paths[0]) as saved:
        saved.load()
        assert saved.size == (128, 128)
