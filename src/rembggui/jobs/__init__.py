"""Worker-safe media, job-lifecycle, and segmentation infrastructure."""

from rembggui.jobs.context import (
    CancellationState,
    ExclusiveJobScheduler,
    JobContext,
    JobTerminalState,
    ProgressEvent,
)
from rembggui.jobs.protocol import (
    PROTOCOL_VERSION,
    CancelAck,
    CancelRequest,
    SegmentRequest,
    SegmentResponse,
    Shutdown,
)
from rembggui.jobs.segmentation_host import SegmentationClient
from rembggui.jobs.source import (
    DecodedFrame,
    SourceInfo,
    SourceRevision,
    SourceValidationProof,
    decode_frame,
    probe_source,
)

__all__ = [
    "PROTOCOL_VERSION",
    "CancelAck",
    "CancelRequest",
    "CancellationState",
    "DecodedFrame",
    "ExclusiveJobScheduler",
    "JobContext",
    "JobTerminalState",
    "ProgressEvent",
    "SegmentRequest",
    "SegmentResponse",
    "SegmentationClient",
    "Shutdown",
    "SourceInfo",
    "SourceRevision",
    "SourceValidationProof",
    "decode_frame",
    "probe_source",
]
