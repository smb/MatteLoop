"""Shared immutable input and request construction for preview and render."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from matteloop.core.parameters import (
    ParameterState,
    output_directory_for_source,
    output_filename_for_source,
)
from matteloop.core.specs import (
    CollisionPolicy,
    CropSpec,
    FramingSpec,
    OutputSpec,
    RenderRequest,
    SamplingSpec,
    SegmentationSpec,
)
from matteloop.core.timeline import TimelineState


@dataclass(frozen=True)
class _PreviewInputs:
    source: Path
    width: int
    height: int
    duration: Fraction
    start: Fraction
    end: Fraction
    playhead: Fraction
    crop: CropSpec
    parameters: ParameterState


def _preview_inputs(
    metadata: object,
    timeline: TimelineState | None = None,
    crop: CropSpec | None = None,
    parameters: ParameterState | None = None,
) -> _PreviewInputs:
    source = getattr(metadata, "path", None)
    width = getattr(metadata, "width", None)
    height = getattr(metadata, "height", None)
    duration = getattr(metadata, "duration", None)
    if (
        not isinstance(source, Path)
        or type(width) is not int
        or type(height) is not int
        or not isinstance(duration, Fraction)
    ):
        raise ValueError("loaded source metadata cannot build a preview request")
    selected = parameters or ParameterState()
    if timeline is None:
        return _PreviewInputs(
            source,
            width,
            height,
            duration,
            Fraction(0),
            duration,
            Fraction(0),
            crop or CropSpec(0, 0, width, height),
            selected,
        )
    if timeline.duration != duration:
        raise ValueError("timeline duration does not match loaded source metadata")
    return _PreviewInputs(
        source,
        width,
        height,
        duration,
        timeline.start,
        timeline.end,
        timeline.playhead,
        crop or CropSpec(0, 0, width, height),
        selected,
    )


def _render_request(
    inputs: _PreviewInputs,
    *,
    collision_policy: CollisionPolicy = CollisionPolicy.CANCEL,
) -> RenderRequest:
    parameters = inputs.parameters
    return RenderRequest(
        source=inputs.source,
        sampling=SamplingSpec(inputs.start, inputs.end, fps=parameters.fps),
        crop=inputs.crop,
        segmentation=SegmentationSpec(
            model_id=parameters.model_id,
            edge_mode=parameters.edge_mode,
            execution_provider=parameters.execution_provider,
        ),
        framing=FramingSpec(
            trim=parameters.trim,
            alpha_threshold=parameters.alpha_threshold,
            padding=parameters.padding,
            stretch_x=parameters.stretch_x,
        ),
        output=OutputSpec.from_mib(
            output_directory_for_source(parameters, inputs.source),
            output_filename_for_source(parameters, inputs.source),
            parameters.max_mib,
            collision_policy=collision_policy,
        ),
    )
