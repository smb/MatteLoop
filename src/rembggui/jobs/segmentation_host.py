"""Spawned rembg host with one parent-owned, reusable shared-memory slot.

Ownership and request protocol::

    GUI parent                              spawned segmentation child
    ----------                              --------------------------
    owns duplex Pipe end  <---- messages --> owns duplex Pipe end
    owns + unlinks SHM     ---- slot name --> attaches/closes SHM only
    validates RGB(A)       ---- Request ----> owns one session, one inference
    trusts no SHM bytes    <--- Response ---- validates/writes one RGBA frame
    terminate + join       <--- CancelAck --- ack only after native call is safe

There is exactly one request in flight. A crash or any identity/version/size
mismatch invalidates both process and slot. Recovery is never implicit: callers
must explicitly call :meth:`SegmentationClient.start` (or replace the model).
"""

from __future__ import annotations

import multiprocessing
import pickle
import time
from collections.abc import Callable, Mapping
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
    PROTOCOL_VERSION,
    CancelAck,
    CancelRequest,
    SegmentFailure,
    SegmentRequest,
    SegmentResponse,
    SharedFrame,
    Shutdown,
    WorkerReady,
)

_JOIN_TIMEOUT_SECONDS = 1.0
_MAX_FRAME_DIMENSION = 16_383

type Uint8Frame = NDArray[np.uint8]
type ChildTarget = Callable[[Connection, object], None]


