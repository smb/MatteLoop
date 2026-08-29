"""Importable multiprocessing target used by the native packaging smoke."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import socket
from multiprocessing.connection import Connection
from multiprocessing.shared_memory import SharedMemory


class _FakeLocalSession:
    """Tiny deterministic inference boundary that never imports an ML runtime."""

    def process(self, rgba: memoryview) -> None:
        for alpha_index in range(3, len(rgba), 4):
            rgba[alpha_index] = 255 - rgba[alpha_index]


def spawn_smoke_target(
    connection: Connection,
    shared_memory_name: str,
    byte_count: int,
    expected_input_sha256: str,
    use_fake_model: bool,
) -> None:
    """Validate and mutate shared RGBA bytes in a genuine spawned child."""

    def forbidden_network(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("network access is forbidden in the smoke child")

    setattr(socket, "socket", forbidden_network)
    setattr(socket, "create_connection", forbidden_network)
    segment: SharedMemory | None = None
    rgba: memoryview | None = None
    try:
        segment = SharedMemory(name=shared_memory_name, create=False)
        raw_buffer = segment.buf
        if raw_buffer is None:
            raise RuntimeError("shared-memory buffer is unavailable")
        attached_view = raw_buffer[:byte_count]
        rgba = attached_view
        input_digest = hashlib.sha256(attached_view).hexdigest()
        if input_digest != expected_input_sha256:
            raise RuntimeError("shared-memory input digest mismatch")
        if use_fake_model:
            _FakeLocalSession().process(attached_view)
        output_digest = hashlib.sha256(attached_view).hexdigest()
        connection.send_bytes(
            json.dumps(
                {
                    "fake_session_used": use_fake_model,
                    "input_sha256": input_digest,
                    "output_sha256": output_digest,
                    "start_method": multiprocessing.get_start_method(),
                    "shared_memory_roundtrip": True,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
    except BaseException as error:
        connection.send_bytes(
            json.dumps(
                {
                    "error": {
                        "message": str(error),
                        "type": type(error).__name__,
                    }
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
    finally:
        if rgba is not None:
            rgba.release()
        if segment is not None:
            segment.close()
        connection.close()
