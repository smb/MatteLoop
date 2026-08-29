"""Spawned rembg host with a bounded byte protocol and parent-owned frame slot.

Ownership and lock/protocol order::

    parent                                   spawned child
    ------                                   -------------
    owns + unlinks SHM  -- descriptor -----> attaches/closes SHM only
    wire lock: Request   -- send_bytes -----> one session / one inference
      then Cancel        -- send_bytes -----> observed after inference
    validates metadata   <- recv_bytes ------ Response OR matching CancelAck

The active identity is published before request transport. Wire serialization is
always ``SegmentRequest`` then an admitted ``CancelRequest``. Code never holds the
state lock while acquiring the wire/lifecycle locks; nested order is
``wire -> lifecycle -> state``. A dead child, malformed bytes, protocol mismatch,
or teardown anomaly invalidates the slot and cannot implicitly spawn a replacement.
"""

from __future__ import annotations

import json
import math
import multiprocessing
import time
from collections.abc import Callable
from dataclasses import replace
from multiprocessing.connection import Connection
from multiprocessing.context import BaseContext
from multiprocessing.process import BaseProcess
from multiprocessing.shared_memory import SharedMemory
from threading import Lock, RLock
from typing import Any

import numpy as np
from numpy.typing import NDArray

from rembggui.core.errors import AppError, ErrorCode
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
    SegmentRequest,
    SegmentResponse,
    SharedFrame,
    Shutdown,
    WorkerReady,
    decode_child_message,
    decode_parent_message,
    encode_child_message,
    encode_parent_message,
)

_JOIN_TIMEOUT_SECONDS = 1.0
_MAX_FRAME_DIMENSION = 16_383
_MAX_LAUNCH_PAYLOAD_BYTES = 16 * 1024
_MAX_LAUNCH_DEPTH = 6
_MAX_LAUNCH_ITEMS = 256

