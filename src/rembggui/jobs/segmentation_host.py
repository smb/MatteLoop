"""Spawned rembg host with a bounded byte protocol and parent-owned frame slot.

Ownership and lock/protocol order::

    parent                                   spawned child
    ------                                   -------------
    owns + unlinks SHM  -- descriptor -----> attaches/closes SHM only
    wire lock: Request   -- send_bytes -----> one session / one inference
      then Cancel        -- send_bytes -----> observed after inference
    validates metadata   <- recv_bytes ------ Response OR matching CancelAck

Local validation, slot preparation, and bounded encoding finish before an active
identity exists. Publication and request transport are one wire-locked operation,
so an admitted cancel can only follow a request already on the wire. Code never
holds the state lock while acquiring the wire/lifecycle locks; nested order is
``operation -> wire -> lifecycle -> state``. A dead child, malformed bytes,
protocol mismatch, or teardown anomaly invalidates the slot and cannot implicitly
spawn a replacement.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import multiprocessing
import os
import platform
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from multiprocessing.connection import Connection
from multiprocessing.context import BaseContext
from multiprocessing.process import BaseProcess
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path
from threading import Lock, RLock, get_ident
from typing import Any, NoReturn

import numpy as np
from numpy.typing import NDArray

from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.execution_providers import (
    CPU_EXECUTION_PROVIDER,
    is_allowed_provider,
    provider_base_label,
)
from rembggui.jobs.models.cache_fs import (
    BoundDirectoryCloseError,
    BoundModelDirectory,
    UnsafeCacheError,
)
from rembggui.jobs.protocol import (
    CONTROL_JOB_ID,
    MAX_PROTOCOL_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    CancelAck,
    CancelRequest,
    ChildMessage,
    ParentMessage,
    ProtocolCodecError,
    SegmentFailure,
    SegmentOptions,
    SegmentRequest,
    SegmentResponse,
    SharedFrame,
    Shutdown,
    WorkerReady,
    decode_child_message,
    decode_parent_message,
    encode_child_message,
    encode_parent_message,
    validate_segment_options,
)

_JOIN_TIMEOUT_SECONDS = 1.0
_MAX_FRAME_DIMENSION = 16_383
_MAX_LAUNCH_PAYLOAD_BYTES = 16 * 1024
_MAX_LAUNCH_DEPTH = 6
_MAX_LAUNCH_ITEMS = 256
_MAX_LAUNCH_NODES = 2_048
_MAX_LAUNCH_TEXT_BYTES = 16 * 1024
_LOGGER = logging.getLogger(__name__)

type Uint8Frame = NDArray[np.uint8]
type ChildTarget = Callable[[Connection, object], None]
type Inference = Callable[[Uint8Frame, object, SegmentOptions], Uint8Frame]


class SegmentationClient:
    """Parent-side lifecycle and byte-level trust boundary for one child."""

    def __init__(
        self,
        model_spec: object,
        *,
        child_target: ChildTarget | None = None,
        startup_timeout: float = 30.0,
        response_timeout: float = 120.0,
        mp_context: BaseContext | None = None,
    ) -> None:
        if startup_timeout <= 0 or response_timeout <= 0:
            raise ValueError("process timeouts must be positive")
        self._model_spec = _normalize_launch_payload(model_spec)
        self._effective_provider = _launch_provider(self._model_spec)
        self._startup_notice: str | None = None
        self._child_target = (
            child_target if child_target is not None else segmentation_process_main
        )
        self._startup_timeout = startup_timeout
        self._response_timeout = response_timeout
        self._mp_context = mp_context or multiprocessing.get_context("spawn")
        self._lifecycle_lock = RLock()
        self._state_lock = Lock()
        self._wire_lock = Lock()
        self._operation_lock = Lock()
        self._operation_owner_lock = Lock()
        self._operation_owner_thread_id: int | None = None
        self._operation_job_id: str | None = None
        self._process: BaseProcess | None = None
        self._connection: Connection | None = None
        self._child_endpoint: Connection | None = None
        self._slot: SharedMemory | None = None
        self._slot_capacity = 0
        self._slot_closed = False
        self._slot_unlinked = False
        self._active_job_id: str | None = None
        self._active_request_id: str | None = None
        self._request_sent = False
        self._cancel_requested = False
        self._cancel_wire_sent = False

    @property
    def is_running(self) -> bool:
        with self._lifecycle_lock:
            return self._is_running_unlocked()

    @property
    def process_id(self) -> int | None:
        with self._lifecycle_lock:
            process = self._process
            return process.pid if process is not None and _safe_alive(process) else None

    @property
    def shared_memory_name(self) -> str | None:
        with self._lifecycle_lock:
            return self._slot.name if self._slot is not None else None

    @property
    def active_job_id(self) -> str | None:
        with self._state_lock:
            return self._active_job_id

    @property
    def effective_provider(self) -> str:
        with self._lifecycle_lock:
            return self._effective_provider

    @property
    def startup_notice(self) -> str | None:
        with self._lifecycle_lock:
            return self._startup_notice

    def start(self) -> None:
        """Start a fresh child; after invalidation this is the explicit retry."""
        self._claim_operation(job_id=None, busy_job_id=self.active_job_id)
        try:
            self._start_operation_owned()
        finally:
            self._release_operation()

    def _start_operation_owned(self) -> None:
        with self._wire_lock:
            with self._lifecycle_lock:
                if self._is_running_unlocked():
                    return
                self._effective_provider = _launch_provider(self._model_spec)
                self._startup_notice = None
                self._discard_process_unlocked(graceful=False)
                try:
                    parent, child = self._mp_context.Pipe(duplex=True)
                except BaseException as error:
                    raise self._crash_error(
                        "segmentation endpoints could not be created", cause=error
                    ) from error
                self._connection = parent
                self._child_endpoint = child
                try:
                    process = self._mp_context.Process(  # type: ignore[attr-defined]
                        target=self._child_target,
                        args=(child, self._model_spec),
                        name="rembggui-segmentation",
                        daemon=True,
                    )
                except BaseException as error:
                    self._raise_startup_failure_unlocked(
                        "segmentation process handle could not be created", error
                    )
                self._process = process
                try:
                    process.start()
                except BaseException as error:
                    self._raise_startup_failure_unlocked(
                        "segmentation process could not be spawned", error
                    )
                try:
                    child.close()
                except BaseException as error:
                    self._raise_startup_failure_unlocked(
                        "parent could not close its child-side endpoint", error
                    )
                self._child_endpoint = None
                try:
                    message = self._receive_child_unlocked(
                        self._startup_timeout, expected_job_id=None
                    )
                    if (
                        not isinstance(message, WorkerReady)
                        or message.process_id != process.pid
                    ):
                        raise self._protocol_error_unlocked(
                            "child startup acknowledgement identity mismatch"
                        )
                    if not _safe_alive(process):
                        raise self._crash_error(
                            "segmentation process exited immediately after startup"
                        )
                    self._effective_provider = (
                        message.execution_provider or _launch_provider(self._model_spec)
                    )
                    self._startup_notice = message.startup_notice
                except BaseException as error:
                    try:
                        self._discard_process_unlocked(graceful=False)
                    except AppError as cleanup_error:
                        raise cleanup_error from error
                    raise

    def _raise_startup_failure_unlocked(
        self, detail: str, cause: BaseException
    ) -> NoReturn:
        crash_error = self._crash_error(detail, cause=cause)
        try:
            self._discard_process_unlocked(graceful=False)
        except AppError as cleanup_error:
            raise cleanup_error from cause
        raise crash_error from cause

    def segment(
        self, image: np.ndarray[Any, Any], request: SegmentRequest
    ) -> Uint8Frame:
        """Process one RGB(A) frame and return a private validated RGBA copy."""
        job_id = request.job_id if isinstance(request, SegmentRequest) else None
        self._claim_operation(job_id=job_id, busy_job_id=job_id)
        try:
            self._validate_request(request)
            frame = self._validate_image(image)
            with self._lifecycle_lock:
                if not self._is_running_unlocked():
                    self._discard_process_unlocked(
                        graceful=False, job_id=request.job_id
                    )
                    raise self._crash_error(
                        "segmentation process is not running; explicit retry required",
                        job_id=request.job_id,
                    )
                self._reject_pending_messages_unlocked(request.job_id)
                output_capacity = frame.shape[0] * frame.shape[1] * 4
                slot = self._ensure_slot_unlocked(output_capacity, request.job_id)
                input_bytes = frame.tobytes(order="C")
                try:
                    slot_buffer = slot.buf
                    if slot_buffer is None:
                        raise BufferError("shared-memory buffer is unavailable")
                    slot_buffer[: len(input_bytes)] = input_bytes
                except (BufferError, OSError, TypeError, ValueError) as error:
                    self._discard_process_unlocked(
                        graceful=False, job_id=request.job_id
                    )
                    raise self._crash_error(
                        "could not write the parent-owned segmentation frame slot",
                        job_id=request.job_id,
                        cause=error,
                    ) from error
                wire_request = replace(
                    request,
                    slot=SharedFrame(
                        slot.name,
                        (frame.shape[0], frame.shape[1], frame.shape[2]),
                        "uint8",
                        len(input_bytes),
                    ),
                )
                connection = self._connection
                assert connection is not None
                payload = self._encode_parent(wire_request, request.job_id)
            self._after_local_preparation()
            with self._wire_lock:
                with self._lifecycle_lock:
                    if (
                        not self._is_running_unlocked()
                        or self._connection is not connection
                    ):
                        self._discard_process_unlocked(
                            graceful=False, job_id=request.job_id
                        )
                        raise self._crash_error(
                            "segmentation process closed during local preparation",
                            job_id=request.job_id,
                        )
                    self._reject_pending_messages_unlocked(request.job_id)
                    self._before_request_wire_send()
                    self._publish_active(request)
                    self._send_parent_bytes(connection, payload, request.job_id)
                    with self._state_lock:
                        if self._active_job_id == request.job_id:
                            self._request_sent = True
            return self._await_result(frame.shape[:2], request)
        finally:
            self._clear_active()
            self._release_operation()

    def cancel(self, job_id: str) -> bool:
        """Admit one cancel and serialize it strictly after its SegmentRequest."""
        if type(job_id) is not str or not job_id:
            return False
        self._before_cancel_wire_wait()
        with self._wire_lock:
            with self._state_lock:
                if (
                    self._active_job_id != job_id
                    or not self._request_sent
                    or self._cancel_requested
                ):
                    return False
                self._cancel_requested = True
            with self._lifecycle_lock:
                if not self._is_running_unlocked() or self._connection is None:
                    with self._state_lock:
                        if self._active_job_id == job_id:
                            self._cancel_requested = False
                    return False
                connection = self._connection
                self._send_cancel_on_wire(connection, job_id)
        return True

    def replace_model(self, model_spec: object) -> None:
        """Prove the old process dead before starting the normalized replacement."""
        normalized = _normalize_launch_payload(model_spec)
        self._claim_operation(job_id=None, busy_job_id=self.active_job_id)
        try:
            with self._wire_lock:
                with self._lifecycle_lock:
                    self._discard_process_unlocked(graceful=True)
                    self._model_spec = normalized
                    self._effective_provider = _launch_provider(normalized)
                    self._startup_notice = None
            self._start_operation_owned()
        finally:
            self._release_operation()

    def close(self) -> None:
        """Stop, prove dead, and unlink parent state; safe to call repeatedly."""
        self._before_close_operation_wait()
        self._claim_close_operation()
        try:
            with self._wire_lock:
                with self._lifecycle_lock:
                    with self._state_lock:
                        job_id = self._active_job_id
                    self._discard_process_unlocked(graceful=True, job_id=job_id)
        finally:
            self._release_operation()

    def _before_request_wire_send(self) -> None:
        """Test seam called while the wire lock enforces Request-before-Cancel."""

    def _after_local_preparation(self) -> None:
        """Test seam after local preparation but before active publication."""

    def _before_cancel_wire_wait(self) -> None:
        """Test seam immediately before cancellation waits for wire ownership."""

    def _before_close_operation_wait(self) -> None:
        """Test seam immediately before close waits for operation ownership."""

    def _claim_operation(self, *, job_id: str | None, busy_job_id: str | None) -> None:
        if not self._operation_lock.acquire(blocking=False):
            raise self._busy_error(busy_job_id)
        with self._operation_owner_lock:
            self._operation_owner_thread_id = get_ident()
            self._operation_job_id = job_id

    def _claim_close_operation(self) -> None:
        current_thread_id = get_ident()
        with self._operation_owner_lock:
            if self._operation_owner_thread_id == current_thread_id:
                raise self._busy_error(self._operation_job_id)
        self._operation_lock.acquire()
        with self._operation_owner_lock:
            self._operation_owner_thread_id = current_thread_id
            self._operation_job_id = None

    def _release_operation(self) -> None:
        with self._operation_owner_lock:
            self._operation_owner_thread_id = None
            self._operation_job_id = None
        self._operation_lock.release()

    def _publish_active(self, request: SegmentRequest) -> None:
        with self._state_lock:
            self._active_job_id = request.job_id
            self._active_request_id = request.request_id
            self._request_sent = False
            self._cancel_requested = False
            self._cancel_wire_sent = False

    def _clear_active(self) -> None:
        with self._state_lock:
            self._active_job_id = None
            self._active_request_id = None
            self._request_sent = False
            self._cancel_requested = False
            self._cancel_wire_sent = False

    def _send_cancel_on_wire(self, connection: Connection, job_id: str) -> None:
        self._send_parent(connection, CancelRequest(PROTOCOL_VERSION, job_id), job_id)
        with self._state_lock:
            if self._active_job_id == job_id:
                self._cancel_wire_sent = True

    def _await_result(
        self, source_size: tuple[int, int], request: SegmentRequest
    ) -> Uint8Frame:
        try:
            message = self._receive_child_unlocked(
                self._response_timeout, expected_job_id=request.job_id
            )
        except AppError:
            with self._lifecycle_lock:
                self._discard_process_unlocked(graceful=False, job_id=request.job_id)
            raise
        if isinstance(message, CancelAck):
            self._raise_cancelled(message, request)
        if isinstance(message, SegmentFailure):
            self._validate_response_identity(
                message.job_id, message.request_id, request
            )
            if self._cancel_is_pending(request.job_id):
                self._await_cancel_ack(request)
            try:
                error = AppError.from_primitives(message.error)
            except AppError as invalid_error:
                raise self._protocol_error(
                    "child returned a malformed structured error", request.job_id
                ) from invalid_error
            if error.job_id != request.job_id:
                raise self._protocol_error(
                    "child error job identity mismatch", request.job_id
                )
            if error.code is ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH:
                with self._lifecycle_lock:
                    self._discard_process_unlocked(
                        graceful=False, job_id=request.job_id
                    )
            raise error
        if not isinstance(message, SegmentResponse):
            raise self._protocol_error(
                "unexpected segmentation response type", request.job_id
            )
        self._validate_response_identity(message.job_id, message.request_id, request)
        if self._cancel_is_pending(request.job_id):
            self._await_cancel_ack(request)
        height, width = source_size
        expected_shape = (height, width, 4)
        expected_bytes = height * width * 4
        with self._lifecycle_lock:
            if (
                message.shape != expected_shape
                or message.dtype != "uint8"
                or message.byte_length != expected_bytes
                or self._slot is None
                or message.byte_length > self._slot.size
            ):
                raise self._protocol_error_unlocked(
                    "segmentation response shape, dtype, or byte length is invalid",
                    request.job_id,
                )
            if self._connection is None:
                raise self._crash_error(
                    "segmentation connection disappeared", job_id=request.job_id
                )
            if self._poll_connection_unlocked(request.job_id):
                raise self._protocol_error_unlocked(
                    "unexpected late or duplicate child response", request.job_id
                )
            output = np.ndarray(
                expected_shape, dtype=np.uint8, buffer=self._slot.buf
            ).copy()
        output.setflags(write=False)
        return output

    def _cancel_is_pending(self, job_id: str) -> bool:
        with self._state_lock:
            if self._active_job_id != job_id:
                return False
            if self._cancel_requested:
                return True
            self._active_job_id = None
            self._active_request_id = None
            return False

    def _await_cancel_ack(self, request: SegmentRequest) -> None:
        try:
            acknowledgement = self._receive_child_unlocked(
                self._response_timeout, expected_job_id=request.job_id
            )
        except AppError:
            with self._lifecycle_lock:
                self._discard_process_unlocked(graceful=False, job_id=request.job_id)
            raise
        if not isinstance(acknowledgement, CancelAck):
            raise self._protocol_error(
                "response after cancellation lacked a matching acknowledgement",
                request.job_id,
            )
        self._raise_cancelled(acknowledgement, request)

    def _raise_cancelled(
        self, acknowledgement: CancelAck, request: SegmentRequest
    ) -> None:
        if acknowledgement.job_id != request.job_id:
            raise self._protocol_error(
                "cancellation acknowledgement identity mismatch", request.job_id
            )
        with self._state_lock:
            expected = (
                self._active_job_id == request.job_id
                and self._cancel_requested
                and self._cancel_wire_sent
            )
            if expected:
                self._active_job_id = None
                self._active_request_id = None
        if not expected:
            raise self._protocol_error(
                "unsolicited cancellation acknowledgement", request.job_id
            )
        with self._lifecycle_lock:
            connection = self._connection
            if connection is not None and connection.poll():
                raise self._protocol_error_unlocked(
                    "duplicate or late response followed cancellation", request.job_id
                )
        raise AppError(
            ErrorCode.JOB_CANCELLED,
            "segmentation",
            "error.job.cancelled",
            "segmentation cancelled after the active inference became safe",
            "retry-job",
            request.job_id,
        )

    def _validate_response_identity(
        self, job_id: str, request_id: str, expected: SegmentRequest
    ) -> None:
        if job_id != expected.job_id or request_id != expected.request_id:
            raise self._protocol_error(
                "segmentation response identity mismatch", expected.job_id
            )

    def _validate_request(self, request: object) -> None:
        if (
            type(request) is not SegmentRequest
            or type(request.protocol_version) is not int
            or request.protocol_version != PROTOCOL_VERSION
            or type(request.job_id) is not str
            or not request.job_id
            or type(request.request_id) is not str
            or not request.request_id
            or request.slot is not None
            or type(request.options) is not SegmentOptions
        ):
            job_id = request.job_id if isinstance(request, SegmentRequest) else None
            raise self._protocol_error(
                "segment request is malformed or already contains a shared slot", job_id
            )

    @staticmethod
    def _validate_image(image: object) -> Uint8Frame:
        if (
            not isinstance(image, np.ndarray)
            or image.dtype != np.dtype(np.uint8)
            or image.ndim != 3
            or image.shape[2] not in {3, 4}
            or image.shape[0] < 1
            or image.shape[1] < 1
            or image.shape[0] > _MAX_FRAME_DIMENSION
            or image.shape[1] > _MAX_FRAME_DIMENSION
        ):
            raise _protocol_app_error(
                "input must be a non-empty uint8 HxWx3/4 array within format limits"
            )
        frame = np.ascontiguousarray(image)
        if frame.nbytes != frame.shape[0] * frame.shape[1] * frame.shape[2]:
            raise _protocol_app_error("input frame byte length is inconsistent")
        return frame

    def _ensure_slot_unlocked(self, capacity: int, job_id: str) -> SharedMemory:
        if (
            self._slot is not None
            and not self._slot_closed
            and not self._slot_unlinked
            and self._slot_capacity >= capacity
        ):
            return self._slot
        cleanup_errors: list[str] = []
        self._discard_slot_unlocked(cleanup_errors)
        if cleanup_errors:
            raise _cleanup_error("; ".join(cleanup_errors), job_id)
        try:
            self._slot = SharedMemory(create=True, size=capacity)
        except (OSError, ValueError) as error:
            raise self._crash_error(
                "could not allocate the parent-owned segmentation frame slot",
                job_id=job_id,
                cause=error,
            ) from error
        self._slot_capacity = capacity
        self._slot_closed = False
        self._slot_unlinked = False
        return self._slot

    def _reject_pending_messages_unlocked(self, job_id: str) -> None:
        if self._connection is not None and self._poll_connection_unlocked(job_id):
            raise self._protocol_error_unlocked(
                "stale child response remained before a new request", job_id
            )

    def _poll_connection_unlocked(self, job_id: str) -> bool:
        connection = self._connection
        if connection is None:
            raise self._crash_error(
                "segmentation process has no live connection", job_id=job_id
            )
        try:
            return connection.poll()
        except (BrokenPipeError, EOFError, OSError) as error:
            self._discard_process_unlocked(graceful=False, job_id=job_id)
            raise self._crash_error(
                "segmentation connection failed while checking pending bytes",
                job_id=job_id,
                cause=error,
            ) from error

    def _receive_child_unlocked(
        self, timeout: float, *, expected_job_id: str | None
    ) -> ChildMessage:
        connection = self._connection
        process = self._process
        if connection is None or process is None:
            raise self._crash_error(
                "segmentation process has no live connection", job_id=expected_job_id
            )
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._crash_error(
                    "segmentation process timed out", job_id=expected_job_id
                )
            try:
                if connection.poll(min(remaining, 0.05)):
                    raw = connection.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES)
                    try:
                        return decode_child_message(raw)
                    except ProtocolCodecError as error:
                        raise _protocol_app_error(
                            f"invalid child protocol bytes: {error}", expected_job_id
                        ) from error
            except AppError:
                raise
            except (EOFError, BrokenPipeError) as error:
                raise self._crash_error(
                    "segmentation process connection closed unexpectedly",
                    job_id=expected_job_id,
                    cause=error,
                ) from error
            except OSError as error:
                if "bad message length" in str(error).lower() and _safe_alive(process):
                    raise _protocol_app_error(
                        f"invalid or oversized child transport frame: {error}",
                        expected_job_id,
                    ) from error
                raise self._crash_error(
                    "segmentation process connection closed unexpectedly",
                    job_id=expected_job_id,
                    cause=error,
                ) from error
            if not _safe_alive(process):
                raise self._crash_error(
                    f"segmentation process exited with code {process.exitcode}",
                    job_id=expected_job_id,
                )

    def _send_parent(
        self, connection: Connection, message: ParentMessage, job_id: str
    ) -> None:
        payload = self._encode_parent(message, job_id)
        self._send_parent_bytes(connection, payload, job_id)

    def _encode_parent(self, message: ParentMessage, job_id: str) -> bytes:
        try:
            return encode_parent_message(message)
        except ProtocolCodecError as error:
            with self._lifecycle_lock:
                self._discard_process_unlocked(graceful=False, job_id=job_id)
            raise _protocol_app_error(
                f"invalid parent protocol message: {error}", job_id
            ) from error

    def _send_parent_bytes(
        self, connection: Connection, payload: bytes, job_id: str
    ) -> None:
        try:
            connection.send_bytes(payload)
        except (BrokenPipeError, EOFError, OSError) as error:
            with self._lifecycle_lock:
                self._discard_process_unlocked(graceful=False, job_id=job_id)
            raise self._crash_error(
                "segmentation connection closed while sending",
                job_id=job_id,
                cause=error,
            ) from error

    def _protocol_error(self, detail: str, job_id: str | None = None) -> AppError:
        with self._lifecycle_lock:
            return self._protocol_error_unlocked(detail, job_id)

    def _protocol_error_unlocked(
        self, detail: str, job_id: str | None = None
    ) -> AppError:
        self._discard_process_unlocked(graceful=False, job_id=job_id)
        return _protocol_app_error(detail, job_id)

    def _discard_process_unlocked(
        self, *, graceful: bool, job_id: str | None = None
    ) -> None:
        connection = self._connection
        child_endpoint = self._child_endpoint
        process = self._process
        errors: list[str] = []
        process_alive = process is not None and _safe_alive(process, errors)
        connection_closed = connection is None
        child_endpoint_closed = child_endpoint is None
        process_closed = process is None
        try:
            if graceful and connection is not None and process_alive:
                try:
                    connection.send_bytes(
                        encode_parent_message(Shutdown(PROTOCOL_VERSION))
                    )
                except BaseException as error:
                    errors.append(
                        f"shutdown send failed: {type(error).__name__}: {error}"
                    )
            if connection is not None:
                try:
                    connection.close()
                    connection_closed = True
                except BaseException as error:
                    errors.append(
                        f"connection close failed: {type(error).__name__}: {error}"
                    )
            if child_endpoint is not None:
                try:
                    child_endpoint.close()
                    child_endpoint_closed = True
                except BaseException as error:
                    errors.append(
                        f"child endpoint close failed: {type(error).__name__}: {error}"
                    )
            if process is not None:
                process_alive = _safe_alive(process, errors)
                if graceful and process_alive:
                    _attempt_process_action(process, "join", errors)
                    process_alive = _safe_alive(process, errors)
                if process_alive:
                    _attempt_process_action(process, "terminate", errors)
                    _attempt_process_action(process, "join", errors)
                    process_alive = _safe_alive(process, errors)
                if process_alive:
                    _attempt_process_action(process, "kill", errors)
                    _attempt_process_action(process, "join", errors)
                    process_alive = _safe_alive(process, errors)
                if not process_alive:
                    process_closed = _attempt_process_action(process, "close", errors)
        finally:
            self._connection = None if connection_closed else connection
            self._child_endpoint = None if child_endpoint_closed else child_endpoint
            self._process = process if process_alive or not process_closed else None
            self._discard_slot_unlocked(errors)
            self._clear_active()
        if process_alive:
            errors.append("old segmentation process remained alive after kill")
        if self._connection is not None:
            errors.append("segmentation connection handle remains after close")
        if self._child_endpoint is not None:
            errors.append("child-side segmentation endpoint remains after close")
        if self._process is not None and not process_alive:
            errors.append("dead segmentation process handle remains after close")
        if self._slot is not None:
            errors.append("segmentation shared-memory handle remains after cleanup")
        if errors:
            raise _cleanup_error("; ".join(errors), job_id)

    def _discard_slot_unlocked(self, errors: list[str] | None = None) -> None:
        slot = self._slot
        if slot is None:
            return
        if not self._slot_closed:
            try:
                slot.close()
                self._slot_closed = True
            except BaseException as error:
                if errors is not None:
                    errors.append(
                        f"shared memory close failed: {type(error).__name__}: {error}"
                    )
        if not self._slot_unlinked:
            try:
                slot.unlink()
                self._slot_unlinked = True
            except FileNotFoundError:
                self._slot_unlinked = True
            except BaseException as error:
                if errors is not None:
                    errors.append(
                        f"shared memory unlink failed: {type(error).__name__}: {error}"
                    )
        if self._slot_closed and self._slot_unlinked:
            self._slot = None
            self._slot_capacity = 0
            self._slot_closed = False
            self._slot_unlinked = False

    def _is_running_unlocked(self) -> bool:
        return (
            self._process is not None
            and self._connection is not None
            and self._child_endpoint is None
            and _safe_alive(self._process)
        )

    def _crash_error(
        self,
        detail: str,
        *,
        job_id: str | None = None,
        cause: BaseException | None = None,
    ) -> AppError:
        if cause is not None:
            detail = f"{detail}: {type(cause).__name__}: {cause}"
        return AppError(
            ErrorCode.SEGMENTATION_PROCESS_CRASHED,
            "segmentation-process",
            "error.segmentation.process-crashed",
            detail,
            "restart-segmentation-process",
            job_id,
        )

    @staticmethod
    def _busy_error(job_id: str | None) -> AppError:
        return AppError(
            ErrorCode.JOB_ALREADY_RUNNING,
            "segmentation",
            "error.job.already-running",
            "the one-slot segmentation process already has a request in flight",
            "wait-for-active-job",
            job_id,
        )


def segmentation_process_main(connection: Connection, model_spec: object) -> None:
    """Frozen child entry: create one session, then enter the exact tested loop."""
    session: object | None = None
    try:
        normalized = _normalize_launch_payload(model_spec)
        session = _create_rembg_session(normalized)
        effective_provider = getattr(
            session, "execution_provider", _launch_provider(normalized)
        )
        startup_notice = getattr(session, "startup_notice", None)
        process_id = multiprocessing.current_process().pid
        if process_id is None:
            return
        _serve_segmentation_connection(
            connection,
            session,
            _run_rembg,
            process_id=process_id,
            execution_provider=effective_provider,
            startup_notice=startup_notice,
        )
    finally:
        session = None
        try:
            connection.close()
        except OSError:
            pass


def _serve_segmentation_connection(
    connection: Connection,
    session: object,
    inference: Inference,
    *,
    process_id: int,
    execution_provider: str | None = None,
    startup_notice: str | None = None,
) -> None:
    """Serve one session/one request using only bounded, schema-checked bytes."""
    if not _send_child(
        connection,
        WorkerReady(
            PROTOCOL_VERSION,
            CONTROL_JOB_ID,
            process_id,
            execution_provider,
            startup_notice,
        ),
    ):
        return
    while True:
        message = _receive_parent(connection)
        if message is None:
            return
        if isinstance(message, Shutdown):
            return
        if isinstance(message, CancelRequest):
            if not _send_child(connection, CancelAck(PROTOCOL_VERSION, message.job_id)):
                return
            continue
        if not isinstance(message, SegmentRequest) or message.slot is None:
            return
        descriptor_error = _validate_wire_descriptor(message.slot)
        if descriptor_error is not None:
            if not _send_child(connection, _failure(message, descriptor_error)):
                return
            continue
        try:
            slot = SharedMemory(name=message.slot.name, create=False)
        except (FileNotFoundError, OSError, ValueError):
            if not _send_child(
                connection, _failure(message, "shared-memory slot is unavailable")
            ):
                return
            continue
        try:
            required_capacity = max(
                message.slot.byte_length,
                message.slot.shape[0] * message.slot.shape[1] * 4,
            )
            if slot.size < required_capacity:
                if not _send_child(
                    connection,
                    _failure(message, "shared-memory slot capacity is invalid"),
                ):
                    return
                continue
            try:
                source = np.ndarray(
                    message.slot.shape, dtype=np.uint8, buffer=slot.buf
                ).copy()
            except BaseException:
                if not _send_child(
                    connection,
                    _failure(message, "shared-memory source buffer is invalid"),
                ):
                    return
                continue
            try:
                result = inference(source, session, message.options)
            except BaseException as error:
                if not _send_child(connection, _inference_failure(message, error)):
                    return
                continue
            shutdown = False
            cancel_requested = False
            while True:
                pending_available = _poll_parent(connection)
                if pending_available is None:
                    return
                if not pending_available:
                    break
                pending = _receive_parent(connection)
                if pending is None:
                    return
                if isinstance(pending, Shutdown):
                    shutdown = True
                elif (
                    isinstance(pending, CancelRequest)
                    and pending.job_id == message.job_id
                ):
                    cancel_requested = True
                else:
                    return
            if cancel_requested:
                if not _send_child(
                    connection, CancelAck(PROTOCOL_VERSION, message.job_id)
                ):
                    return
                if shutdown:
                    return
                continue
            if shutdown:
                return
            expected_shape = (source.shape[0], source.shape[1], 4)
            expected_byte_length = source.shape[0] * source.shape[1] * 4
            if (
                not isinstance(result, np.ndarray)
                or result.dtype != np.dtype(np.uint8)
                or result.shape != expected_shape
                or not result.flags.c_contiguous
                or result.nbytes != expected_byte_length
                or expected_byte_length > slot.size
            ):
                if not _send_child(
                    connection, _failure(message, "inference returned invalid RGBA")
                ):
                    return
                continue
            try:
                output_bytes = result.tobytes(order="C")
                if (
                    type(output_bytes) is not bytes
                    or len(output_bytes) != expected_byte_length
                    or len(output_bytes) != result.nbytes
                    or len(output_bytes) > slot.size
                ):
                    raise ValueError("inference returned invalid output bytes")
                slot_buffer = slot.buf
                if slot_buffer is None:
                    raise BufferError("shared-memory output buffer is unavailable")
                slot_buffer[:expected_byte_length] = output_bytes
            except BaseException:
                if not _send_child(
                    connection,
                    _failure(message, "shared-memory output buffer is invalid"),
                ):
                    return
                continue
            if not _send_child(
                connection,
                SegmentResponse(
                    PROTOCOL_VERSION,
                    message.job_id,
                    message.request_id,
                    expected_shape,
                    "uint8",
                    result.nbytes,
                ),
            ):
                return
        finally:
            slot.close()


def _receive_parent(connection: Connection) -> ParentMessage | None:
    try:
        raw = connection.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES)
        return decode_parent_message(raw)
    except (EOFError, BrokenPipeError, OSError, ProtocolCodecError):
        return None


def _poll_parent(connection: Connection) -> bool | None:
    try:
        return connection.poll()
    except (EOFError, BrokenPipeError, OSError):
        return None


def _send_child(connection: Connection, message: ChildMessage) -> bool:
    try:
        connection.send_bytes(encode_child_message(message))
    except (BrokenPipeError, EOFError, OSError, ProtocolCodecError):
        return False
    return True


def _create_rembg_session(model_spec: dict[str, object]) -> object:
    from rembggui.jobs.models.catalog import ModelCatalog

    verified = _validate_verified_launch_payload(
        model_spec, catalog=ModelCatalog.load_resource()
    )
    return _instantiate_verified_rembg_session(
        verified.model_id,
        verified.model_bytes,
        verified.rembg_version,
        verified.inference_kwargs,
        execution_provider=verified.execution_provider,
    )


@dataclass(frozen=True, slots=True)
class _VerifiedModelLaunch:
    model_id: str
    execution_provider: str
    artifact_path: Path
    rembg_version: str
    size_bytes: int
    sha256: str
    model_bytes: bytes
    inference_kwargs: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _PreparedRembgSession:
    session: object
    inference_kwargs: tuple[tuple[str, str], ...]
    execution_provider: str = CPU_EXECUTION_PROVIDER
    startup_notice: str | None = None


def _validate_verified_launch_payload(
    model_spec: object, *, catalog: object
) -> _VerifiedModelLaunch:
    from rembggui.jobs.models.catalog import ExecutionClass, ModelCatalog

    expected_keys = {
        "schema_version",
        "model_id",
        "upstream_id",
        "rembg_version",
        "model_home",
        "runtime_filename",
        "sha256",
        "size_bytes",
        "inference_defaults",
        "execution_provider",
    }
    if type(catalog) is not ModelCatalog:
        raise _model_preparation_error("child model catalog is not authoritative")
    if type(model_spec) is not dict or set(model_spec) != expected_keys:
        raise _model_preparation_error(
            "child model launch payload has missing or unknown fields"
        )
    schema_version = model_spec.get("schema_version")
    model_id = model_spec.get("model_id")
    upstream_id = model_spec.get("upstream_id")
    rembg_version = model_spec.get("rembg_version")
    model_home = model_spec.get("model_home")
    runtime_filename = model_spec.get("runtime_filename")
    sha256 = model_spec.get("sha256")
    size_bytes = model_spec.get("size_bytes")
    inference_defaults = model_spec.get("inference_defaults")
    execution_provider = model_spec.get("execution_provider")
    if type(schema_version) is not int or schema_version != 1:
        raise _model_preparation_error("child model launch schema is invalid")
    if type(model_id) is not str:
        raise _model_preparation_error("child model ID is invalid")
    if not is_allowed_provider(execution_provider):
        raise _model_preparation_error("child execution provider is invalid")
    try:
        spec = catalog.get(model_id)
    except AppError as error:
        raise _model_preparation_error(
            "child model ID is not a pinned built-in"
        ) from error
    artifact = spec.artifact
    if spec.execution_class is not ExecutionClass.LOCAL or artifact is None:
        raise _model_preparation_error("child launch requires a local built-in model")
    if (
        upstream_id != spec.upstream_id
        or rembg_version != catalog.rembg_version
        or runtime_filename != artifact.runtime_filename
        or sha256 != artifact.sha256
        or type(size_bytes) is not int
        or size_bytes != artifact.size_bytes
        or inference_defaults != spec.inference_defaults.to_primitives()
    ):
        raise _model_preparation_error(
            "child model launch payload does not match the pinned manifest"
        )
    if type(model_home) is not str or not model_home:
        raise _model_preparation_error("child model home is invalid")
    home = Path(model_home)
    if (
        not home.is_absolute()
        or home.name != spec.id
        or home.parent.name != catalog.rembg_version
    ):
        raise _model_preparation_error(
            "child model home is outside the version/model cache namespace"
        )
    cache_root = home.parent.parent
    try:
        bound = BoundModelDirectory.bind(
            cache_root,
            catalog.rembg_version,
            spec.id,
            create=False,
        )
    except UnsafeCacheError as error:
        raise _model_cache_error(str(error)) from error
    except OSError as error:
        raise _model_cache_error(
            f"child model namespace cannot be bound: {type(error).__name__}"
        ) from error
    if bound is None:
        raise _model_cache_error("child model namespace does not exist")
    artifact_path = bound.target(artifact.runtime_filename)
    try:
        with bound:
            model_bytes = _read_verified_bound_artifact(
                bound,
                artifact.runtime_filename,
                expected_size=artifact.size_bytes,
                expected_sha256=artifact.sha256,
            )
            bound.assert_still_named()
    except BoundDirectoryCloseError as error:
        cause = (
            error.primary_error
            if error.primary_error is not None
            else error.close_error
        )
        raise _model_cache_error(
            f"child model namespace cleanup failed: {type(error.close_error).__name__}"
        ) from cause
    except UnsafeCacheError as error:
        raise _model_cache_error(str(error)) from error
    return _VerifiedModelLaunch(
        spec.id,
        execution_provider,
        artifact_path,
        catalog.rembg_version,
        artifact.size_bytes,
        artifact.sha256,
        model_bytes,
        tuple(spec.inference_defaults.to_primitives().items()),
    )


def _read_verified_bound_artifact(
    bound: BoundModelDirectory,
    filename: str,
    *,
    expected_size: int,
    expected_sha256: str,
) -> bytes:
    try:
        before = bound.lstat(filename)
    except OSError as error:
        raise _model_cache_error(
            f"verified model artifact cannot be inspected: {type(error).__name__}"
        ) from error
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size != expected_size
    ):
        raise _model_cache_error(
            "verified model artifact is not a size-matched regular file"
        )
    try:
        descriptor = bound.open_read(filename)
    except OSError as error:
        raise _model_cache_error(
            f"verified model artifact cannot be opened: {type(error).__name__}"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != expected_size
        ):
            raise _model_cache_error("verified model artifact changed before hashing")
        with os.fdopen(descriptor, "rb", closefd=False) as model_file:
            model_bytes = model_file.read(expected_size + 1)
    except AppError:
        raise
    except OSError as error:
        raise _model_cache_error(
            f"verified model artifact cannot be hashed: {type(error).__name__}"
        ) from error
    finally:
        os.close(descriptor)
    try:
        after = bound.lstat(filename)
    except OSError as error:
        raise _model_cache_error(
            f"verified model artifact disappeared after hashing: {type(error).__name__}"
        ) from error
    if (
        after.st_dev != opened.st_dev
        or after.st_ino != opened.st_ino
        or after.st_size != expected_size
        or len(model_bytes) != expected_size
        or hashlib.sha256(model_bytes).hexdigest() != expected_sha256
    ):
        raise _model_preparation_error(
            "verified model artifact failed the child SHA-256 proof"
        )
    return model_bytes


def _instantiate_verified_rembg_session(
    model_id: str,
    model_bytes: bytes,
    rembg_version: str,
    inference_kwargs: tuple[tuple[str, str], ...] = (),
    *,
    execution_provider: str = CPU_EXECUTION_PROVIDER,
    session_classes: object | None = None,
    ort_module: object | None = None,
    installed_version: str | None = None,
) -> object:
    """Construct the pinned rembg class around the exact SHA-proven bytes."""
    if type(model_bytes) is not bytes:
        raise _model_preparation_error("verified model content is not immutable bytes")
    if ort_module is None:
        import onnxruntime as ort_module  # type: ignore[import-untyped,no-redef]
    if session_classes is None:
        from rembg.sessions import sessions_class  # type: ignore[import-untyped]

        session_classes = sessions_class
    session_class = _resolve_rembg_session_class(
        model_id,
        session_classes,
        rembg_version,
        installed_version,
    )
    runtime: Any = ort_module
    if not is_allowed_provider(execution_provider):
        raise _model_preparation_error("execution provider is not allowlisted")
    session_options = _session_options(runtime)
    available = _available_runtime_providers(runtime)
    if execution_provider != CPU_EXECUTION_PROVIDER and (
        execution_provider not in available
    ):
        return _create_cpu_fallback_session(
            session_class,
            model_id,
            model_bytes,
            session_options,
            runtime,
            inference_kwargs,
            execution_provider,
            RuntimeError("provider is not reported by ONNX Runtime"),
        )
    return _construct_requested_session(
        session_class,
        model_id,
        model_bytes,
        session_options,
        runtime,
        inference_kwargs,
        execution_provider,
    )


def _resolve_rembg_session_class(
    model_id: str,
    session_classes: object,
    rembg_version: str,
    installed_version: str | None,
) -> Any:
    if installed_version is None:
        try:
            installed_version = package_version("rembg")
        except PackageNotFoundError as error:
            raise _model_preparation_error(
                "pinned rembg runtime is not installed"
            ) from error
    if installed_version != rembg_version:
        raise _model_preparation_error(
            "installed rembg runtime does not match the verified model namespace"
        )
    if not isinstance(session_classes, list):
        raise _model_preparation_error("rembg session registry is invalid")
    for candidate in session_classes:
        if candidate.name() == model_id:
            return candidate
    raise _model_preparation_error("verified built-in rembg session is unavailable")


def _session_options(runtime: Any) -> object:
    options = runtime.SessionOptions()
    options.enable_profiling = False
    if platform.system() == "Darwin":
        optimization = getattr(
            getattr(runtime, "GraphOptimizationLevel", None), "ORT_ENABLE_ALL", None
        )
        mode = getattr(getattr(runtime, "ExecutionMode", None), "ORT_SEQUENTIAL", None)
        if optimization is not None:
            options.graph_optimization_level = optimization
        if mode is not None:
            options.execution_mode = mode
        options.intra_op_num_threads = 0
    return options


def _available_runtime_providers(runtime: Any) -> tuple[str, ...]:
    try:
        return tuple(runtime.get_available_providers())
    except (AttributeError, TypeError) as error:
        raise _model_preparation_error(
            "ONNX Runtime did not report execution providers"
        ) from error


def _construct_requested_session(
    session_class: Any,
    model_id: str,
    model_bytes: bytes,
    session_options: object,
    runtime: Any,
    inference_kwargs: tuple[tuple[str, str], ...],
    execution_provider: str,
) -> _PreparedRembgSession:
    providers = (
        [CPU_EXECUTION_PROVIDER]
        if execution_provider == CPU_EXECUTION_PROVIDER
        else [execution_provider, CPU_EXECUTION_PROVIDER]
    )
    try:
        session = session_class.__new__(session_class)
        session.model_name = model_id
        session.inner_session = runtime.InferenceSession(
            model_bytes,
            sess_options=session_options,
            providers=providers,
        )
    except Exception as error:
        if execution_provider == CPU_EXECUTION_PROVIDER:
            raise _model_preparation_error(
                "verified ONNX session could not be constructed: "
                f"{type(error).__name__}"
            ) from error
        return _create_cpu_fallback_session(
            session_class,
            model_id,
            model_bytes,
            session_options,
            runtime,
            inference_kwargs,
            execution_provider,
            error,
        )
    return _PreparedRembgSession(session, inference_kwargs, execution_provider)


def _create_cpu_fallback_session(
    session_class: Any,
    model_id: str,
    model_bytes: bytes,
    session_options: object,
    runtime: Any,
    inference_kwargs: tuple[tuple[str, str], ...],
    requested_provider: str,
    error: BaseException,
) -> _PreparedRembgSession:
    label = provider_base_label(requested_provider)
    _LOGGER.error(
        "ONNX Runtime provider %s could not initialise model %s; using CPU: %s",
        requested_provider,
        model_id,
        error,
    )
    try:
        session = session_class.__new__(session_class)
        session.model_name = model_id
        session.inner_session = runtime.InferenceSession(
            model_bytes,
            sess_options=session_options,
            providers=[CPU_EXECUTION_PROVIDER],
        )
    except Exception as fallback_error:
        raise _model_preparation_error(
            "verified ONNX CPU fallback session could not be constructed: "
            f"{type(fallback_error).__name__}"
        ) from fallback_error
    return _PreparedRembgSession(
        session,
        inference_kwargs,
        CPU_EXECUTION_PROVIDER,
        f"{label} konnte dieses Modell nicht laden. Die Verarbeitung wird "
        "automatisch über die CPU fortgesetzt.",
    )


def _model_preparation_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.MODEL_PREPARATION_INVALID,
        "model-session",
        "error.model.preparation-invalid",
        detail,
        "retry-model-preparation",
    )


def _model_cache_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.MODEL_CACHE_UNSAFE,
        "model-cache",
        "error.model.cache-unsafe",
        detail,
        "reacquire-model",
    )


def _run_rembg(
    source: Uint8Frame, session: object, options: SegmentOptions
) -> Uint8Frame:
    options = validate_segment_options(options)
    from rembg import remove  # type: ignore[import-untyped]

    actual_session = session
    inference_kwargs: dict[str, object] = {}
    if type(session) is _PreparedRembgSession:
        actual_session = session.session
        inference_kwargs = dict(session.inference_kwargs)
    inference_kwargs["alpha_matting"] = options.edge_mode == "alpha_matting"
    if options.edge_mode == "alpha_matting":
        inference_kwargs.update(
            {
                "alpha_matting_foreground_threshold": (
                    options.alpha_matting_foreground_threshold
                ),
                "alpha_matting_background_threshold": (
                    options.alpha_matting_background_threshold
                ),
                "alpha_matting_erode_size": options.alpha_matting_erode_size,
            }
        )
    result = np.asarray(remove(source, session=actual_session, **inference_kwargs))
    if result.dtype != np.dtype(np.uint8):
        raise ValueError("rembg output dtype is not uint8")
    return np.ascontiguousarray(result)


def _validate_wire_descriptor(slot: SharedFrame) -> str | None:
    if (
        type(slot.name) is not str
        or not slot.name
        or type(slot.shape) is not tuple
        or len(slot.shape) != 3
        or any(type(value) is not int for value in slot.shape)
        or not 1 <= slot.shape[0] <= _MAX_FRAME_DIMENSION
        or not 1 <= slot.shape[1] <= _MAX_FRAME_DIMENSION
        or slot.shape[2] not in {3, 4}
        or slot.dtype != "uint8"
        or type(slot.byte_length) is not int
        or slot.byte_length != slot.shape[0] * slot.shape[1] * slot.shape[2]
    ):
        return "shared-frame descriptor is invalid"
    return None


@dataclass(slots=True)
class _LaunchBudget:
    nodes: int = 0
    text_bytes: int = 0
    estimated_bytes: int = 0

    def add_node(self, estimated_bytes: int = 1) -> None:
        self.nodes += 1
        self.estimated_bytes += estimated_bytes
        if self.nodes > _MAX_LAUNCH_NODES:
            raise _protocol_app_error("model launch payload has too many nodes")
        if self.estimated_bytes > _MAX_LAUNCH_PAYLOAD_BYTES:
            raise _protocol_app_error("model launch payload exceeds its byte limit")

    def add_estimate(self, estimated_bytes: int) -> None:
        self.estimated_bytes += estimated_bytes
        if self.estimated_bytes > _MAX_LAUNCH_PAYLOAD_BYTES:
            raise _protocol_app_error("model launch payload exceeds its byte limit")

    def add_text(self, value: str, *, key: bool = False) -> None:
        if len(value) > _MAX_LAUNCH_TEXT_BYTES:
            raise _protocol_app_error("model launch payload text is too large")
        try:
            byte_length = len(value.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise _protocol_app_error(
                "model launch payload contains an isolated Unicode surrogate"
            ) from error
        self.text_bytes += byte_length
        self.estimated_bytes += _json_string_encoded_length(value)
        if key:
            self.estimated_bytes += 1
        if self.text_bytes > _MAX_LAUNCH_TEXT_BYTES:
            raise _protocol_app_error("model launch payload text budget is exceeded")
        if self.estimated_bytes > _MAX_LAUNCH_PAYLOAD_BYTES:
            raise _protocol_app_error("model launch payload exceeds its byte limit")


def _json_string_encoded_length(value: str) -> int:
    length = 2
    for character in value:
        codepoint = ord(character)
        if character in {'"', "\\"}:
            length += 2
        elif codepoint < 0x20 or codepoint <= 0xFFFF and codepoint > 0x7E:
            length += 6
        elif codepoint > 0xFFFF:
            length += 12
        else:
            length += 1
    return length


def _normalize_launch_payload(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise _protocol_app_error("model launch payload must be an exact built-in dict")
    cloned = _clone_json_safe(value, depth=0, budget=_LaunchBudget(), ancestors=set())
    assert type(cloned) is dict
    try:
        encoded = json.dumps(
            cloned,
            allow_nan=False,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError) as error:
        raise _protocol_app_error("model launch payload is not JSON-safe") from error
    if len(encoded) > _MAX_LAUNCH_PAYLOAD_BYTES:
        raise _protocol_app_error("model launch payload exceeds its byte limit")
    normalized = json.loads(encoded.decode("ascii"))
    assert type(normalized) is dict
    return normalized


def _launch_provider(model_spec: dict[str, object]) -> str:
    provider = model_spec.get("execution_provider")
    return (
        provider
        if isinstance(provider, str) and is_allowed_provider(provider)
        else CPU_EXECUTION_PROVIDER
    )


def _clone_json_safe(
    value: object,
    *,
    depth: int,
    budget: _LaunchBudget,
    ancestors: set[int],
) -> object:
    if depth > _MAX_LAUNCH_DEPTH:
        raise _protocol_app_error("model launch payload nesting is too deep")
    budget.add_node()
    if value is None or type(value) is bool:
        return value
    if type(value) is str:
        budget.add_text(value)
        return value
    if type(value) is int:
        estimated_digits = max(1, math.ceil(value.bit_length() * math.log10(2)))
        if value < 0:
            estimated_digits += 1
        budget.add_estimate(estimated_digits)
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _protocol_app_error("model launch payload has a non-finite number")
        budget.add_estimate(32)
        return value
    if type(value) is list:
        if len(value) > _MAX_LAUNCH_ITEMS:
            raise _protocol_app_error("model launch list has too many items")
        identity = id(value)
        if identity in ancestors:
            raise _protocol_app_error("model launch payload contains a cycle")
        ancestors.add(identity)
        try:
            return [
                _clone_json_safe(
                    item,
                    depth=depth + 1,
                    budget=budget,
                    ancestors=ancestors,
                )
                for item in value
            ]
        finally:
            ancestors.remove(identity)
    if type(value) is dict:
        if len(value) > _MAX_LAUNCH_ITEMS:
            raise _protocol_app_error("model launch object has too many fields")
        identity = id(value)
        if identity in ancestors:
            raise _protocol_app_error("model launch payload contains a cycle")
        ancestors.add(identity)
        try:
            result: dict[str, object] = {}
            for key, item in value.items():
                if type(key) is not str or not key or len(key) > 256:
                    raise _protocol_app_error(
                        "model launch keys must be bounded strings"
                    )
                budget.add_text(key, key=True)
                result[key] = _clone_json_safe(
                    item,
                    depth=depth + 1,
                    budget=budget,
                    ancestors=ancestors,
                )
            return result
        finally:
            ancestors.remove(identity)
    raise _protocol_app_error("model launch payload contains a custom object")


def _attempt_process_action(
    process: BaseProcess, action: str, errors: list[str]
) -> bool:
    try:
        method = getattr(process, action)
        if action == "join":
            method(_JOIN_TIMEOUT_SECONDS)
        else:
            method()
    except BaseException as error:
        errors.append(f"process {action} failed: {type(error).__name__}: {error}")
        return False
    return True


def _safe_alive(process: BaseProcess, errors: list[str] | None = None) -> bool:
    try:
        return process.is_alive()
    except BaseException as error:
        if errors is not None:
            errors.append(f"process liveness failed: {type(error).__name__}: {error}")
        return True


def _failure(request: SegmentRequest, detail: str) -> SegmentFailure:
    error = _protocol_app_error(detail, request.job_id)
    return SegmentFailure(
        PROTOCOL_VERSION,
        request.job_id,
        request.request_id,
        error.to_primitives(),
    )


def _inference_failure(request: SegmentRequest, error: BaseException) -> SegmentFailure:
    app_error = AppError(
        ErrorCode.INVALID_SEGMENTATION,
        "segmentation",
        "error.segmentation.inference-failed",
        f"{type(error).__name__}: {error}",
        "retry-job",
        request.job_id,
    )
    return SegmentFailure(
        PROTOCOL_VERSION,
        request.job_id,
        request.request_id,
        app_error.to_primitives(),
    )


def _protocol_app_error(detail: str, job_id: str | None = None) -> AppError:
    return AppError(
        ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH,
        "segmentation-protocol",
        "error.segmentation.protocol-mismatch",
        detail,
        "restart-segmentation-process",
        job_id,
    )


def _cleanup_error(detail: str, job_id: str | None = None) -> AppError:
    return AppError(
        ErrorCode.SEGMENTATION_CLEANUP_FAILED,
        "segmentation-cleanup",
        "error.segmentation.cleanup-failed",
        detail,
        "restart-application",
        job_id,
    )


def freeze_support_entry() -> None:
    multiprocessing.freeze_support()


if __name__ == "__main__":  # pragma: no cover - frozen artifact path
    freeze_support_entry()
