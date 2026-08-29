from __future__ import annotations

import threading
import time
from multiprocessing import Pipe, active_children
from multiprocessing.shared_memory import SharedMemory

import numpy as np
import pytest

import rembggui.app as app_module
import rembggui.jobs.segmentation_host as segmentation_host_module
from rembggui.core.errors import AppError, ErrorCode
from rembggui.jobs.protocol import (
    CONTROL_JOB_ID,
    MAX_PROTOCOL_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    CancelAck,
    CancelRequest,
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
from rembggui.jobs.segmentation_host import (
    SegmentationClient,
    _serve_segmentation_connection,
)
from tests.jobs.fake_segmentation_child import (
    fake_segmentation_child,
    unpicklable_model_spec,
)


def red_frame(height: int = 4, width: int = 5) -> np.ndarray:
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[..., 0] = 255
    return frame


def request(job_id: str = "j1", request_id: str = "r1") -> SegmentRequest:
    return SegmentRequest(PROTOCOL_VERSION, job_id, request_id)


def client(mode: str = "success", *, timeout: float = 2.0) -> SegmentationClient:
    return SegmentationClient(
        {"mode": mode},
        child_target=fake_segmentation_child,
        response_timeout=timeout,
        startup_timeout=timeout,
    )


def _capture_segment_error(
    segmentation: SegmentationClient, caught: list[AppError]
) -> None:
    try:
        segmentation.segment(red_frame(), request())
    except AppError as error:
        caught.append(error)


def test_protocol_dataclasses_are_frozen_and_round_trip_through_byte_codec() -> None:
    messages = (
        request(),
        SegmentRequest(
            PROTOCOL_VERSION,
            "j1",
            "r1",
            SharedFrame("slot", (2, 3, 3), "uint8", 18),
        ),
        SegmentResponse(PROTOCOL_VERSION, "j1", "r1", (2, 3, 4), "uint8", 24),
        CancelRequest(PROTOCOL_VERSION, "j1"),
        CancelAck(PROTOCOL_VERSION, "j1"),
        Shutdown(PROTOCOL_VERSION, "__control__"),
        WorkerReady(PROTOCOL_VERSION, "__control__", 123),
        SegmentFailure(
            PROTOCOL_VERSION,
            "j1",
            "r1",
            AppError(
                ErrorCode.INVALID_SEGMENTATION,
                "segmentation",
                "error.segmentation.inference-failed",
                "fake failure",
                "retry-job",
                "j1",
            ).to_primitives(),
        ),
    )
    parent_messages = (messages[1], messages[3], messages[5])
    child_messages = (messages[2], messages[4], messages[6], messages[7])
    assert (
        tuple(
            decode_parent_message(encode_parent_message(item))
            for item in parent_messages
        )
        == parent_messages
    )
    assert (
        tuple(
            decode_child_message(encode_child_message(item)) for item in child_messages
        )
        == child_messages
    )
    with pytest.raises((AttributeError, TypeError)):
        messages[0].job_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        b"not-json",
        b'{"type":"cancel_request","protocol_version":true,"job_id":"j1"}',
        b'{"type":"cancel_request","protocol_version":1,"job_id":"j1","extra":0}',
        b'{"type":"cancel_request","protocol_version":1}',
        b'{"type":"unknown","protocol_version":1,"job_id":"j1"}',
        b'{"type":"shutdown","protocol_version":999,"job_id":"__control__"}',
        b'{"type":"cancel_request","protocol_version":1,"job_id":"j1","job_id":"j2"}',
        b"{" + b" " * MAX_PROTOCOL_MESSAGE_BYTES + b"}",
    ],
)
def test_byte_codec_rejects_malformed_noncanonical_or_oversized_messages(
    payload: bytes,
) -> None:
    with pytest.raises(ProtocolCodecError):
        decode_parent_message(payload)


def test_model_launch_payload_rejects_mapping_subclasses_and_executable_values() -> (
    None
):
    class DictSubclass(dict[str, object]):
        pass

    for unsafe in (DictSubclass(mode="success"), {"factory": lambda: None}):
        with pytest.raises(AppError) as exc:
            SegmentationClient(unsafe, child_target=fake_segmentation_child)
        assert exc.value.code is ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH


