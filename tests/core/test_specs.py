from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

import rembggui.core.specs as core_specs
from rembggui.core.errors import ErrorCode, ValidationError
from rembggui.core.specs import (
    MAX_ALPHA_MATTING_ERODE_SIZE,
    AlphaMattingSpec,
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
    sampling = SamplingSpec(end=Fraction(2))

    assert sampling.start == Fraction(0)
    assert sampling.contains(Fraction(0))
    assert sampling.contains(Fraction(3, 2))
    assert not sampling.contains(Fraction(2))
    assert sampling.fps == 15


def test_sampling_public_positional_order_is_start_end_fps() -> None:
    sampling = SamplingSpec(Fraction(0), Fraction(2), 15)

    assert sampling.start == Fraction(0)
    assert sampling.end == Fraction(2)
    assert sampling.fps == 15


@pytest.mark.parametrize(
    ("start", "end", "duration", "accepted"),
    [
        (Fraction(0), Fraction(2), Fraction(2), True),
        (Fraction(1), Fraction(2), Fraction(2), True),
        (Fraction(2), Fraction(3), Fraction(2), False),
        (Fraction(0), Fraction(3), Fraction(2), False),
    ],
)
def test_sampling_validates_interval_against_source_duration(
    start: Fraction, end: Fraction, duration: Fraction, accepted: bool
) -> None:
    sampling = SamplingSpec(start, end, 15)

    if accepted:
        sampling.validate_for_duration(duration)
    else:
        with pytest.raises(ValidationError) as exc:
            sampling.validate_for_duration(duration)
        assert exc.value.code is ErrorCode.INVALID_SAMPLING


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
        CropSpec(*values)

    assert exc.value.code is ErrorCode.INVALID_CROP


def test_crop_accepts_right_and_bottom_source_edges() -> None:
    crop = CropSpec(x=90, y=80, width=10, height=20)

    crop.validate_for(100, 100)


@pytest.mark.parametrize(
    ("crop", "source_width", "source_height"),
    [
        (CropSpec(x=91, y=0, width=10, height=10), 100, 100),
        (CropSpec(x=0, y=91, width=10, height=10), 100, 100),
    ],
)
def test_crop_rejects_right_and_bottom_overflow_independently(
    crop: CropSpec, source_width: int, source_height: int
) -> None:
    with pytest.raises(ValidationError) as exc:
        crop.validate_for(source_width, source_height)

    assert exc.value.code is ErrorCode.INVALID_CROP


def test_segmentation_defaults_and_edge_mode_are_immutable() -> None:
    segmentation = SegmentationSpec()

    assert segmentation.model_id == "birefnet-portrait"
    assert segmentation.edge_mode is EdgeMode.STANDARD
    with pytest.raises(FrozenInstanceError):
        segmentation.model_id = "u2net"  # type: ignore[misc]


def test_alpha_matting_defaults_match_the_pinned_rembg_api() -> None:
    matting = AlphaMattingSpec()

    assert matting.foreground_threshold == 240
    assert matting.background_threshold == 10
    assert matting.erode_size == 10
    assert SegmentationSpec().alpha_matting == matting
    with pytest.raises(FrozenInstanceError):
        matting.foreground_threshold = 200  # type: ignore[misc]


@pytest.mark.parametrize("erode_size", [0, 10, MAX_ALPHA_MATTING_ERODE_SIZE])
def test_alpha_matting_accepts_bounded_erosion(erode_size: int) -> None:
    assert AlphaMattingSpec(erode_size=erode_size).erode_size == erode_size


@pytest.mark.parametrize(
    "values",
    [
        (0, 10, 10),
        (256, 10, 10),
        (240, -1, 10),
        (240, 256, 10),
        (10, 10, 10),
        (240, 10, -1),
        (240, 10, MAX_ALPHA_MATTING_ERODE_SIZE + 1),
        (240, 10, 2**63),
        (240, 10, True),
    ],
)
def test_alpha_matting_rejects_invalid_thresholds_and_erosion(
    values: tuple[int, int, int],
) -> None:
    with pytest.raises(ValidationError) as exc:
        AlphaMattingSpec(*values)

    assert exc.value.code is ErrorCode.INVALID_SEGMENTATION


@pytest.mark.parametrize(
    "threshold",
    [
        Decimal("-0.01"),
        Decimal("100.01"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_framing_rejects_alpha_threshold_outside_percentage_range(
    threshold: Decimal,
) -> None:
    with pytest.raises(ValidationError) as exc:
        FramingSpec(alpha_threshold=threshold)

    assert exc.value.code is ErrorCode.INVALID_FRAMING


@pytest.mark.parametrize("threshold", [Decimal("0"), Decimal("100")])
def test_framing_accepts_alpha_threshold_percentage_boundaries(
    threshold: Decimal,
) -> None:
    assert FramingSpec(alpha_threshold=threshold).alpha_threshold == threshold


@pytest.mark.parametrize("padding", [-1, -10])
def test_framing_rejects_negative_padding(padding: int) -> None:
    with pytest.raises(ValidationError) as exc:
        FramingSpec(padding=padding)

    assert exc.value.code is ErrorCode.INVALID_FRAMING


@pytest.mark.parametrize(
    "stretch_x",
    [
        Decimal("0"),
        Decimal("-0.01"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
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


def test_output_rejects_embedded_nul_filename(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as exc:
        OutputSpec(directory=tmp_path, filename="cut\x00out.webp")

    assert exc.value.code is ErrorCode.INVALID_OUTPUT


@pytest.mark.parametrize("filename", ["CON.webp", "cut?out.webp", "C:cutout.webp"])
def test_output_rejects_windows_impossible_filename_on_any_host(
    filename: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core_specs.os, "name", "nt")

    with pytest.raises(ValidationError) as exc:
        OutputSpec(directory=tmp_path, filename=filename)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT


@pytest.mark.parametrize("filename", ["cut\x01out.webp", "CONIN$.webp", "CONOUT$.webp"])
def test_output_rejects_standard_windows_reserved_filename_on_any_host(
    filename: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core_specs.os, "name", "nt")

    with pytest.raises(ValidationError) as exc:
        OutputSpec(directory=tmp_path, filename=filename)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT


@pytest.mark.skipif(os.name == "nt", reason="Windows filename rules apply on Windows")
@pytest.mark.parametrize("filename", ["CON.webp", "cut?out.webp", "C:cutout.webp"])
def test_output_preserves_valid_posix_filename_characters(
    filename: str, tmp_path: Path
) -> None:
    output = OutputSpec(directory=tmp_path, filename=filename)

    assert output.filename == filename


@pytest.mark.parametrize(
    "directory",
    [
        Path("https://example.test/exports"),
        Path(r"https:\example.test\exports"),
        Path("//server/share/exports"),
        Path(r"\\server\share\exports"),
    ],
)
def test_output_rejects_non_local_directory_syntax(directory: Path) -> None:
    with pytest.raises(ValidationError) as exc:
        OutputSpec(directory=directory, filename="cutout.webp")

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


def test_output_zero_decimal_mib_means_no_size_limit() -> None:
    output = OutputSpec.from_mib(Path("."), "cutout.webp", Decimal("0"))

    assert output.max_bytes is None


@pytest.mark.parametrize(
    "max_mib", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")]
)
def test_output_rejects_non_finite_maximum_size(max_mib: Decimal) -> None:
    with pytest.raises(ValidationError) as exc:
        OutputSpec.from_mib(Path("."), "cutout.webp", max_mib)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT


@pytest.mark.parametrize("max_mib", [Decimal("-0.1"), Decimal("-1")])
def test_output_rejects_negative_maximum_size(max_mib: Decimal) -> None:
    with pytest.raises(ValidationError) as exc:
        OutputSpec.from_mib(Path("exports"), "cutout.webp", max_mib)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT


def test_render_request_is_frozen(tmp_path: Path) -> None:
    request = valid_render_request(tmp_path)

    with pytest.raises(FrozenInstanceError):
        request.sampling = SamplingSpec(Fraction(0), Fraction(2), 15)  # type: ignore[misc]


@pytest.mark.parametrize(
    ("rebuild", "regenerate", "accepted"),
    [
        (False, False, True),
        (True, False, True),
        (False, True, True),
        (True, True, False),
    ],
)
def test_render_request_validates_all_rebuild_and_regenerate_combinations(
    tmp_path: Path, rebuild: bool, regenerate: bool, accepted: bool
) -> None:
    request = valid_render_request(tmp_path)

    if accepted:
        rendered = RenderRequest(
            source=request.source,
            sampling=request.sampling,
            crop=request.crop,
            segmentation=request.segmentation,
            framing=request.framing,
            output=request.output,
            rebuild=rebuild,
            regenerate=regenerate,
        )
        assert rendered.rebuild is rebuild
        assert rendered.regenerate is regenerate
    else:
        with pytest.raises(ValidationError) as exc:
            RenderRequest(
                source=request.source,
                sampling=request.sampling,
                crop=request.crop,
                segmentation=request.segmentation,
                framing=request.framing,
                output=request.output,
                rebuild=rebuild,
                regenerate=regenerate,
            )

        assert exc.value.code is ErrorCode.INVALID_RENDER_REQUEST


@pytest.mark.parametrize(
    "source",
    [
        Path("."),
        Path("source.avi"),
        Path("https://example.test/source.mp4"),
        Path(r"https:\example.test\source.mp4"),
        Path(r"file:\example.test\source.mp4"),
        Path("//server/share/source.mp4"),
        Path(r"\\server\share\source.mp4"),
    ],
)
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


def test_render_request_accepts_windows_drive_source_syntax(tmp_path: Path) -> None:
    request = valid_render_request(tmp_path)
    source = Path(r"C:\videos\source.mp4")

    rendered = RenderRequest(
        source=source,
        sampling=request.sampling,
        crop=request.crop,
        segmentation=request.segmentation,
        framing=request.framing,
        output=request.output,
    )

    assert rendered.source == source


def test_construction_defers_filesystem_path_preflight(tmp_path: Path) -> None:
    request = valid_render_request(tmp_path)

    assert request.source == tmp_path / "source.mp4"


def test_job_path_preflight_requires_readable_regular_source_file(
    tmp_path: Path,
) -> None:
    request = valid_render_request(tmp_path)

    with pytest.raises(ValidationError) as exc:
        request.preflight_job_paths()

    assert exc.value.code is ErrorCode.INVALID_RENDER_REQUEST


def test_job_path_preflight_accepts_regular_source_and_dot_output_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.touch()
    monkeypatch.chdir(tmp_path)
    request = RenderRequest(
        source=source,
        sampling=SamplingSpec(),
        crop=CropSpec(0, 0, 128, 128),
        segmentation=SegmentationSpec(),
        framing=FramingSpec(),
        output=OutputSpec(Path("."), "cutout.webp"),
    )

    request.preflight_job_paths()


def test_job_path_preflight_requires_an_existing_usable_output_directory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.touch()
    request = RenderRequest(
        source=source,
        sampling=SamplingSpec(),
        crop=CropSpec(0, 0, 128, 128),
        segmentation=SegmentationSpec(),
        framing=FramingSpec(),
        output=OutputSpec(tmp_path / "missing", "cutout.webp"),
    )

    with pytest.raises(ValidationError) as exc:
        request.preflight_job_paths()

    assert exc.value.code is ErrorCode.INVALID_OUTPUT


def test_source_validation_combines_duration_crop_and_final_dimensions(
    tmp_path: Path,
) -> None:
    request = valid_render_request(tmp_path)

    assert request.validate_for_source(128, 128, Fraction(1)) == (128, 128)

    too_long = RenderRequest(
        source=request.source,
        sampling=SamplingSpec(Fraction(0), Fraction(2), 15),
        crop=request.crop,
        segmentation=request.segmentation,
        framing=request.framing,
        output=request.output,
    )
    with pytest.raises(ValidationError) as exc:
        too_long.validate_for_source(128, 128, Fraction(1))

    assert exc.value.code is ErrorCode.INVALID_SAMPLING
