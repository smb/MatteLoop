"""Deterministic byte-protocol child used by spawned client tests."""

from __future__ import annotations

import os
import time
from multiprocessing.connection import Connection
from multiprocessing.shared_memory import SharedMemory
from typing import Any

import numpy as np

from rembggui.jobs.protocol import (
    CONTROL_JOB_ID,
    MAX_PROTOCOL_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    CancelAck,
    CancelRequest,
    SegmentFailure,
    SegmentRequest,
    SegmentResponse,
    Shutdown,
    WorkerReady,
    decode_parent_message,
    encode_child_message,
)


def fake_segmentation_child(connection: Connection, model_spec: object) -> None:
    config = model_spec if type(model_spec) is dict else {}
    mode = str(config.get("mode", "success"))
    ready = encode_child_message(
        WorkerReady(PROTOCOL_VERSION, CONTROL_JOB_ID, os.getpid())
    )
    if mode == "startup-version":
        ready = ready.replace(b'"protocol_version":1', b'"protocol_version":2')
    connection.send_bytes(ready)
    if mode == "startup-version":
        connection.close()
        return

    cancelled: set[str] = set()
    try:
        while True:
            message = decode_parent_message(
                connection.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES)
            )
            if isinstance(message, Shutdown):
                return
            if isinstance(message, CancelRequest):
                if message.job_id not in cancelled:
                    cancelled.add(message.job_id)
                    _send(connection, CancelAck(PROTOCOL_VERSION, message.job_id))
                continue
            if not isinstance(message, SegmentRequest) or message.slot is None:
                continue
            if mode == "crash-delayed":
                time.sleep(0.1)
                os._exit(23)
            if mode == "crash":
                os._exit(23)
            if mode == "unsolicited-ack":
                _send(connection, CancelAck(PROTOCOL_VERSION, message.job_id))
                continue

            slot = SharedMemory(name=message.slot.name, create=False)
            try:
                source = np.ndarray(
                    message.slot.shape, dtype=np.uint8, buffer=slot.buf
                ).copy()
                if mode == "delayed-cancel":
                    time.sleep(0.25)
                rgba = np.concatenate(
                    (
                        source[..., :3],
                        np.full(source.shape[:2] + (1,), 255, dtype=np.uint8),
                    ),
                    axis=2,
                )
                output_bytes = rgba.tobytes(order="C")
                slot.buf[: len(output_bytes)] = output_bytes

                while connection.poll():
                    pending = decode_parent_message(
                        connection.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES)
                    )
                    if (
                        isinstance(pending, CancelRequest)
                        and pending.job_id == message.job_id
                    ):
                        cancelled.add(pending.job_id)
                if message.job_id in cancelled:
                    _send(connection, CancelAck(PROTOCOL_VERSION, message.job_id))
                    continue

                response = SegmentResponse(
                    PROTOCOL_VERSION,
                    "stale-job" if mode == "wrong-job" else message.job_id,
                    "stale-request" if mode == "wrong-request" else message.request_id,
                    (rgba.shape[0], rgba.shape[1], 3)
                    if mode == "wrong-shape"
                    else rgba.shape,
                    "float32" if mode == "wrong-dtype" else "uint8",
                    len(output_bytes) - 1
                    if mode == "wrong-byte-length"
                    else len(output_bytes),
                )
                if mode == "malformed-error":
                    connection.send_bytes(b'{"type":"segment_failure"}')
                    continue
                raw = encode_child_message(response)
                if mode == "response-version":
                    raw = raw.replace(b'"protocol_version":1', b'"protocol_version":2')
                if mode == "invalid-utf8":
                    raw = b"\xff"
                if mode == "oversized":
                    raw = b"x" * (MAX_PROTOCOL_MESSAGE_BYTES + 1)
                connection.send_bytes(raw)
                if mode == "late-response":
                    connection.send_bytes(raw)
                if mode == "exit-after-response":
                    return
                if mode == "exit-delayed-after-response":
                    time.sleep(0.1)
                    return
            finally:
                slot.close()
    except (EOFError, BrokenPipeError, OSError, ValueError):
        return
    finally:
        connection.close()


def _send(connection: Connection, message: CancelAck | SegmentFailure) -> None:
    connection.send_bytes(encode_child_message(message))


def unpicklable_model_spec() -> dict[str, Any]:
    return {"factory": lambda: None}
