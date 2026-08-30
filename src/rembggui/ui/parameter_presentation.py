"""Qt-free presentation values and conversions for inspector parameters."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from rembggui.core.parameters import (
    output_directory_for_source,
    output_filename_for_source,
)
from rembggui.core.specs import EdgeMode
from rembggui.core.state import AppState


@dataclass(frozen=True, slots=True)
class ParameterPresentation:
    model_id: str
    edge_mode: EdgeMode
    execution_provider: str
    fps: int
    trim: bool
    alpha_threshold: Decimal
    padding: int
    stretch_x: Decimal
    output_directory: Path | None
    output_filename: str
    max_mib: Decimal
    start: Fraction | None
    end: Fraction | None
    duration: Fraction | None
    source_duration: Fraction | None


def present_parameters(state: AppState) -> ParameterPresentation:
    """Map reducer-owned settings to values the standard widgets can display."""
    parameters = state.parameters
    source = getattr(state.source_value, "path", None)
    source_path = source if isinstance(source, Path) else None
    timeline = state.timeline
    filename = (
        output_filename_for_source(parameters, source_path)
        if source_path is not None
        else parameters.output_filename or ""
    )
    directory = (
        output_directory_for_source(parameters, source_path)
        if source_path is not None
        else parameters.output_directory
    )
    return ParameterPresentation(
        model_id=parameters.model_id,
        edge_mode=parameters.edge_mode,
        execution_provider=parameters.execution_provider,
        fps=timeline.fps if timeline is not None else parameters.fps,
        trim=parameters.trim,
        alpha_threshold=parameters.alpha_threshold,
        padding=parameters.padding,
        stretch_x=parameters.stretch_x,
        output_directory=directory,
        output_filename=filename,
        max_mib=parameters.max_mib,
        start=timeline.start if timeline is not None else None,
        end=timeline.end if timeline is not None else None,
        duration=timeline.end - timeline.start if timeline is not None else None,
        source_duration=timeline.duration if timeline is not None else None,
    )


def fraction_from_widget_value(value: float) -> Fraction:
    """Convert a decimal widget value without binary-float arithmetic."""
    return Fraction(str(value))


def decimal_from_widget_value(value: float) -> Decimal:
    """Convert a decimal widget value without binary-float drift."""
    return Decimal(str(value))