@pytest.mark.parametrize(
    "payload",
    [
        b'{"type":"worker_ready","protocol_version":true,"job_id":"__control__","process_id":1}',
        b'{"type":"worker_ready","protocol_version":1,"job_id":"wrong","process_id":1}',
        b'{"type":"worker_ready","protocol_version":1,"job_id":"__control__","process_id":false}',
        b'{"type":"cancel_ack","protocol_version":1,"job_id":"j1","extra":0}',
    ],
)
def test_child_codec_rejects_strict_schema_violations(payload: bytes) -> None:
    with pytest.raises(ProtocolCodecError):
        decode_child_message(payload)


def test_application_entry_calls_freeze_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[None] = []
    monkeypatch.setattr(
        app_module.multiprocessing, "freeze_support", lambda: calls.append(None)
    )
    assert app_module.main(["--smoke-test"]) == 0
    assert calls == [None]


def test_segment_success_reuses_parent_owned_slot() -> None:
    segmentation = client()
    try:
        segmentation.start()
        first = segmentation.segment(red_frame(), request())
        slot_name = segmentation.shared_memory_name
        second = segmentation.segment(red_frame(2, 3), request("j2", "r2"))
        assert first.shape == (4, 5, 4)
        assert np.all(first[..., 0] == 255)
        assert np.all(first[..., 3] == 255)
        assert second.shape == (2, 3, 4)
        assert segmentation.shared_memory_name == slot_name
    finally:
        segmentation.close()


def test_child_crash_invalidates_slot_and_restarts_only_on_explicit_start() -> None:
    segmentation = client("crash-delayed")
    segmentation.start()
    caught: list[AppError] = []

    def crash_request() -> None:
        try:
            segmentation.segment(red_frame(), request())
        except AppError as error:
            caught.append(error)

    thread = threading.Thread(target=crash_request)
    thread.start()
    deadline = time.monotonic() + 1
    while segmentation.shared_memory_name is None and time.monotonic() < deadline:
        time.sleep(0.005)
    slot_name = segmentation.shared_memory_name
    assert slot_name is not None
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert caught and caught[0].code is ErrorCode.SEGMENTATION_PROCESS_CRASHED
    assert not segmentation.is_running
    assert segmentation.shared_memory_name is None
    with pytest.raises(FileNotFoundError):
        orphan = SharedMemory(name=slot_name, create=False)
        orphan.close()

    with pytest.raises(AppError) as again:
        segmentation.segment(red_frame(), request("j2", "r2"))
    assert again.value.code is ErrorCode.SEGMENTATION_PROCESS_CRASHED
    segmentation.start()
    assert segmentation.is_running
    segmentation.close()


@pytest.mark.parametrize(
    "mode",
    [
        "response-version",
        "wrong-job",
        "wrong-request",
        "wrong-shape",
        "wrong-dtype",
        "wrong-byte-length",
        "late-response",
        "malformed-error",
        "unsolicited-ack",
        "invalid-utf8",
        "oversized",
    ],
)
def test_untrusted_response_invalidates_process_and_slot(mode: str) -> None:
    segmentation = client(mode)
    segmentation.start()
    with pytest.raises(AppError) as exc:
        segmentation.segment(red_frame(), request())
    assert exc.value.code is ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH
    assert not segmentation.is_running
    assert segmentation.shared_memory_name is None


def test_startup_version_mismatch_is_not_running() -> None:
    segmentation = client("startup-version")
    with pytest.raises(AppError) as exc:
        segmentation.start()
    assert exc.value.code is ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH
    assert not segmentation.is_running


def test_start_boundary_cleans_dead_child_and_old_slot_before_new_spawn() -> None:
    segmentation = client("exit-delayed-after-response")
    segmentation.start()
    segmentation.segment(red_frame(), request())
    old_slot = segmentation.shared_memory_name
    assert old_slot is not None
    deadline = time.monotonic() + 1
    while segmentation.process_id is not None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert segmentation.process_id is None

    segmentation.start()
    assert segmentation.is_running
    assert segmentation.shared_memory_name is None
    with pytest.raises(FileNotFoundError):
        orphan = SharedMemory(name=old_slot, create=False)
        orphan.close()
    segmentation.close()


