"""Spawn-serializable messages for the segmentation process boundary."""

from __future__ import annotations

from dataclasses import dataclass

PROTOCOL_VERSION = 1
CONTROL_JOB_ID = "__control__"

type FrameShape = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class SharedFrame:
    """A byte-exact view into the parent-owned shared-memory slot."""

    name: str
    shape: FrameShape
    dtype: str
    byte_length: int


@dataclass(frozen=True, slots=True)
class SegmentRequest:
    """One request; the client fills ``slot`` immediately before transport."""

    protocol_version: int
    job_id: str
    request_id: str
    slot: SharedFrame | None = None


@dataclass(frozen=True, slots=True)
class SegmentResponse:
    """Metadata describing an RGBA result written back into the same slot."""

    protocol_version: int
    job_id: str
    request_id: str
    shape: FrameShape
    dtype: str
    byte_length: int


@dataclass(frozen=True, slots=True)
class SegmentFailure:
    """A primitive ``AppError`` payload emitted after a handled child failure."""

    protocol_version: int
    job_id: str
    request_id: str
    error: dict[str, str | None]


@dataclass(frozen=True, slots=True)
class CancelRequest:
    protocol_version: int
    job_id: str


@dataclass(frozen=True, slots=True)
class CancelAck:
    protocol_version: int
    job_id: str


@dataclass(frozen=True, slots=True)
class Shutdown:
    protocol_version: int
    job_id: str = CONTROL_JOB_ID


@dataclass(frozen=True, slots=True)
class WorkerReady:
    protocol_version: int
    job_id: str
    process_id: int


type ParentMessage = SegmentRequest | CancelRequest | Shutdown
type ChildMessage = WorkerReady | SegmentResponse | SegmentFailure | CancelAck
