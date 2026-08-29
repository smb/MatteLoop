"""Bounded canonical-JSON protocol for the segmentation process boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import NoReturn

PROTOCOL_VERSION = 1
CONTROL_JOB_ID = "__control__"
MAX_PROTOCOL_MESSAGE_BYTES = 64 * 1024
_MAX_ID_LENGTH = 256
_MAX_TEXT_LENGTH = 16 * 1024

type FrameShape = tuple[int, int, int]


class ProtocolCodecError(ValueError):
    """Raised when untrusted wire bytes do not match one exact schema."""


@dataclass(frozen=True, slots=True)
class SharedFrame:
    name: str
    shape: FrameShape
    dtype: str
    byte_length: int


@dataclass(frozen=True, slots=True)
class SegmentRequest:
    protocol_version: int
    job_id: str
    request_id: str
    slot: SharedFrame | None = None


@dataclass(frozen=True, slots=True)
class SegmentResponse:
    protocol_version: int
    job_id: str
    request_id: str
    shape: FrameShape
    dtype: str
    byte_length: int


@dataclass(frozen=True, slots=True)
class SegmentFailure:
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


def encode_parent_message(message: ParentMessage) -> bytes:
    payload = _message_payload(message)
    encoded = _dump(payload)
    decode_parent_message(encoded)
    return encoded


def encode_child_message(message: ChildMessage) -> bytes:
    payload = _message_payload(message)
    encoded = _dump(payload)
    decode_child_message(encoded)
    return encoded


def decode_parent_message(data: bytes) -> ParentMessage:
    payload = _load(data)
    message_type = _type_tag(payload)
    if message_type == "segment_request":
        _exact_keys(
            payload,
            {"type", "protocol_version", "job_id", "request_id", "slot"},
        )
        slot_payload = payload["slot"]
        if type(slot_payload) is not dict:
            _fail("slot must be an object")
        return SegmentRequest(
            _version(payload),
            _identifier(payload, "job_id"),
            _identifier(payload, "request_id"),
            _decode_slot(slot_payload),
        )
    if message_type == "cancel_request":
        _exact_keys(payload, {"type", "protocol_version", "job_id"})
        return CancelRequest(_version(payload), _identifier(payload, "job_id"))
    if message_type == "shutdown":
        _exact_keys(payload, {"type", "protocol_version", "job_id"})
        job_id = _identifier(payload, "job_id")
        if job_id != CONTROL_JOB_ID:
            _fail("Shutdown requires the control job ID")
        return Shutdown(_version(payload), job_id)
    _fail("message type is not valid in the parent-to-child direction")


def decode_child_message(data: bytes) -> ChildMessage:
    payload = _load(data)
    message_type = _type_tag(payload)
    if message_type == "worker_ready":
        _exact_keys(payload, {"type", "protocol_version", "job_id", "process_id"})
        job_id = _identifier(payload, "job_id")
        if job_id != CONTROL_JOB_ID:
            _fail("WorkerReady requires the control job ID")
        process_id = _strict_int(payload, "process_id", minimum=1)
        return WorkerReady(_version(payload), job_id, process_id)
    if message_type == "segment_response":
        _exact_keys(
            payload,
            {
                "type",
                "protocol_version",
                "job_id",
                "request_id",
                "shape",
                "dtype",
                "byte_length",
            },
        )
        return SegmentResponse(
            _version(payload),
            _identifier(payload, "job_id"),
            _identifier(payload, "request_id"),
            _decode_shape(payload["shape"]),
            _exact_string(payload, "dtype", 32),
            _strict_int(payload, "byte_length", minimum=1),
        )
    if message_type == "segment_failure":
        _exact_keys(
            payload,
            {"type", "protocol_version", "job_id", "request_id", "error"},
        )
        error = payload["error"]
        if type(error) is not dict:
            _fail("error must be an object")
        return SegmentFailure(
            _version(payload),
            _identifier(payload, "job_id"),
            _identifier(payload, "request_id"),
            _decode_error(error),
        )
    if message_type == "cancel_ack":
        _exact_keys(payload, {"type", "protocol_version", "job_id"})
        return CancelAck(_version(payload), _identifier(payload, "job_id"))
    _fail("message type is not valid in the child-to-parent direction")


def _message_payload(message: object) -> dict[str, object]:
    if type(message) is SegmentRequest:
        if message.slot is None:
            _fail("wire SegmentRequest requires a shared-frame descriptor")
        return {
            "type": "segment_request",
            "protocol_version": message.protocol_version,
            "job_id": message.job_id,
            "request_id": message.request_id,
            "slot": _slot_payload(message.slot),
        }
    if type(message) is SegmentResponse:
        return {
            "type": "segment_response",
            "protocol_version": message.protocol_version,
            "job_id": message.job_id,
            "request_id": message.request_id,
            "shape": list(message.shape),
            "dtype": message.dtype,
            "byte_length": message.byte_length,
        }
    if type(message) is SegmentFailure:
        return {
            "type": "segment_failure",
            "protocol_version": message.protocol_version,
            "job_id": message.job_id,
            "request_id": message.request_id,
            "error": message.error,
        }
    if type(message) is CancelRequest:
        return {
            "type": "cancel_request",
            "protocol_version": message.protocol_version,
            "job_id": message.job_id,
        }
    if type(message) is CancelAck:
        return {
            "type": "cancel_ack",
            "protocol_version": message.protocol_version,
            "job_id": message.job_id,
        }
    if type(message) is Shutdown:
        return {
            "type": "shutdown",
            "protocol_version": message.protocol_version,
            "job_id": message.job_id,
        }
    if type(message) is WorkerReady:
        return {
            "type": "worker_ready",
            "protocol_version": message.protocol_version,
            "job_id": message.job_id,
            "process_id": message.process_id,
        }
    _fail("unsupported protocol message object")


def _slot_payload(slot: SharedFrame) -> dict[str, object]:
    return {
        "name": slot.name,
        "shape": list(slot.shape),
        "dtype": slot.dtype,
        "byte_length": slot.byte_length,
    }


def _decode_slot(payload: dict[str, object]) -> SharedFrame:
    _exact_keys(payload, {"name", "shape", "dtype", "byte_length"})
    shape = _decode_shape(payload["shape"])
    byte_length = _strict_int(payload, "byte_length", minimum=1)
    if byte_length != shape[0] * shape[1] * shape[2]:
        _fail("shared-frame byte length does not match its shape")
    return SharedFrame(
        _exact_string(payload, "name", 256),
        shape,
        _exact_string(payload, "dtype", 32),
        byte_length,
    )


def _decode_shape(value: object) -> FrameShape:
    if type(value) is not list or len(value) != 3:
        _fail("shape must contain exactly three integers")
    dimensions: list[int] = []
    for dimension in value:
        if type(dimension) is not int or not 1 <= dimension <= 16_383:
            _fail("shape dimensions must be bounded integers")
        dimensions.append(dimension)
    return dimensions[0], dimensions[1], dimensions[2]


def _decode_error(payload: dict[str, object]) -> dict[str, str | None]:
    keys = {
        "code",
        "stage",
        "message_key",
        "technical_detail",
        "retry_action",
        "job_id",
    }
    _exact_keys(payload, keys)
    result: dict[str, str | None] = {}
    for key in keys:
        value = payload[key]
        if key == "job_id" and value is None:
            result[key] = None
        elif type(value) is str and 0 < len(value) <= _MAX_TEXT_LENGTH:
            result[key] = value
        else:
            _fail(f"error.{key} must be a bounded string or permitted null")
    return result


def _load(data: bytes) -> dict[str, object]:
    if type(data) is not bytes or not 0 < len(data) <= MAX_PROTOCOL_MESSAGE_BYTES:
        _fail("protocol message byte length is invalid")
    try:
        text = data.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=lambda token: _fail(f"invalid JSON constant {token}"),
        )
    except ProtocolCodecError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as error:
        raise ProtocolCodecError(
            "protocol message is not valid bounded JSON"
        ) from error
    if type(value) is not dict:
        _fail("protocol message root must be an object")
    return value


def _dump(payload: dict[str, object]) -> bytes:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError) as error:
        raise ProtocolCodecError("message object is not safely encodable") from error
    if not 0 < len(encoded) <= MAX_PROTOCOL_MESSAGE_BYTES:
        _fail("encoded protocol message exceeds its byte limit")
    return encoded


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            _fail("protocol objects require unique string keys")
        result[key] = value
    return result


def _type_tag(payload: dict[str, object]) -> str:
    return _exact_string(payload, "type", 64)


def _version(payload: dict[str, object]) -> int:
    version = _strict_int(payload, "protocol_version", minimum=1)
    if version != PROTOCOL_VERSION:
        _fail("protocol version mismatch")
    return version


def _identifier(payload: dict[str, object], key: str) -> str:
    return _exact_string(payload, key, _MAX_ID_LENGTH)


def _exact_string(payload: dict[str, object], key: str, maximum: int) -> str:
    value = payload.get(key)
    if type(value) is not str or not value or len(value) > maximum:
        _fail(f"{key} must be a bounded non-empty string")
    return value


def _strict_int(payload: dict[str, object], key: str, *, minimum: int) -> int:
    value = payload.get(key)
    if type(value) is not int or value < minimum:
        _fail(f"{key} must be an integer greater than or equal to {minimum}")
    return value


def _exact_keys(payload: dict[str, object], expected: set[str]) -> None:
    if set(payload) != expected:
        _fail("protocol message has missing or unknown fields")


def _fail(detail: str) -> NoReturn:
    raise ProtocolCodecError(detail)