def test_cancel_is_sent_once_and_acknowledged_after_inference() -> None:
    segmentation = client("delayed-cancel")
    segmentation.start()
    caught: list[AppError] = []

    def run_segment() -> None:
        try:
            segmentation.segment(red_frame(), request())
        except AppError as error:
            caught.append(error)

    thread = threading.Thread(target=run_segment)
    thread.start()
    deadline = time.monotonic() + 1
    while segmentation.active_job_id is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert segmentation.cancel("wrong") is False
    assert segmentation.cancel("j1") is True
    assert segmentation.cancel("j1") is False
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert caught and caught[0].code is ErrorCode.JOB_CANCELLED
    assert segmentation.is_running
    segmentation.close()


def test_cancel_cannot_overtake_request_wire_send() -> None:
    request_published = threading.Event()
    cancel_admitted = threading.Event()
    release_request_send = threading.Event()

    class BarrierClient(SegmentationClient):
        def _before_request_wire_send(self) -> None:
            request_published.set()
            assert release_request_send.wait(timeout=1)

        def _after_cancel_admitted(self) -> None:
            cancel_admitted.set()

    segmentation = BarrierClient(
        {"mode": "delayed-cancel"},
        child_target=fake_segmentation_child,
        response_timeout=2,
        startup_timeout=2,
    )
    segmentation.start()
    caught: list[AppError] = []
    cancel_results: list[bool] = []

    segment_thread = threading.Thread(
        target=lambda: _capture_segment_error(segmentation, caught)
    )
    segment_thread.start()
    assert request_published.wait(timeout=1)
    cancel_thread = threading.Thread(
        target=lambda: cancel_results.append(segmentation.cancel("j1"))
    )
    cancel_thread.start()
    assert cancel_admitted.wait(timeout=1)
    release_request_send.set()
    cancel_thread.join(timeout=1)
    segment_thread.join(timeout=2)
    assert not cancel_thread.is_alive()
    assert not segment_thread.is_alive()
    assert cancel_results == [True]
    assert caught and caught[0].code is ErrorCode.JOB_CANCELLED
    assert segmentation.is_running
    segmentation.close()


def test_model_replacement_terminates_old_process_and_unlinks_slot() -> None:
    segmentation = client()
    segmentation.start()
    segmentation.segment(red_frame(), request())
    old_pid = segmentation.process_id
    old_slot = segmentation.shared_memory_name

    segmentation.replace_model({"mode": "success", "model": "other"})
    assert segmentation.is_running
    assert segmentation.process_id != old_pid
    assert old_pid not in {child.pid for child in active_children()}
    assert segmentation.shared_memory_name is None
    assert old_slot is not None
    with pytest.raises(FileNotFoundError):
        orphan = SharedMemory(name=old_slot, create=False)
        orphan.close()
    segmentation.close()


def test_growing_slot_unlinks_the_replaced_shared_memory() -> None:
    segmentation = client()
    segmentation.start()
    segmentation.segment(red_frame(2, 2), request())
    old_slot = segmentation.shared_memory_name
    segmentation.segment(red_frame(8, 8), request("j2", "r2"))
    assert old_slot is not None
    assert segmentation.shared_memory_name != old_slot
    with pytest.raises(FileNotFoundError):
        orphan = SharedMemory(name=old_slot, create=False)
        orphan.close()
    segmentation.close()


def test_second_request_and_model_replacement_are_rejected_while_active() -> None:
    segmentation = client("delayed-cancel")
    segmentation.start()
    errors: list[AppError] = []

    def run_segment() -> None:
        try:
            segmentation.segment(red_frame(), request())
        except AppError as error:
            errors.append(error)

    thread = threading.Thread(target=run_segment)
    thread.start()
    deadline = time.monotonic() + 1
    while segmentation.active_job_id is None and time.monotonic() < deadline:
        time.sleep(0.005)
    with pytest.raises(AppError) as second:
        segmentation.segment(red_frame(), request("j2", "r2"))
    assert second.value.code is ErrorCode.JOB_ALREADY_RUNNING
    with pytest.raises(AppError) as replacement:
        segmentation.replace_model({"mode": "success"})
    assert replacement.value.code is ErrorCode.JOB_ALREADY_RUNNING
    assert segmentation.cancel("j1")
    thread.join(timeout=2)
    assert errors and errors[0].code is ErrorCode.JOB_CANCELLED
    segmentation.close()


def test_close_is_idempotent_and_unlinks_parent_slot() -> None:
    segmentation = client()
    segmentation.start()
    segmentation.segment(red_frame(), request())
    slot_name = segmentation.shared_memory_name
    assert slot_name is not None
    segmentation.close()
    segmentation.close()
    assert not segmentation.is_running
    with pytest.raises(FileNotFoundError):
        orphan = SharedMemory(name=slot_name, create=False)
        orphan.close()


