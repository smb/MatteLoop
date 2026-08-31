from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from matteloop.core.state import AppState, SourceLoaded, SourceLoadRequested, reduce
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
