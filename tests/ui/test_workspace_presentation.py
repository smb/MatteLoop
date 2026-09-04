from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from matteloop.core.specs import (
    CollisionPolicy,
    CropSpec,
    FramingSpec,
    OutputSpec,
    RenderRequest,
    SamplingSpec,
    SegmentationSpec,
    TransformSpec,
)
from matteloop.ui.workspace_presentation import request_for_workspace


@dataclass(frozen=True)
class _FakeManifest:
    """Duck-typed stand-in exposing only what request_for_workspace reads."""

    cache_key_inputs: dict[str, object]
    source_path: str


def _manifest() -> _FakeManifest:
    return _FakeManifest(
        cache_key_inputs={
            "sampling": {
                "start": {"numerator": 0, "denominator": 1},
                "end": {"numerator": 4, "denominator": 1},
                "fps": 15,
            },
            "crop": {"x": 0, "y": 0, "width": 640, "height": 360},
            "model": {"id": "u2net"},
            "edge_settings": {
                "mode": "standard",
                "alpha_matting": {
                    "foreground_threshold": 240,
                    "background_threshold": 10,
                    "erode_size": 10,
                },
            },
        },
        source_path="/clips/holiday.mp4",
    )


def _base_request(transform: TransformSpec) -> RenderRequest:
    return RenderRequest(
        source=Path("/clips/holiday.mp4"),
        sampling=SamplingSpec(),
        crop=CropSpec(0, 0, 640, 360),
        segmentation=SegmentationSpec(),
        framing=FramingSpec(),
        output=OutputSpec.from_mib(
            Path("/exports"), "holiday.webp", Decimal(0), CollisionPolicy.CANCEL
        ),
        transform=transform,
    )


def test_request_for_workspace_forwards_the_base_transform() -> None:
    transform = TransformSpec(first_frame=2, last_frame=5)
    base = _base_request(transform)

    request = request_for_workspace(_manifest(), base)  # type: ignore[arg-type]

    assert request.transform == transform


def test_request_for_workspace_defaults_to_identity_when_base_is_identity() -> None:
    base = _base_request(TransformSpec())

    request = request_for_workspace(_manifest(), base)  # type: ignore[arg-type]

    assert request.transform == TransformSpec()
