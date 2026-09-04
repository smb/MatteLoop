from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from matteloop.core.parameters import TransformChanged
from matteloop.core.specs import CropSpec, TransformSpec
from matteloop.core.state import (
    AppState,
    ArtifactResult,
    RenderRequested,
    RenderSucceeded,
    SourceLoaded,
    SourceLoadRequested,
    reduce,
)
from matteloop.ui.parameter_presentation import present_parameters


@dataclass(frozen=True)
class Metadata:
    path: Path
    width: int = 128
    height: int = 128
    duration: Fraction = Fraction(4)
    average_rate: Fraction = Fraction(30)


def test_parameter_presentation_uses_source_defaults_for_output() -> None:
    source = Path("/clips/holiday.mp4")
    loading = reduce(AppState(), SourceLoadRequested("source", "load"))
    state = reduce(loading, SourceLoaded("source", "load", Metadata(source)))

    presentation = present_parameters(state)

    assert presentation.output_directory == source.parent
    assert presentation.output_filename == "holiday.webp"
    assert presentation.duration == Fraction(4)
    assert presentation.fps == 15
    assert presentation.transform == TransformSpec()
    assert presentation.artifact is None


def test_parameter_presentation_exposes_the_transform_and_last_artifact() -> None:
    source = Path("/clips/holiday.mp4")
    loading = reduce(AppState(), SourceLoadRequested("source", "load"))
    ready = reduce(loading, SourceLoaded("source", "load", Metadata(source)))
    transform = TransformSpec(first_frame=1, crop=CropSpec(0, 0, 4, 4))
    with_transform = reduce(ready, TransformChanged(transform))
    running = reduce(with_transform, RenderRequested("job", "req"))
    artifact = ArtifactResult(
        source_id="source",
        request_id="req",
        output_path=Path("/exports/holiday.webp"),
        width=256,
        height=128,
        file_size=4096,
    )
    state = reduce(running, RenderSucceeded("job", artifact))

    presentation = present_parameters(state)

    assert presentation.transform == transform
    assert presentation.artifact == artifact