def test_close_during_inference_terminates_child_and_unlinks_slot() -> None:
    segmentation = client("delayed-cancel")
    segmentation.start()
    caught: list[AppError] = []

    def run_segment() -> None:
        try:
            segmentation.segment(red_frame(), request())
        except AppError as error:
            caught.append(error)

    thread = threading.Thread(target=run_segment)
    thread.start()
    deadline = time.monotonic() + 1
    while segmentation.active_job_id is None and time.monotonic() < deadline:
        time.sleep(0.005)
    slot_name = segmentation.shared_memory_name
    assert slot_name is not None
    segmentation.close()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert caught and caught[0].code is ErrorCode.SEGMENTATION_PROCESS_CRASHED
    with pytest.raises(FileNotFoundError):
        orphan = SharedMemory(name=slot_name, create=False)
        orphan.close()


@pytest.mark.parametrize(
    "bad_image",
    [
        np.zeros((2, 2), dtype=np.uint8),
        np.zeros((2, 2, 3), dtype=np.float32),
        np.zeros((2, 2, 5), dtype=np.uint8),
        np.zeros((0, 2, 3), dtype=np.uint8),
    ],
)
def test_invalid_image_is_rejected_before_shared_memory_allocation(
    bad_image: np.ndarray,
) -> None:
    segmentation = client()
    segmentation.start()
    with pytest.raises(AppError) as exc:
        segmentation.segment(bad_image, request())
    assert exc.value.code is ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH
    assert segmentation.shared_memory_name is None
    segmentation.close()


def test_unpicklable_model_spec_is_rejected_before_process_start() -> None:
    with pytest.raises(AppError) as exc:
        SegmentationClient(
            unpicklable_model_spec(), child_target=fake_segmentation_child
        )
    assert exc.value.code is ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH


def test_malformed_request_invalidates_the_process_before_transport() -> None:
    segmentation = client()
    segmentation.start()
    with pytest.raises(AppError) as exc:
        segmentation.segment(red_frame(), SegmentRequest(999, "j1", "r1"))
    assert exc.value.code is ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH
    assert not segmentation.is_running


class _SlotDouble:
    name = "test-slot"
    size = 64

    def __init__(self, *, unlink_failures: int = 0) -> None:
        self.closed = False
        self.unlinked = False
        self.unlink_failures = unlink_failures

    def close(self) -> None:
        self.closed = True

    def unlink(self) -> None:
        if self.unlink_failures:
            self.unlink_failures -= 1
            raise OSError("unlink failed")
        self.unlinked = True


class _ConnectionDouble:
    def __init__(self, *, close_error: bool = False) -> None:
        self.closed = False
        self.close_error = close_error

    def poll(self, _timeout: float = 0.0) -> bool:
        return False

    def send_bytes(self, _payload: bytes) -> None:
        return

    def close(self) -> None:
        self.closed = True
        if self.close_error:
            raise OSError("close failed")


class _ProcessDouble:
    pid = 4242
    exitcode = 0

    def __init__(
        self, *, alive: bool, stubborn: bool = False, join_error: bool = False
    ) -> None:
        self.alive = alive
        self.stubborn = stubborn
        self.join_error = join_error
        self.actions: list[str] = []

    def is_alive(self) -> bool:
        return self.alive

    def join(self, _timeout: float) -> None:
        self.actions.append("join")
        if self.join_error:
            self.join_error = False
            raise RuntimeError("join failed")

    def terminate(self) -> None:
        self.actions.append("terminate")
        if not self.stubborn:
            self.alive = False

    def kill(self) -> None:
        self.actions.append("kill")
        if not self.stubborn:
            self.alive = False

    def close(self) -> None:
        self.actions.append("close")


def _install_process_doubles(
    segmentation: SegmentationClient,
    process: _ProcessDouble,
    connection: _ConnectionDouble,
    slot: _SlotDouble,
) -> None:
    segmentation._process = process  # type: ignore[assignment]
    segmentation._connection = connection  # type: ignore[assignment]
    segmentation._slot = slot  # type: ignore[assignment]
    segmentation._slot_capacity = slot.size


