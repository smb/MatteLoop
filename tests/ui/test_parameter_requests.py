from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from rembggui.core.parameters import ParameterState
from rembggui.core.specs import EdgeMode
from rembggui.core.timeline import TimelineState
from rembggui.ui.preview_controller import _preview_inputs, _render_request


@dataclass(frozen=True)
class Metadata:
    path: Path
    width: int = 640
    height: int = 360
    duration: Fraction = Fraction(4)


def test_preview_and_render_requests_share_every_inspector_parameter() -> None:
    source = Path("/clips/holiday.mp4")
    parameters = ParameterState(
        model_id="u2net",
        edge_mode=EdgeMode.DECONTAMINATE_COLORS,
        fps=120,
        trim=True,
        alpha_threshold=Decimal("4.5"),
        padding=6,
        stretch_x=Decimal("1.25"),
        output_directory=Path("/exports"),
        output_filename="holiday-cut.webp",
        max_mib=Decimal("12.5"),
    )
    timeline = TimelineState(
        Fraction(4), Fraction(1, 2), Fraction(7, 2), Fraction(1), fps=120
    )

    inputs = _preview_inputs(Metadata(source), timeline, parameters=parameters)
    request = _render_request(inputs)

    assert request.sampling == timeline_to_sampling(timeline)
    assert request.segmentation.model_id == parameters.model_id
    assert request.segmentation.edge_mode is parameters.edge_mode
    assert request.framing.trim is parameters.trim
    assert request.framing.alpha_threshold == parameters.alpha_threshold
    assert request.framing.padding == parameters.padding
    assert request.framing.stretch_x == parameters.stretch_x
    assert request.output.path == Path("/exports/holiday-cut.webp")
    assert request.output.max_bytes == int(Decimal("12.5") * 1024 * 1024)


def timeline_to_sampling(timeline: TimelineState):
    from rembggui.core.specs import SamplingSpec

    return SamplingSpec(timeline.start, timeline.end, timeline.fps)