type Uint8Frame = NDArray[np.uint8]
type ChildTarget = Callable[[Connection, object], None]
type Inference = Callable[[Uint8Frame, object], Uint8Frame]


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
        self._process: BaseProcess | None = None
        self._connection: Connection | None = None
        self._slot: SharedMemory | None = None
        self._slot_capacity = 0
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

    def start(self) -> None:
        """Start a fresh child; after invalidation this is the explicit retry."""
        with self._lifecycle_lock:
            if self._is_running_unlocked():
                return
            self._discard_process_unlocked(graceful=False)
            parent, child = self._mp_context.Pipe(duplex=True)
            process = self._mp_context.Process(  # type: ignore[attr-defined]
                target=self._child_target,
                args=(child, self._model_spec),
                name="rembggui-segmentation",
                daemon=True,
            )
            try:
                process.start()
            except BaseException as error:
                parent.close()
                child.close()
                raise self._crash_error(
                    "segmentation process could not be spawned", cause=error
                ) from error
            child.close()
            self._process = process
            self._connection = parent
            try:
                message = self._receive_child_unlocked(self._startup_timeout)
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
            except BaseException:
                self._discard_process_unlocked(graceful=False)
                raise

    def segment(
        self, image: np.ndarray[Any, Any], request: SegmentRequest
    ) -> Uint8Frame:
        """Process one RGB(A) frame and return a private validated RGBA copy."""
        if not self._operation_lock.acquire(blocking=False):
            raise self._busy_error(
                request.job_id if isinstance(request, SegmentRequest) else None
            )
        try:
            self._validate_request(request)
            self._publish_active(request)
            frame = self._validate_image(image)
            with self._lifecycle_lock:
                if not self._is_running_unlocked():
                    self._discard_process_unlocked(graceful=False)
                    raise self._crash_error(
                        "segmentation process is not running; explicit retry required",
                        job_id=request.job_id,
                    )
                self._reject_pending_messages_unlocked(request.job_id)
                output_capacity = frame.shape[0] * frame.shape[1] * 4
                slot = self._ensure_slot_unlocked(output_capacity, request.job_id)
                input_bytes = frame.tobytes(order="C")
                slot_buffer = slot.buf
                assert slot_buffer is not None
                slot_buffer[: len(input_bytes)] = input_bytes
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
            with self._wire_lock:
                self._before_request_wire_send()
                self._send_parent(connection, wire_request, request.job_id)
                with self._state_lock:
                    self._request_sent = True
                    send_cancel = self._cancel_requested and not self._cancel_wire_sent
                if send_cancel:
                    self._send_cancel_on_wire(connection, request.job_id)
            return self._await_result(frame.shape[:2], request)
        finally:
            self._clear_active()
            self._operation_lock.release()

    def cancel(self, job_id: str) -> bool:
        """Admit one cancel and serialize it strictly after its SegmentRequest."""
        if not isinstance(job_id, str) or not job_id:
            return False
        with self._state_lock:
            if self._active_job_id != job_id or self._cancel_requested:
                return False
            self._cancel_requested = True
        self._after_cancel_admitted()
        with self._wire_lock:
            with self._state_lock:
                should_send = (
                    self._active_job_id == job_id
                    and self._request_sent
                    and not self._cancel_wire_sent
                )
            if should_send:
                with self._lifecycle_lock:
                    if not self._is_running_unlocked() or self._connection is None:
                        return False
                    connection = self._connection
                self._send_cancel_on_wire(connection, job_id)
        return True

    def replace_model(self, model_spec: object) -> None:
        """Prove the old process dead before starting the normalized replacement."""
        normalized = _normalize_launch_payload(model_spec)
        if not self._operation_lock.acquire(blocking=False):
            raise self._busy_error(self.active_job_id)
        try:
            with self._wire_lock:
                with self._lifecycle_lock:
                    self._discard_process_unlocked(graceful=True)
                    self._model_spec = normalized
            self.start()
        finally:
            self._operation_lock.release()

    def close(self) -> None:
        """Stop, prove dead, and unlink parent state; safe to call repeatedly."""
        with self._wire_lock:
            with self._lifecycle_lock:
                self._discard_process_unlocked(graceful=True)

    def _before_request_wire_send(self) -> None:
        """Test seam called while the wire lock enforces Request-before-Cancel."""

    def _after_cancel_admitted(self) -> None:
        """Test seam called after state admission and before wire serialization."""

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
            message = self._receive_child_unlocked(self._response_timeout)
        except AppError:
            with self._lifecycle_lock:
                self._discard_process_unlocked(graceful=False)
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
                    self._discard_process_unlocked(graceful=False)
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
            if self._connection.poll():
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
            acknowledgement = self._receive_child_unlocked(self._response_timeout)
        except AppError:
            with self._lifecycle_lock:
                self._discard_process_unlocked(graceful=False)
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
            or request.protocol_version != PROTOCOL_VERSION
            or type(request.job_id) is not str
            or not request.job_id
            or type(request.request_id) is not str
            or not request.request_id
            or request.slot is not None
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
        if self._slot is not None and self._slot_capacity >= capacity:
            return self._slot
        cleanup_errors: list[str] = []
        self._discard_slot_unlocked(cleanup_errors)
        if cleanup_errors:
            raise _cleanup_error("; ".join(cleanup_errors))
        try:
            self._slot = SharedMemory(create=True, size=capacity)
        except (OSError, ValueError) as error:
            raise self._crash_error(
                "could not allocate the parent-owned segmentation frame slot",
                job_id=job_id,
                cause=error,
            ) from error
        self._slot_capacity = capacity
        return self._slot

    def _reject_pending_messages_unlocked(self, job_id: str) -> None:
        if self._connection is not None and self._connection.poll():
            raise self._protocol_error_unlocked(
                "stale child response remained before a new request", job_id
            )

    def _receive_child_unlocked(self, timeout: float) -> ChildMessage:
        connection = self._connection
        process = self._process
        if connection is None or process is None:
            raise self._crash_error("segmentation process has no live connection")
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._crash_error("segmentation process timed out")
            try:
                if connection.poll(min(remaining, 0.05)):
                    raw = connection.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES)
                    try:
                        return decode_child_message(raw)
                    except ProtocolCodecError as error:
                        raise _protocol_app_error(
                            f"invalid child protocol bytes: {error}"
                        ) from error
            except AppError:
                raise
            except (EOFError, BrokenPipeError) as error:
                raise self._crash_error(
                    "segmentation process connection closed unexpectedly", cause=error
                ) from error
            except OSError as error:
                if "bad message length" in str(error).lower() and _safe_alive(process):
                    raise _protocol_app_error(
                        f"invalid or oversized child transport frame: {error}"
                    ) from error
                raise self._crash_error(
                    "segmentation process connection closed unexpectedly", cause=error
                ) from error
            if not _safe_alive(process):
                raise self._crash_error(
                    f"segmentation process exited with code {process.exitcode}"
                )

    def _send_parent(
        self, connection: Connection, message: ParentMessage, job_id: str
    ) -> None:
        try:
            payload = encode_parent_message(message)
            connection.send_bytes(payload)
        except ProtocolCodecError as error:
            with self._lifecycle_lock:
                self._discard_process_unlocked(graceful=False)
            raise _protocol_app_error(
                f"invalid parent protocol message: {error}", job_id
            ) from error
        except (BrokenPipeError, EOFError, OSError) as error:
            with self._lifecycle_lock:
                self._discard_process_unlocked(graceful=False)
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
        self._discard_process_unlocked(graceful=False)
        return _protocol_app_error(detail, job_id)

    def _discard_process_unlocked(self, *, graceful: bool) -> None:
        connection = self._connection
        process = self._process
        errors: list[str] = []
        process_alive = process is not None and _safe_alive(process, errors)
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
                except BaseException as error:
                    errors.append(
                        f"connection close failed: {type(error).__name__}: {error}"
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
                    _attempt_process_action(process, "close", errors)
        finally:
            self._connection = None
            self._process = process if process_alive else None
            self._discard_slot_unlocked(errors)
            self._clear_active()
        if process_alive:
            errors.append("old segmentation process remained alive after kill")
        if errors:
            raise _cleanup_error("; ".join(errors))

    def _discard_slot_unlocked(self, errors: list[str] | None = None) -> None:
        slot = self._slot
        if slot is None:
            return
        try:
            slot.close()
        except BaseException as error:
            if errors is not None:
                errors.append(
                    f"shared memory close failed: {type(error).__name__}: {error}"
                )
        unlinked = False
        try:
            slot.unlink()
            unlinked = True
        except FileNotFoundError:
            unlinked = True
        except BaseException as error:
            if errors is not None:
                errors.append(
                    f"shared memory unlink failed: {type(error).__name__}: {error}"
                )
        if unlinked:
            self._slot = None
            self._slot_capacity = 0

    def _is_running_unlocked(self) -> bool:
        return (
            self._process is not None
            and self._connection is not None
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
        process_id = multiprocessing.current_process().pid
        if process_id is None:
            return
        _serve_segmentation_connection(
            connection, session, _run_rembg, process_id=process_id
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
) -> None:
    """Serve one session/one request using only bounded, schema-checked bytes."""
    if not _send_child(
        connection, WorkerReady(PROTOCOL_VERSION, CONTROL_JOB_ID, process_id)
    ):
        return
    cancelled: set[str] = set()
    while True:
        message = _receive_parent(connection)
        if message is None:
            return
        if isinstance(message, Shutdown):
            return
        if isinstance(message, CancelRequest):
            if message.job_id not in cancelled:
                cancelled.add(message.job_id)
                if not _send_child(
                    connection, CancelAck(PROTOCOL_VERSION, message.job_id)
                ):
                    return
            continue
        if not isinstance(message, SegmentRequest) or message.slot is None:
            return
        descriptor_error = _validate_wire_descriptor(message.slot)
        if descriptor_error is not None:
            _send_child(connection, _failure(message, descriptor_error))
            return
        try:
            slot = SharedMemory(name=message.slot.name, create=False)
        except (FileNotFoundError, OSError, ValueError):
            _send_child(
                connection, _failure(message, "shared-memory slot is unavailable")
            )
            return
        try:
            required_capacity = max(
                message.slot.byte_length,
                message.slot.shape[0] * message.slot.shape[1] * 4,
            )
            if slot.size < required_capacity:
                _send_child(
                    connection,
                    _failure(message, "shared-memory slot capacity is invalid"),
                )
                return
            source = np.ndarray(
                message.slot.shape, dtype=np.uint8, buffer=slot.buf
            ).copy()
            try:
                result = inference(source, session)
            except BaseException as error:
                _send_child(connection, _inference_failure(message, error))
                continue
            shutdown = False
            while connection.poll():
                pending = _receive_parent(connection)
                if pending is None:
                    return
                if isinstance(pending, Shutdown):
                    shutdown = True
                elif (
                    isinstance(pending, CancelRequest)
                    and pending.job_id == message.job_id
                ):
                    cancelled.add(pending.job_id)
                else:
                    return
            if shutdown:
                return
            if message.job_id in cancelled:
                if not _send_child(
                    connection, CancelAck(PROTOCOL_VERSION, message.job_id)
                ):
                    return
                continue
            expected_shape = (source.shape[0], source.shape[1], 4)
            if (
                not isinstance(result, np.ndarray)
                or result.dtype != np.dtype(np.uint8)
                or result.shape != expected_shape
                or not result.flags.c_contiguous
                or result.nbytes != source.shape[0] * source.shape[1] * 4
                or result.nbytes > slot.size
            ):
                _send_child(
                    connection, _failure(message, "inference returned invalid RGBA")
                )
                return
            slot_buffer = slot.buf
            assert slot_buffer is not None
            slot_buffer[: result.nbytes] = result.tobytes(order="C")
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


def _send_child(connection: Connection, message: ChildMessage) -> bool:
    try:
        connection.send_bytes(encode_child_message(message))
    except (BrokenPipeError, EOFError, OSError, ProtocolCodecError):
        return False
    return True


def _create_rembg_session(model_spec: dict[str, object]) -> object:
    from rembg import new_session  # type: ignore[import-untyped]

    model_id = model_spec.get("upstream_id", model_spec.get("model_id"))
    if type(model_id) is not str or not model_id:
        raise ValueError("local model launch payload has no upstream model ID")
    return new_session(model_id)


def _run_rembg(source: Uint8Frame, session: object) -> Uint8Frame:
    from rembg import remove

    result = np.asarray(remove(source, session=session))
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


def _normalize_launch_payload(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise _protocol_app_error("model launch payload must be an exact built-in dict")
    cloned = _clone_json_safe(value, depth=0)
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


def _clone_json_safe(value: object, *, depth: int) -> object:
    if depth > _MAX_LAUNCH_DEPTH:
        raise _protocol_app_error("model launch payload nesting is too deep")
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _protocol_app_error("model launch payload has a non-finite number")
        return value
    if type(value) is list:
        if len(value) > _MAX_LAUNCH_ITEMS:
            raise _protocol_app_error("model launch list has too many items")
        return [_clone_json_safe(item, depth=depth + 1) for item in value]
    if type(value) is dict:
        if len(value) > _MAX_LAUNCH_ITEMS:
            raise _protocol_app_error("model launch object has too many fields")
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str or not key or len(key) > 256:
                raise _protocol_app_error("model launch keys must be bounded strings")
            result[key] = _clone_json_safe(item, depth=depth + 1)
        return result
    raise _protocol_app_error("model launch payload contains a custom object")


def _attempt_process_action(
    process: BaseProcess, action: str, errors: list[str]
) -> None:
    try:
        method = getattr(process, action)
        if action == "join":
            method(_JOIN_TIMEOUT_SECONDS)
        else:
            method()
    except BaseException as error:
        errors.append(f"process {action} failed: {type(error).__name__}: {error}")


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


def _cleanup_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.SEGMENTATION_CLEANUP_FAILED,
        "segmentation-cleanup",
        "error.segmentation.cleanup-failed",
        detail,
        "restart-application",
    )


def freeze_support_entry() -> None:
    multiprocessing.freeze_support()


if __name__ == "__main__":  # pragma: no cover - frozen artifact path
    freeze_support_entry()
