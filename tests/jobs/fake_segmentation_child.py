"""Deterministic spawned child used by segmentation protocol tests."""

from __future__ import annotations

import os
import time
from multiprocessing.connection import Connection
from multiprocessing.shared_memory import SharedMemory
from typing import Any

import numpy as np


def fake_segmentation_child(connection: Connection, model_spec: object) -> None:
    """Run a no-ONNX protocol peer whose behaviour is selected by *model_spec*."""
    from rembggui.jobs.protocol import (
        PROTOCOL_VERSION,
        CancelAck,
        CancelRequest,
        SegmentFailure,
        SegmentRequest,
        SegmentResponse,
        Shutdown,
        WorkerReady,
    )

    config = model_spec if isinstance(model_spec, dict) else {}
    mode = str(config.get("mode", "success"))
    connection.send(
        WorkerReady(
            protocol_version=(
                PROTOCOL_VERSION + 1 if mode == "startup-version" else PROTOCOL_VERSION
            ),
            job_id="__control__",
            process_id=os.getpid(),
        )
    )
    if mode == "startup-version":
        connection.close()
        return

    cancelled: set[str] = set()
    try:
        while True:
            message = connection.recv()
            if isinstance(message, Shutdown):
                return
            if isinstance(message, CancelRequest):
                if message.job_id not in cancelled:
                    cancelled.add(message.job_id)
                    connection.send(CancelAck(PROTOCOL_VERSION, message.job_id))
                continue
            if not isinstance(message, SegmentRequest):
                continue
            if mode == "crash-delayed":
                time.sleep(0.1)
                os._exit(23)
            if mode == "crash":
                os._exit(23)
            if mode == "unsolicited-ack":
                connection.send(CancelAck(PROTOCOL_VERSION, message.job_id))
                continue

            assert message.slot is not None
            slot = SharedMemory(name=message.slot.name, create=False)
            try:
                source = np.ndarray(
                    message.slot.shape,
                    dtype=np.dtype(message.slot.dtype),
                    buffer=slot.buf,
                ).copy()
                if mode == "delayed-cancel":
                    time.sleep(0.25)

                alpha = np.full(source.shape[:2] + (1,), 255, dtype=np.uint8)
                rgba = np.concatenate((source[..., :3], alpha), axis=2)
                output_bytes = rgba.tobytes(order="C")
                slot.buf[: len(output_bytes)] = output_bytes

                while connection.poll():
                    pending = connection.recv()
                    if (
                        isinstance(pending, CancelRequest)
                        and pending.job_id == message.job_id
                    ):
                        cancelled.add(pending.job_id)
                if message.job_id in cancelled:
                    connection.send(CancelAck(PROTOCOL_VERSION, message.job_id))
                    continue

                response = SegmentResponse(
                    protocol_version=(
                        PROTOCOL_VERSION + 1
                        if mode == "response-version"
                        else PROTOCOL_VERSION
                    ),
                    job_id=("stale-job" if mode == "wrong-job" else message.job_id),
                    request_id=(
                        "stale-request"
                        if mode == "wrong-request"
                        else message.request_id
                    ),
                    shape=rgba.shape,
                    dtype="uint8",
                    byte_length=(
                        len(output_bytes) - 1
                        if mode == "wrong-byte-length"
                        else len(output_bytes)
                    ),
                )
                if mode == "malformed-error":
                    connection.send(
                        SegmentFailure(
                            PROTOCOL_VERSION,
                            message.job_id,
                            message.request_id,
                            {"code": 7},  # type: ignore[dict-item]
                        )
                    )
                    continue
                connection.send(response)
                if mode == "late-response":
                    connection.send(response)
            finally:
                slot.close()
    except (EOFError, BrokenPipeError, OSError):
        return
    finally:
        connection.close()


def unpicklable_model_spec() -> dict[str, Any]:
    return {"factory": lambda: None}
