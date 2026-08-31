"""Worker-safe media, job-lifecycle, and segmentation infrastructure."""

from matteloop.jobs.context import (
    CancellationState,
    ExclusiveJobScheduler,
    JobContext,
    JobTerminalState,
    ProgressEvent,
)
from matteloop.jobs.protocol import (
    PROTOCOL_VERSION,
    CancelAck,
    CancelRequest,
    SegmentRequest,
    SegmentResponse,
    Shutdown,
)
from matteloop.jobs.segmentation_host import SegmentationClient
from matteloop.jobs.source import (
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
