from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from rembggui.core.errors import ErrorCode, ValidationError
from rembggui.core.specs import (
    CollisionPolicy,
    CropSpec,
    EdgeMode,
    FramingSpec,
    OutputSpec,
    RenderRequest,
    SamplingSpec,
    SegmentationSpec,
)


def valid_render_request(tmp_path: Path) -> RenderRequest:
    return RenderRequest(
        source=tmp_path / "source.mp4",
        sampling=SamplingSpec(end=Fraction(1)),
        crop=CropSpec(x=0, y=0, width=128, height=128),
        segmentation=SegmentationSpec(),
        framing=FramingSpec(),
        output=OutputSpec(directory=tmp_path, filename="cutout.webp"),
    )


@pytest.mark.parametrize("fps", [0, -1, 241])
def test_sampling_rejects_fps_outside_gui_guard(fps: int) -> None:
    with pytest.raises(ValidationError) as exc:
        SamplingSpec(start=Fraction(0), end=Fraction(1), fps=fps)

    assert exc.value.code is ErrorCode.INVALID_SAMPLING


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (Fraction(-1), Fraction(1)),
        (Fraction(1), Fraction(1)),
        (Fraction(2), Fraction(1)),
    ],
)
def test_sampling_rejects_empty_or_backwards_half_open_intervals(
    start: Fraction, end: Fraction
) -> None:
    with pytest.raises(ValidationError) as exc:
        SamplingSpec(start=start, end=end)

    assert exc.value.code is ErrorCode.INVALID_SAMPLING


def test_sampling_preserves_start_inclusive_end_exclusive_semantics() -> None:
    sampling = SamplingSpec(end=Fraction(2), fps=15)

    assert sampling.start == Fraction(0)
    assert sampling.contains(Fraction(0))
    assert sampling.contains(Fraction(3, 2))
    assert not sampling.contains(Fraction(2))


def test_crop_must_be_positive_and_inside_oriented_source() -> None:
    with pytest.raises(ValidationError) as exc:
        CropSpec(x=90, y=0, width=20, height=10).validate_for(100, 100)

    assert exc.value.code is ErrorCode.INVALID_CROP


@pytest.mark.parametrize(
    "values",
    [
        (-1, 0, 1, 1),
        (0, -1, 1, 1),
        (0, 0, 0, 1),
        (0, 0, 1, 0),
    ],
)
def test_crop_rejects_negative_origin_or_empty_extent(
    values: tuple[int, int, int, int],
) -> None:
    with pytest.raises(ValidationError) as exc:
        CropSpec(*values).validate()

    assert exc.value.code is ErrorCode.INVALID_CROP


def test_crop_accepts_right_and_bottom_source_edges() -> None:
    crop = CropSpec(x=90, y=80, width=10, height=20)

    crop.validate_for(100, 100)


def test_segmentation_defaults_and_edge_mode_are_immutable() -> None:
    segmentation = SegmentationSpec()

    assert segmentation.model_id == "birefnet-portrait"
    assert segmentation.edge_mode is EdgeMode.STANDARD
    with pytest.raises(FrozenInstanceError):
        segmentation.model_id = "u2net"  # type: ignore[misc]


@pytest.mark.parametrize("threshold", [Decimal("-0.01"), Decimal("100.01")])
def test_framing_rejects_alpha_threshold_outside_percentage_range(
    threshold: Decimal,
) -> None:
    with pytest.raises(ValidationError) as exc:
        FramingSpec(alpha_threshold=threshold)

    assert exc.value.code is ErrorCode.INVALID_FRAMING


@pytest.mark.parametrize("padding", [-1, -10])
def test_framing_rejects_negative_padding(padding: int) -> None:
    with pytest.raises(ValidationError) as exc:
        FramingSpec(padding=padding)

    assert exc.value.code is ErrorCode.INVALID_FRAMING


@pytest.mark.parametrize("stretch_x", [Decimal("0"), Decimal("-0.01")])
def test_framing_rejects_non_positive_horizontal_stretch(stretch_x: Decimal) -> None:
    with pytest.raises(ValidationError) as exc:
        FramingSpec(stretch_x=stretch_x)

    assert exc.value.code is ErrorCode.INVALID_FRAMING


def test_framing_defaults_and_final_dimension_guards() -> None:
    framing = FramingSpec()

    assert framing.trim is False
    assert framing.alpha_threshold == Decimal("2.0")
    assert framing.padding == 0
    assert framing.stretch_x == Decimal("1.0")
    assert framing.validate_final_dimensions(128, 16383) == (128, 16383)

    for width, height in ((127, 128), (128, 127), (16384, 128), (128, 16384)):
        with pytest.raises(ValidationError) as exc:
            framing.validate_final_dimensions(width, height)
        assert exc.value.code is ErrorCode.INVALID_FINAL_DIMENSIONS


@pytest.mark.parametrize(
    "filename",
    ["", "nested/cutout.webp", "nested\\cutout.webp", "cutout.png", ".webp"],
)
def test_output_rejects_invalid_filename(filename: str, tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as exc:
        OutputSpec(directory=tmp_path, filename=filename)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT


def test_output_uses_decimal_mib_conversion_and_path() -> None:
    output = OutputSpec.from_mib(
        directory=Path("exports"),
        filename="cutout.webp",
        max_mib=Decimal("1.5"),
        collision_policy=CollisionPolicy.REPLACE,
    )

    assert output.max_bytes == 1_572_864
    assert output.path == Path("exports/cutout.webp")
    assert output.collision_policy is CollisionPolicy.REPLACE


@pytest.mark.parametrize("max_mib", [Decimal("-0.1"), Decimal("-1")])
def test_output_rejects_negative_maximum_size(max_mib: Decimal) -> None:
    with pytest.raises(ValidationError) as exc:
        OutputSpec.from_mib(Path("exports"), "cutout.webp", max_mib)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT


def test_render_request_is_frozen(tmp_path: Path) -> None:
    request = valid_render_request(tmp_path)

    with pytest.raises(FrozenInstanceError):
        request.sampling = SamplingSpec(end=Fraction(2), fps=15)  # type: ignore[misc]


def test_render_request_rejects_rebuild_and_regenerate_together(tmp_path: Path) -> None:
    request = valid_render_request(tmp_path)

    with pytest.raises(ValidationError) as exc:
        RenderRequest(
            source=request.source,
            sampling=request.sampling,
            crop=request.crop,
            segmentation=request.segmentation,
            framing=request.framing,
            output=request.output,
            rebuild=True,
            regenerate=True,
        )

    assert exc.value.code is ErrorCode.INVALID_RENDER_REQUEST


@pytest.mark.parametrize("source", [Path("."), Path("source.avi")])
def test_render_request_rejects_invalid_source_path(
    source: Path, tmp_path: Path
) -> None:
    request = valid_render_request(tmp_path)

    with pytest.raises(ValidationError) as exc:
        RenderRequest(
            source=source,
            sampling=request.sampling,
            crop=request.crop,
            segmentation=request.segmentation,
            framing=request.framing,
            output=request.output,
        )

    assert exc.value.code is ErrorCode.INVALID_RENDER_REQUEST