def test_dead_process_before_request_invalidates_connection_and_slot() -> None:
    segmentation = client()
    process = _ProcessDouble(alive=False)
    connection = _ConnectionDouble()
    slot = _SlotDouble()
    _install_process_doubles(segmentation, process, connection, slot)

    with pytest.raises(AppError) as exc:
        segmentation.segment(red_frame(), request())
    assert exc.value.code is ErrorCode.SEGMENTATION_PROCESS_CRASHED
    assert connection.closed
    assert slot.closed and slot.unlinked
    assert segmentation.shared_memory_name is None


def test_stubborn_process_blocks_replacement_but_always_unlinks_slot() -> None:
    segmentation = client()
    process = _ProcessDouble(alive=True, stubborn=True)
    connection = _ConnectionDouble()
    slot = _SlotDouble()
    _install_process_doubles(segmentation, process, connection, slot)

    with pytest.raises(AppError) as exc:
        segmentation.replace_model({"mode": "success"})
    assert exc.value.code is ErrorCode.SEGMENTATION_CLEANUP_FAILED
    assert process.actions == ["join", "terminate", "join", "kill", "join"]
    assert slot.closed and slot.unlinked
    assert segmentation._process is process

    process.alive = False
    segmentation.close()
    assert segmentation._process is None


def test_teardown_exception_surfaces_after_slot_cleanup_and_no_old_process() -> None:
    segmentation = client()
    process = _ProcessDouble(alive=True, join_error=True)
    connection = _ConnectionDouble()
    slot = _SlotDouble()
    _install_process_doubles(segmentation, process, connection, slot)

    with pytest.raises(AppError) as exc:
        segmentation.close()
    assert exc.value.code is ErrorCode.SEGMENTATION_CLEANUP_FAILED
    assert not process.alive
    assert segmentation._process is None
    assert slot.closed and slot.unlinked
    segmentation.close()


def test_unlink_failure_preserves_parent_handle_for_explicit_cleanup_retry() -> None:
    segmentation = client()
    process = _ProcessDouble(alive=False)
    connection = _ConnectionDouble()
    slot = _SlotDouble(unlink_failures=1)
    _install_process_doubles(segmentation, process, connection, slot)

    with pytest.raises(AppError) as exc:
        segmentation.close()
    assert exc.value.code is ErrorCode.SEGMENTATION_CLEANUP_FAILED
    assert segmentation._slot is slot
    assert not slot.unlinked

    segmentation.close()
    assert slot.unlinked
    assert segmentation._slot is None


def _start_production_loop(
    inference: object,
) -> tuple[object, threading.Thread]:
    parent, child = Pipe(duplex=True)
    thread = threading.Thread(
        target=_serve_segmentation_connection,
        args=(child, object(), inference),
        kwargs={"process_id": 123},
    )
    thread.start()
    ready = decode_child_message(parent.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES))
    assert ready == WorkerReady(PROTOCOL_VERSION, CONTROL_JOB_ID, 123)
    return parent, thread


def test_production_serve_loop_success_uses_exact_codec_and_shared_slot() -> None:
    def inference(frame: np.ndarray, _session: object) -> np.ndarray:
        alpha = np.full(frame.shape[:2] + (1,), 255, dtype=np.uint8)
        return np.ascontiguousarray(np.concatenate((frame[..., :3], alpha), axis=2))

    parent, thread = _start_production_loop(inference)
    slot = SharedMemory(create=True, size=16)
    try:
        frame = red_frame(2, 2)
        slot.buf[: frame.nbytes] = frame.tobytes()
        wire = SegmentRequest(
            PROTOCOL_VERSION,
            "j1",
            "r1",
            SharedFrame(slot.name, frame.shape, "uint8", frame.nbytes),
        )
        parent.send_bytes(encode_parent_message(wire))
        response = decode_child_message(parent.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES))
        assert response == SegmentResponse(
            PROTOCOL_VERSION, "j1", "r1", (2, 2, 4), "uint8", 16
        )
        output = np.ndarray((2, 2, 4), dtype=np.uint8, buffer=slot.buf).copy()
        assert np.all(output[..., 0] == 255)
        assert np.all(output[..., 3] == 255)
        parent.send_bytes(encode_parent_message(Shutdown(PROTOCOL_VERSION)))
        thread.join(timeout=1)
        assert not thread.is_alive()
    finally:
        parent.close()
        slot.close()
        slot.unlink()