class SegmentationClient:
    """Parent-side lifecycle and trust boundary for one segmentation process."""

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
        self._model_spec = model_spec
        self._child_target = (
            child_target if child_target is not None else segmentation_process_main
        )
        self._startup_timeout = startup_timeout
        self._response_timeout = response_timeout
        self._mp_context = (
            mp_context
            if mp_context is not None
            else multiprocessing.get_context("spawn")
        )
        self._lifecycle_lock = RLock()
        self._state_lock = Lock()
        self._send_lock = Lock()
        self._operation_lock = Lock()
        self._process: BaseProcess | None = None
        self._connection: Connection | None = None
        self._slot: SharedMemory | None = None
        self._slot_capacity = 0
        self._active_job_id: str | None = None
        self._active_request_id: str | None = None
        self._cancel_sent = False

    @property
    def is_running(self) -> bool:
        with self._lifecycle_lock:
            return self._is_running_unlocked()

    @property
    def process_id(self) -> int | None:
        with self._lifecycle_lock:
            process = self._process
            return process.pid if process is not None and process.is_alive() else None

    @property
    def shared_memory_name(self) -> str | None:
        with self._lifecycle_lock:
            return self._slot.name if self._slot is not None else None

    @property
    def active_job_id(self) -> str | None:
        with self._state_lock:
            return self._active_job_id

    def start(self) -> None:
        """Start a fresh child; calling this after invalidation is explicit retry."""
        with self._lifecycle_lock:
            if self._is_running_unlocked():
                return
            self._discard_process_unlocked(graceful=False)
            self._assert_spawn_serializable(self._model_spec)
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
                message = self._receive_unlocked(self._startup_timeout)
                if (
                    not isinstance(message, WorkerReady)
                    or message.protocol_version != PROTOCOL_VERSION
                    or message.job_id != CONTROL_JOB_ID
                    or not isinstance(message.process_id, int)
                    or isinstance(message.process_id, bool)
                    or message.process_id != process.pid
                ):
                    raise self._protocol_error_unlocked(
                        "child startup acknowledgement is malformed or mismatched"
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
            frame = self._validate_image(image)
            self._validate_request(request)
            with self._lifecycle_lock:
                if not self._is_running_unlocked():
                    raise self._crash_error(
                        "segmentation process is not running; "
                        "explicit retry is required",
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
            with self._state_lock:
                self._active_job_id = request.job_id
                self._active_request_id = request.request_id
                self._cancel_sent = False
            self._send(connection, wire_request, request.job_id)
            return self._await_result(frame.shape[:2], request)
        finally:
            with self._state_lock:
                self._active_job_id = None
                self._active_request_id = None
                self._cancel_sent = False
            self._operation_lock.release()

    def cancel(self, job_id: str) -> bool:
        """Send at most one cooperative cancellation request for the active job."""
        if not isinstance(job_id, str) or not job_id:
            return False
        with self._state_lock:
            if self._active_job_id != job_id or self._cancel_sent:
                return False
            self._cancel_sent = True
        with self._lifecycle_lock:
            if not self._is_running_unlocked() or self._connection is None:
                with self._state_lock:
                    self._cancel_sent = False
                return False
            connection = self._connection
        try:
            self._send(connection, CancelRequest(PROTOCOL_VERSION, job_id), job_id)
        except AppError:
            with self._state_lock:
                self._cancel_sent = False
            raise
        return True

    def replace_model(self, model_spec: object) -> None:
        """Reclaim the old native session at OS level, then spawn its replacement."""
        if not self._operation_lock.acquire(blocking=False):
            raise self._busy_error(self.active_job_id)
        try:
            self._assert_spawn_serializable(model_spec)
            with self._lifecycle_lock:
                self._discard_process_unlocked(graceful=True)
                self._model_spec = model_spec
            self.start()
        finally:
            self._operation_lock.release()

    def close(self) -> None:
        """Stop the child and unlink all parent-owned shared memory, idempotently."""
        with self._lifecycle_lock:
            self._discard_process_unlocked(graceful=True)

    def _await_result(
        self, source_size: tuple[int, int], request: SegmentRequest
    ) -> Uint8Frame:
        try:
            message = self._receive_unlocked(self._response_timeout)
        except AppError:
            with self._lifecycle_lock:
                self._discard_process_unlocked(graceful=False)
            raise
        if isinstance(message, CancelAck):
            self._raise_cancelled(message, request)
        if isinstance(message, SegmentFailure):
            self._validate_response_identity(
                message.protocol_version,
                message.job_id,
                message.request_id,
                request,
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
        self._validate_response_identity(
            message.protocol_version,
            message.job_id,
            message.request_id,
            request,
        )
        if self._cancel_is_pending(request.job_id):
            self._await_cancel_ack(request)
        height, width = source_size
        expected_shape = (height, width, 4)
        expected_bytes = height * width * 4
        with self._lifecycle_lock:
            if (
                not _is_frame_shape(message.shape, channels=(4,))
                or message.shape != expected_shape
                or message.dtype != "uint8"
                or not isinstance(message.byte_length, int)
                or isinstance(message.byte_length, bool)
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
        """Atomically close cancellation admission or require its acknowledgement."""
        with self._state_lock:
            if self._active_job_id != job_id:
                return False
            if self._cancel_sent:
                return True
            self._active_job_id = None
            self._active_request_id = None
            return False

    def _await_cancel_ack(self, request: SegmentRequest) -> None:
        try:
            acknowledgement = self._receive_unlocked(self._response_timeout)
        except AppError:
            with self._lifecycle_lock:
                self._discard_process_unlocked(graceful=False)
            raise
        if not isinstance(acknowledgement, CancelAck):
            raise self._protocol_error(
                "response arrived after cancellation without a matching "
                "acknowledgement",
                request.job_id,
            )
        self._raise_cancelled(acknowledgement, request)

    def _raise_cancelled(
        self, acknowledgement: CancelAck, request: SegmentRequest
    ) -> None:
        if (
            acknowledgement.protocol_version != PROTOCOL_VERSION
            or acknowledgement.job_id != request.job_id
        ):
            raise self._protocol_error(
                "cancellation acknowledgement identity mismatch",
                request.job_id,
            )
        with self._state_lock:
            expected_acknowledgement = (
                self._active_job_id == request.job_id and self._cancel_sent
            )
            if expected_acknowledgement:
                self._active_job_id = None
                self._active_request_id = None
        if not expected_acknowledgement:
            raise self._protocol_error(
                "unsolicited cancellation acknowledgement",
                request.job_id,
            )
        with self._lifecycle_lock:
            connection = self._connection
            if connection is not None and connection.poll():
                raise self._protocol_error_unlocked(
                    "duplicate or late response followed cancellation",
                    request.job_id,
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
        self,
        protocol_version: object,
        job_id: object,
        request_id: object,
        expected: SegmentRequest,
    ) -> None:
        if (
            not isinstance(protocol_version, int)
            or isinstance(protocol_version, bool)
            or protocol_version != PROTOCOL_VERSION
            or not isinstance(job_id, str)
            or job_id != expected.job_id
            or not isinstance(request_id, str)
            or request_id != expected.request_id
        ):
            raise self._protocol_error(
                "segmentation response identity mismatch", expected.job_id
            )

    def _validate_request(self, request: object) -> None:
        if (
            not isinstance(request, SegmentRequest)
            or request.protocol_version != PROTOCOL_VERSION
            or not isinstance(request.job_id, str)
            or not request.job_id
            or not isinstance(request.request_id, str)
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
        expected = frame.shape[0] * frame.shape[1] * frame.shape[2]
        if frame.nbytes != expected:
            raise _protocol_app_error("input frame byte length is inconsistent")
        return frame

    def _ensure_slot_unlocked(self, capacity: int, job_id: str) -> SharedMemory:
        if self._slot is not None and self._slot_capacity >= capacity:
            return self._slot
        self._discard_slot_unlocked()
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

    def _receive_unlocked(self, timeout: float) -> object:
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
                    return connection.recv()
            except (EOFError, BrokenPipeError, OSError) as error:
                raise self._crash_error(
                    "segmentation process connection closed unexpectedly", cause=error
                ) from error
            try:
                alive = process.is_alive()
            except ValueError:
                alive = False
            if not alive:
                raise self._crash_error(
                    f"segmentation process exited with code {process.exitcode}"
                )

    def _send(self, connection: Connection, message: object, job_id: str) -> None:
        try:
            with self._send_lock:
                connection.send(message)
        except (BrokenPipeError, EOFError, OSError) as error:
            with self._lifecycle_lock:
                self._discard_process_unlocked(graceful=False)
            raise self._crash_error(
                "segmentation process connection closed while sending",
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

    @staticmethod
    def _assert_spawn_serializable(value: object) -> None:
        try:
            pickle.dumps(value)
        except (pickle.PickleError, TypeError, AttributeError) as error:
            raise _protocol_app_error(
                f"model specification is not spawn serializable: {type(error).__name__}"
            ) from error

    def _is_running_unlocked(self) -> bool:
        return (
            self._process is not None
            and self._connection is not None
            and self._process.is_alive()
        )

    def _discard_process_unlocked(self, *, graceful: bool) -> None:
        connection = self._connection
        process = self._process
        self._connection = None
        self._process = None
        if (
            connection is not None
            and graceful
            and process is not None
            and process.is_alive()
        ):
            try:
                connection.send(Shutdown(PROTOCOL_VERSION))
            except (BrokenPipeError, EOFError, OSError):
                pass
        if connection is not None:
            connection.close()
        if process is not None:
            if graceful and process.is_alive():
                process.join(_JOIN_TIMEOUT_SECONDS)
            if process.is_alive():
                process.terminate()
                process.join(_JOIN_TIMEOUT_SECONDS)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(_JOIN_TIMEOUT_SECONDS)
            try:
                process.close()
            except ValueError:
                pass
        self._discard_slot_unlocked()
        with self._state_lock:
            self._active_job_id = None
            self._active_request_id = None
            self._cancel_sent = False

    def _discard_slot_unlocked(self) -> None:
        slot = self._slot
        self._slot = None
        self._slot_capacity = 0
        if slot is None:
            return
        try:
            slot.close()
        finally:
            try:
                slot.unlink()
            except FileNotFoundError:
                pass


def segmentation_process_main(connection: Connection, model_spec: object) -> None:
    """Child entry point: construct one rembg session and serve serial requests."""
    session: object | None = None
    try:
        session = _create_rembg_session(model_spec)
        process_id = multiprocessing.current_process().pid
        assert process_id is not None
        connection.send(WorkerReady(PROTOCOL_VERSION, CONTROL_JOB_ID, process_id))
        cancelled: set[str] = set()
        while True:
            message = connection.recv()
            if isinstance(message, Shutdown):
                return
            if isinstance(message, CancelRequest):
                if _valid_cancel(message) and message.job_id not in cancelled:
                    cancelled.add(message.job_id)
                    connection.send(CancelAck(PROTOCOL_VERSION, message.job_id))
                continue
            if not isinstance(message, SegmentRequest):
                return
            protocol_detail = _validate_wire_request(message)
            if protocol_detail is not None:
                connection.send(_failure(message, protocol_detail))
                return
            assert message.slot is not None
            slot = SharedMemory(name=message.slot.name, create=False)
            try:
                required_capacity = max(
                    message.slot.byte_length,
                    message.slot.shape[0] * message.slot.shape[1] * 4,
                )
                if slot.size < required_capacity:
                    connection.send(
                        _failure(message, "shared-memory slot capacity is invalid")
                    )
                    return
                source = np.ndarray(
                    message.slot.shape,
                    dtype=np.uint8,
                    buffer=slot.buf,
                ).copy()
                result = _run_rembg(source, session)
                expected_shape = (source.shape[0], source.shape[1], 4)
                if (
                    result.dtype != np.dtype(np.uint8)
                    or result.shape != expected_shape
                    or not result.flags.c_contiguous
                    or result.nbytes != source.shape[0] * source.shape[1] * 4
                    or result.nbytes > slot.size
                ):
                    connection.send(
                        _failure(message, "rembg returned an invalid RGBA frame")
                    )
                    return
                pending_shutdown = False
                while connection.poll():
                    pending = connection.recv()
                    if isinstance(pending, Shutdown):
                        pending_shutdown = True
                    elif _valid_cancel(pending) and pending.job_id == message.job_id:
                        cancelled.add(pending.job_id)
                if pending_shutdown:
                    return
                if message.job_id in cancelled:
                    connection.send(CancelAck(PROTOCOL_VERSION, message.job_id))
                    continue
                slot_buffer = slot.buf
                assert slot_buffer is not None
                slot_buffer[: result.nbytes] = result.tobytes(order="C")
                connection.send(
                    SegmentResponse(
                        PROTOCOL_VERSION,
                        message.job_id,
                        message.request_id,
                        expected_shape,
                        "uint8",
                        result.nbytes,
                    )
                )
            except BaseException as error:
                app_error = AppError(
                    ErrorCode.INVALID_SEGMENTATION,
                    "segmentation",
                    "error.segmentation.inference-failed",
                    f"{type(error).__name__}: {error}",
                    "retry-job",
                    message.job_id,
                )
                connection.send(
                    SegmentFailure(
                        PROTOCOL_VERSION,
                        message.job_id,
                        message.request_id,
                        app_error.to_primitives(),
                    )
                )
            finally:
                slot.close()
    except (EOFError, BrokenPipeError, OSError):
        return
    finally:
        session = None
        connection.close()


def _create_rembg_session(model_spec: object) -> object:
    from rembg import new_session  # type: ignore[import-untyped]

    model_id: object = None
    if isinstance(model_spec, Mapping):
        model_id = model_spec.get("upstream_id", model_spec.get("model_id"))
    else:
        model_id = getattr(
            model_spec, "upstream_id", getattr(model_spec, "model_id", None)
        )
    if not isinstance(model_id, str) or not model_id:
        raise ValueError("local model specification has no upstream model identifier")
    return new_session(model_id)


def _run_rembg(source: Uint8Frame, session: object) -> Uint8Frame:
    from rembg import remove

    value = remove(source, session=session)
    result = np.asarray(value)
    if result.dtype != np.dtype(np.uint8):
        raise ValueError("rembg output dtype is not uint8")
    return np.ascontiguousarray(result)


def _validate_wire_request(request: SegmentRequest) -> str | None:
    if (
        request.protocol_version != PROTOCOL_VERSION
        or not isinstance(request.job_id, str)
        or not request.job_id
        or not isinstance(request.request_id, str)
        or not request.request_id
        or not isinstance(request.slot, SharedFrame)
    ):
        return "request identity or shared-frame descriptor is malformed"
    slot = request.slot
    if (
        not isinstance(slot.name, str)
        or not slot.name
        or not _is_frame_shape(slot.shape, channels=(3, 4))
        or slot.dtype != "uint8"
        or not isinstance(slot.byte_length, int)
        or isinstance(slot.byte_length, bool)
        or slot.byte_length != slot.shape[0] * slot.shape[1] * slot.shape[2]
    ):
        return "shared-frame shape, dtype, or byte length is invalid"
    return None


def _is_frame_shape(shape: object, *, channels: tuple[int, ...]) -> bool:
    return (
        isinstance(shape, tuple)
        and len(shape) == 3
        and all(
            isinstance(value, int) and not isinstance(value, bool) for value in shape
        )
        and 1 <= shape[0] <= _MAX_FRAME_DIMENSION
        and 1 <= shape[1] <= _MAX_FRAME_DIMENSION
        and shape[2] in channels
    )


def _valid_cancel(message: object) -> bool:
    return (
        isinstance(message, CancelRequest)
        and message.protocol_version == PROTOCOL_VERSION
        and isinstance(message.job_id, str)
        and bool(message.job_id)
    )


def _failure(request: SegmentRequest, detail: str) -> SegmentFailure:
    error = _protocol_app_error(detail, request.job_id)
    return SegmentFailure(
        PROTOCOL_VERSION,
        request.job_id,
        request.request_id,
        error.to_primitives(),
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


def freeze_support_entry() -> None:
    """Frozen application entry hook used by native packaging configurations."""
    multiprocessing.freeze_support()


if __name__ == "__main__":  # pragma: no cover - exercised by frozen artifacts
    freeze_support_entry()
