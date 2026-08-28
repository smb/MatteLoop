"""Pure, spawn-safe domain contracts for rembgGUI."""

from rembggui.core.errors import AppError, ErrorCode, ValidationError
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

__all__ = [
    "AppError",
    "CollisionPolicy",
    "CropSpec",
    "EdgeMode",
    "ErrorCode",
    "FramingSpec",
    "OutputSpec",
    "RenderRequest",
    "SamplingSpec",
    "SegmentationSpec",
    "ValidationError",
]