def test_production_loop_acknowledges_cancel_only_after_inference_barrier() -> None:
    entered = threading.Event()
    release = threading.Event()

    def inference(frame: np.ndarray, _session: object) -> np.ndarray:
        entered.set()
        assert release.wait(timeout=1)
        alpha = np.full(frame.shape[:2] + (1,), 255, dtype=np.uint8)
        return np.ascontiguousarray(np.concatenate((frame[..., :3], alpha), axis=2))

    parent, thread = _start_production_loop(inference)
    slot = SharedMemory(create=True, size=16)
    try:
        frame = red_frame(2, 2)
        slot.buf[: frame.nbytes] = frame.tobytes()
        parent.send_bytes(
            encode_parent_message(
                SegmentRequest(
                    PROTOCOL_VERSION,
                    "j1",
                    "r1",
                    SharedFrame(slot.name, frame.shape, "uint8", frame.nbytes),
                )
            )
        )
        assert entered.wait(timeout=1)
        parent.send_bytes(encode_parent_message(CancelRequest(PROTOCOL_VERSION, "j1")))
        assert not parent.poll()
        release.set()
        assert decode_child_message(
            parent.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES)
        ) == CancelAck(PROTOCOL_VERSION, "j1")
        parent.send_bytes(encode_parent_message(Shutdown(PROTOCOL_VERSION)))
        thread.join(timeout=1)
        assert not thread.is_alive()
    finally:
        parent.close()
        slot.close()
        slot.unlink()


def test_production_loop_rejects_insufficient_actual_slot_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inference_called = False

    def inference(_frame: np.ndarray, _session: object) -> np.ndarray:
        nonlocal inference_called
        inference_called = True
        raise AssertionError("must not run")

    class UndersizedSlot:
        name = "undersized"
        size = 12

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.buf = memoryview(bytearray(16))

        def close(self) -> None:
            return

    monkeypatch.setattr(segmentation_host_module, "SharedMemory", UndersizedSlot)
    parent, thread = _start_production_loop(inference)
    try:
        frame = red_frame(2, 2)
        parent.send_bytes(
            encode_parent_message(
                SegmentRequest(
                    PROTOCOL_VERSION,
                    "j1",
                    "r1",
                    SharedFrame("undersized", frame.shape, "uint8", frame.nbytes),
                )
            )
        )
        failure = decode_child_message(parent.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES))
        assert isinstance(failure, SegmentFailure)
        assert failure.error["code"] == ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH
        assert not inference_called
        thread.join(timeout=1)
        assert not thread.is_alive()
    finally:
        parent.close()


@pytest.mark.parametrize(
    ("wire_mutation", "expects_failure"),
    [
        (
            lambda raw: raw.replace(b'"byte_length":12', b'"byte_length":20').replace(
                b'"shape":[2,2,3]', b'"shape":[2,2,5]'
            ),
            True,
        ),
        (lambda raw: raw.replace(b'"dtype":"uint8"', b'"dtype":"float32"'), True),
        (lambda raw: raw.replace(b'"byte_length":12', b'"byte_length":11'), False),
    ],
)
def test_production_loop_rejects_descriptor_shape_dtype_and_byte_length(
    wire_mutation: object,
    expects_failure: bool,
) -> None:
    parent, thread = _start_production_loop(
        lambda frame, _session: np.zeros(frame.shape[:2] + (4,), dtype=np.uint8)
    )
    wire = encode_parent_message(
        SegmentRequest(
            PROTOCOL_VERSION,
            "j1",
            "r1",
            SharedFrame("not-opened", (2, 2, 3), "uint8", 12),
        )
    )
    mutated = wire_mutation(wire)  # type: ignore[operator]
    assert mutated != wire
    parent.send_bytes(mutated)
    if expects_failure:
        failure = decode_child_message(parent.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES))
        assert isinstance(failure, SegmentFailure)
        assert failure.error["code"] == ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH
    thread.join(timeout=1)
    assert not thread.is_alive()
    parent.close()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"type":"shutdown","protocol_version":2,"job_id":"__control__"}',
        b'{"type":"shutdown","protocol_version":1,"job_id":"wrong"}',
        b"\xff",
        b"not-json",
    ],
)
def test_production_loop_closes_on_invalid_control_bytes(raw: bytes) -> None:
    parent, thread = _start_production_loop(
        lambda frame, _session: np.zeros(frame.shape[:2] + (4,), dtype=np.uint8)
    )
    parent.send_bytes(raw)
    thread.join(timeout=1)
    assert not thread.is_alive()
    parent.close()
