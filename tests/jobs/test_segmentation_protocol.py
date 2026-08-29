from __future__ import annotations

import multiprocessing
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


def _capture_custom_segment_error(
    segmentation: SegmentationClient,
    image: np.ndarray,
    caught: list[AppError],
) -> None:
    try:
        segmentation.segment(image, request())
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


def test_launch_normalization_rejects_cycles_depth_oversize_and_expanded_dag() -> None:
    cycle: dict[str, object] = {}
    cycle["self"] = cycle
    deep: object = "leaf"
    for _ in range(8):
        deep = [deep]
    shared = ["x"] * 256
    expanded_dag = [shared] * 256

    for unsafe in (
        cycle,
        {"deep": deep},
        {"huge": "x" * 20_000},
        {"dag": expanded_dag},
    ):
        with pytest.raises(AppError) as exc:
            SegmentationClient(unsafe, child_target=fake_segmentation_child)
        assert exc.value.code is ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH


def test_launch_size_budgets_reject_before_final_json_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = ["12345678"] * 256

    def must_not_encode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("unsafe payload reached final JSON encoding")

    monkeypatch.setattr(segmentation_host_module.json, "dumps", must_not_encode)
    for unsafe in ({"huge": "x" * 20_000}, {"dag": [shared] * 256}):
        with pytest.raises(AppError) as exc:
            SegmentationClient(unsafe, child_target=fake_segmentation_child)
        assert exc.value.code is ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH


def test_launch_normalization_never_executes_custom_reducer() -> None:
    reducer_called = False

    class Reducer:
        def __reduce__(self) -> object:
            nonlocal reducer_called
            reducer_called = True
            return str, ("unsafe",)

    with pytest.raises(AppError):
        SegmentationClient({"custom": Reducer()}, child_target=fake_segmentation_child)
    assert not reducer_called


@pytest.mark.parametrize(
    "unsafe",
    [
        {"value": "\ud800"},
        {"\udfff": "value"},
    ],
)
def test_launch_normalization_rejects_isolated_surrogates(
    unsafe: dict[str, object],
) -> None:
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
    assert caught[0].job_id == "j1"
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
    assert exc.value.job_id == "j1"
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


def test_concurrent_start_is_rejected_while_segment_waits_for_response() -> None:
    receive_entered = threading.Event()
    release_receive = threading.Event()

    class ReceiveBarrierClient(SegmentationClient):
        def __init__(self) -> None:
            super().__init__({"mode": "success"}, child_target=fake_segmentation_child)
            self.receive_calls = 0

        def _receive_child_unlocked(
            self,
            timeout: float,
            *,
            expected_job_id: str | None = None,
        ) -> object:
            self.receive_calls += 1
            if self.receive_calls == 2:
                receive_entered.set()
                assert release_receive.wait(timeout=1)
            return super()._receive_child_unlocked(
                timeout, expected_job_id=expected_job_id
            )

    segmentation = ReceiveBarrierClient()
    segmentation.start()
    old_pid = segmentation.process_id
    output: list[np.ndarray] = []
    errors: list[AppError] = []

    def run_segment() -> None:
        try:
            output.append(segmentation.segment(red_frame(), request()))
        except AppError as error:
            errors.append(error)

    thread = threading.Thread(target=run_segment)
    thread.start()
    assert receive_entered.wait(timeout=1)
    with pytest.raises(AppError) as exc:
        segmentation.start()
    assert exc.value.code is ErrorCode.JOB_ALREADY_RUNNING
    assert exc.value.job_id == "j1"
    assert segmentation.process_id == old_pid
    release_receive.set()
    thread.join(timeout=1)
    assert not errors
    assert output and output[0].shape == (4, 5, 4)
    assert segmentation.process_id == old_pid
    segmentation.close()


def test_active_timeout_error_preserves_job_identity() -> None:
    segmentation = SegmentationClient(
        {"mode": "hang"},
        child_target=fake_segmentation_child,
        startup_timeout=2,
        response_timeout=0.05,
    )
    segmentation.start()
    with pytest.raises(AppError) as exc:
        segmentation.segment(red_frame(), request())
    assert exc.value.code is ErrorCode.SEGMENTATION_PROCESS_CRASHED
    assert exc.value.job_id == "j1"
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
    request_send_entered = threading.Event()
    cancel_attempted = threading.Event()
    release_request_send = threading.Event()

    class BarrierClient(SegmentationClient):
        def _before_request_wire_send(self) -> None:
            request_send_entered.set()
            assert release_request_send.wait(timeout=1)

        def _before_cancel_wire_wait(self) -> None:
            cancel_attempted.set()

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

    def cancel() -> None:
        cancel_results.append(segmentation.cancel("j1"))

    assert request_send_entered.wait(timeout=1)
    cancel_thread = threading.Thread(target=cancel)
    cancel_thread.start()
    assert cancel_attempted.wait(timeout=1)
    assert cancel_results == []
    release_request_send.set()
    cancel_thread.join(timeout=1)
    segment_thread.join(timeout=2)
    assert not cancel_thread.is_alive()
    assert not segment_thread.is_alive()
    assert cancel_results == [True]
    assert caught and caught[0].code is ErrorCode.JOB_CANCELLED
    assert segmentation.is_running
    segmentation.close()


def test_same_thread_close_during_segment_is_rejected_without_deadlock() -> None:
    class ReentrantCloseClient(SegmentationClient):
        close_error: AppError | None = None

        def _before_request_wire_send(self) -> None:
            try:
                self.close()
            except AppError as error:
                self.close_error = error

    segmentation = ReentrantCloseClient(
        {"mode": "success"}, child_target=fake_segmentation_child
    )
    segmentation.start()
    output = segmentation.segment(red_frame(), request())
    assert output.shape == (4, 5, 4)
    assert segmentation.close_error is not None
    assert segmentation.close_error.code is ErrorCode.JOB_ALREADY_RUNNING
    assert segmentation.close_error.job_id == "j1"
    assert segmentation.is_running
    segmentation.close()


def test_accepted_cancel_completes_before_concurrent_close_cleanup() -> None:
    receive_entered = threading.Event()
    release_receive = threading.Event()
    close_wait_boundary = threading.Event()
    close_finished = threading.Event()

    class CloseBarrierClient(SegmentationClient):
        def __init__(self) -> None:
            super().__init__(
                {"mode": "delayed-cancel"},
                child_target=fake_segmentation_child,
                response_timeout=2,
                startup_timeout=2,
            )
            self.receive_calls = 0

        def _receive_child_unlocked(
            self,
            timeout: float,
            *,
            expected_job_id: str | None,
        ) -> object:
            self.receive_calls += 1
            if self.receive_calls == 2:
                receive_entered.set()
                assert release_receive.wait(timeout=1)
            return super()._receive_child_unlocked(
                timeout, expected_job_id=expected_job_id
            )

        def _before_close_operation_wait(self) -> None:
            close_wait_boundary.set()

    segmentation = CloseBarrierClient()
    segmentation.start()
    segment_errors: list[AppError] = []
    segment_thread = threading.Thread(
        target=lambda: _capture_segment_error(segmentation, segment_errors)
    )
    segment_thread.start()
    assert receive_entered.wait(timeout=1)
    assert segmentation.cancel("j1")

    def close() -> None:
        segmentation.close()
        close_finished.set()

    close_thread = threading.Thread(target=close)
    close_thread.start()
    boundary_seen = close_wait_boundary.wait(timeout=1)
    if not boundary_seen:
        release_receive.set()
        segment_thread.join(timeout=2)
        close_thread.join(timeout=2)
    assert boundary_seen
    assert not close_finished.is_set()
    release_receive.set()
    segment_thread.join(timeout=2)
    close_thread.join(timeout=2)
    assert not segment_thread.is_alive()
    assert not close_thread.is_alive()
    assert segment_errors and segment_errors[0].code is ErrorCode.JOB_CANCELLED
    assert close_finished.is_set()
    assert not segmentation.is_running
    assert segmentation.shared_memory_name is None
    assert segmentation.active_job_id is None


def test_cancel_during_invalid_image_preparation_is_not_admitted() -> None:
    validation_entered = threading.Event()
    release_validation = threading.Event()

    class BarrierClient(SegmentationClient):
        def _validate_image(self, image: object) -> np.ndarray:
            validation_entered.set()
            assert release_validation.wait(timeout=1)
            return super()._validate_image(image)

    segmentation = BarrierClient(
        {"mode": "success"}, child_target=fake_segmentation_child
    )
    segmentation.start()
    caught: list[AppError] = []
    thread = threading.Thread(
        target=lambda: _capture_custom_segment_error(
            segmentation, np.zeros((2, 2), dtype=np.uint8), caught
        )
    )
    thread.start()
    assert validation_entered.wait(timeout=1)
    cancel_result = segmentation.cancel("j1")
    release_validation.set()
    thread.join(timeout=1)
    assert not cancel_result
    assert caught and caught[0].code is ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH
    assert segmentation.active_job_id is None
    segmentation.close()


def test_cancel_during_slot_allocation_failure_is_not_admitted() -> None:
    allocation_entered = threading.Event()
    release_allocation = threading.Event()

    class BarrierClient(SegmentationClient):
        def _ensure_slot_unlocked(self, capacity: int, job_id: str) -> SharedMemory:
            allocation_entered.set()
            assert release_allocation.wait(timeout=1)
            raise self._crash_error("injected allocation failure", job_id=job_id)

    segmentation = BarrierClient(
        {"mode": "success"}, child_target=fake_segmentation_child
    )
    segmentation.start()
    caught: list[AppError] = []
    thread = threading.Thread(
        target=lambda: _capture_segment_error(segmentation, caught)
    )
    thread.start()
    assert allocation_entered.wait(timeout=1)
    cancel_result = segmentation.cancel("j1")
    release_allocation.set()
    thread.join(timeout=1)
    assert not cancel_result
    assert caught and caught[0].job_id == "j1"
    assert segmentation.active_job_id is None
    segmentation.close()


def test_cancel_during_slot_write_failure_is_not_admitted() -> None:
    write_entered = threading.Event()
    release_write = threading.Event()

    class FailingBuffer:
        def __setitem__(self, _key: object, _value: object) -> None:
            write_entered.set()
            assert release_write.wait(timeout=1)
            raise OSError("injected slot write failure")

    class FailingSlot:
        name = "failing-write-slot"
        size = 256
        buf = FailingBuffer()

        def close(self) -> None:
            return

        def unlink(self) -> None:
            return

    segmentation = client()
    segmentation.start()
    segmentation._slot = FailingSlot()  # type: ignore[assignment]
    segmentation._slot_capacity = 256
    segmentation._slot_closed = False
    segmentation._slot_unlinked = False
    caught: list[AppError] = []
    thread = threading.Thread(
        target=lambda: _capture_segment_error(segmentation, caught)
    )
    thread.start()
    assert write_entered.wait(timeout=1)
    cancel_result = segmentation.cancel("j1")
    release_write.set()
    thread.join(timeout=1)
    assert not cancel_result
    assert caught and caught[0].code is ErrorCode.SEGMENTATION_PROCESS_CRASHED
    assert caught[0].job_id == "j1"
    assert segmentation.active_job_id is None
    assert segmentation.shared_memory_name is None


def test_cancel_during_request_serialization_failure_is_not_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serialization_entered = threading.Event()
    release_serialization = threading.Event()
    segmentation = client()
    segmentation.start()
    original_encode = segmentation_host_module.encode_parent_message

    def fail_request(message: object) -> bytes:
        if isinstance(message, SegmentRequest):
            serialization_entered.set()
            assert release_serialization.wait(timeout=1)
            raise ProtocolCodecError("injected serialization failure")
        return original_encode(message)  # type: ignore[arg-type]

    monkeypatch.setattr(segmentation_host_module, "encode_parent_message", fail_request)
    caught: list[AppError] = []
    thread = threading.Thread(
        target=lambda: _capture_segment_error(segmentation, caught)
    )
    thread.start()
    assert serialization_entered.wait(timeout=1)
    cancel_result = segmentation.cancel("j1")
    release_serialization.set()
    thread.join(timeout=1)
    assert not cancel_result
    assert caught and caught[0].code is ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH
    assert caught[0].job_id == "j1"
    assert segmentation.active_job_id is None


def test_close_during_local_preparation_prevents_cancel_admission() -> None:
    prepared = threading.Event()
    release_preparation = threading.Event()
    close_finished = threading.Event()

    class BarrierClient(SegmentationClient):
        def _after_local_preparation(self) -> None:
            prepared.set()
            assert release_preparation.wait(timeout=1)

    segmentation = BarrierClient(
        {"mode": "success"}, child_target=fake_segmentation_child
    )
    segmentation.start()
    caught: list[AppError] = []
    thread = threading.Thread(
        target=lambda: _capture_segment_error(segmentation, caught)
    )
    thread.start()
    assert prepared.wait(timeout=1)
    cancel_result = segmentation.cancel("j1")
    close_thread = threading.Thread(
        target=lambda: (segmentation.close(), close_finished.set())
    )
    close_thread.start()
    assert not close_finished.wait(timeout=0.05)
    release_preparation.set()
    thread.join(timeout=1)
    close_thread.join(timeout=1)
    assert not cancel_result
    assert not caught
    assert close_finished.is_set()
    assert segmentation.active_job_id is None


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


def test_close_during_inference_waits_for_result_then_unlinks_slot() -> None:
    segmentation = client("delayed-cancel")
    segmentation.start()
    caught: list[AppError] = []
    outputs: list[np.ndarray] = []

    def run_segment() -> None:
        try:
            outputs.append(segmentation.segment(red_frame(), request()))
        except AppError as error:
            caught.append(error)

    thread = threading.Thread(target=run_segment)
    thread.start()
    deadline = time.monotonic() + 1
    while segmentation.active_job_id is None and time.monotonic() < deadline:
        time.sleep(0.005)
    slot_name = segmentation.shared_memory_name
    assert slot_name is not None
    close_thread = threading.Thread(target=segmentation.close)
    close_thread.start()
    thread.join(timeout=2)
    close_thread.join(timeout=2)
    assert not thread.is_alive()
    assert not close_thread.is_alive()
    assert not caught
    assert outputs and outputs[0].shape == (4, 5, 4)
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


@pytest.mark.parametrize("version", [999, True])
def test_malformed_request_invalidates_the_process_before_transport(
    version: object,
) -> None:
    class AllocationTrackingClient(SegmentationClient):
        allocation_calls = 0

        def _ensure_slot_unlocked(self, capacity: int, job_id: str) -> SharedMemory:
            self.allocation_calls += 1
            return super()._ensure_slot_unlocked(capacity, job_id)

    segmentation = AllocationTrackingClient(
        {"mode": "success"}, child_target=fake_segmentation_child
    )
    segmentation.start()
    with pytest.raises(AppError) as exc:
        segmentation.segment(red_frame(), SegmentRequest(version, "j1", "r1"))  # type: ignore[arg-type]
    assert exc.value.code is ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH
    assert exc.value.job_id == "j1"
    assert not segmentation.is_running
    assert segmentation.allocation_calls == 0
    assert segmentation.shared_memory_name is None


class _SlotDouble:
    name = "test-slot"
    size = 64

    def __init__(self, *, close_failures: int = 0, unlink_failures: int = 0) -> None:
        self.closed = False
        self.unlinked = False
        self.close_failures = close_failures
        self.unlink_failures = unlink_failures
        self.close_calls = 0
        self.unlink_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.close_failures:
            self.close_failures -= 1
            raise OSError("close failed")
        self.closed = True

    def unlink(self) -> None:
        self.unlink_calls += 1
        if self.unlink_failures:
            self.unlink_failures -= 1
            raise OSError("unlink failed")
        self.unlinked = True


class _ConnectionDouble:
    def __init__(self, *, close_failures: int = 0, poll_failures: int = 0) -> None:
        self.closed = False
        self.close_failures = close_failures
        self.poll_failures = poll_failures
        self.close_calls = 0

    def poll(self, _timeout: float = 0.0) -> bool:
        if self.poll_failures:
            self.poll_failures -= 1
            raise OSError("poll failed")
        return False

    def send_bytes(self, _payload: bytes) -> None:
        return

    def close(self) -> None:
        self.close_calls += 1
        if self.close_failures:
            self.close_failures -= 1
            raise OSError("close failed")
        self.closed = True


class _ProcessDouble:
    pid = 4242
    exitcode = 0

    def __init__(
        self,
        *,
        alive: bool,
        stubborn: bool = False,
        join_error: bool = False,
        close_failures: int = 0,
        start_error: bool = False,
    ) -> None:
        self.alive = alive
        self.stubborn = stubborn
        self.join_error = join_error
        self.close_failures = close_failures
        self.start_error = start_error
        self.actions: list[str] = []

    def start(self) -> None:
        self.actions.append("start")
        if self.start_error:
            raise OSError("process start failed")
        self.alive = True

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
        if self.close_failures:
            self.close_failures -= 1
            raise OSError("process close failed")


class _StartupContextDouble:
    def __init__(
        self,
        parent: _ConnectionDouble,
        child: _ConnectionDouble,
        process: _ProcessDouble,
    ) -> None:
        self.parent = parent
        self.child = child
        self.process = process

    def Pipe(self, *, duplex: bool) -> tuple[_ConnectionDouble, _ConnectionDouble]:
        assert duplex
        return self.parent, self.child

    def Process(self, **_kwargs: object) -> _ProcessDouble:
        return self.process


def test_spawn_start_failure_retains_failed_parent_close_until_retry() -> None:
    parent = _ConnectionDouble(close_failures=1)
    child = _ConnectionDouble()
    process = _ProcessDouble(alive=False, start_error=True)
    context = _StartupContextDouble(parent, child, process)
    segmentation = SegmentationClient(
        {"mode": "success"},
        child_target=fake_segmentation_child,
        mp_context=context,  # type: ignore[arg-type]
    )

    with pytest.raises(AppError) as exc:
        segmentation.start()
    assert exc.value.code is ErrorCode.SEGMENTATION_CLEANUP_FAILED
    assert segmentation._connection is parent
    assert segmentation._child_endpoint is None
    assert segmentation._process is None
    assert parent.close_calls == 1
    assert child.closed
    assert process.actions == ["start", "close"]
    assert not process.alive

    segmentation._mp_context = multiprocessing.get_context("spawn")
    segmentation.start()
    assert parent.close_calls == 2
    assert segmentation.is_running
    segmentation.close()


def test_child_endpoint_close_failure_is_owned_until_cleanup_retry() -> None:
    parent = _ConnectionDouble()
    child = _ConnectionDouble(close_failures=2)
    process = _ProcessDouble(alive=False)
    context = _StartupContextDouble(parent, child, process)
    segmentation = SegmentationClient(
        {"mode": "success"},
        child_target=fake_segmentation_child,
        mp_context=context,  # type: ignore[arg-type]
    )

    with pytest.raises(AppError) as exc:
        segmentation.start()
    assert exc.value.code is ErrorCode.SEGMENTATION_CLEANUP_FAILED
    assert segmentation._connection is None
    assert segmentation._child_endpoint is child
    assert segmentation._process is None
    assert parent.closed
    assert child.close_calls == 2
    assert not process.alive
    assert "terminate" in process.actions
    assert "close" in process.actions

    segmentation._mp_context = multiprocessing.get_context("spawn")
    segmentation.start()
    assert child.close_calls == 3
    assert segmentation._child_endpoint is None
    assert segmentation.is_running
    segmentation.close()


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
    segmentation._slot_closed = False
    segmentation._slot_unlinked = False


def test_dead_process_before_request_invalidates_connection_and_slot() -> None:
    segmentation = client()
    process = _ProcessDouble(alive=False)
    connection = _ConnectionDouble()
    slot = _SlotDouble()
    _install_process_doubles(segmentation, process, connection, slot)

    with pytest.raises(AppError) as exc:
        segmentation.segment(red_frame(), request())
    assert exc.value.code is ErrorCode.SEGMENTATION_PROCESS_CRASHED
    assert exc.value.job_id == "j1"
    assert connection.closed
    assert slot.closed and slot.unlinked
    assert segmentation.shared_memory_name is None


def test_pre_request_poll_failure_is_structured_and_preserves_job_id() -> None:
    segmentation = client()
    process = _ProcessDouble(alive=True)
    connection = _ConnectionDouble(poll_failures=1)
    slot = _SlotDouble()
    _install_process_doubles(segmentation, process, connection, slot)

    with pytest.raises(AppError) as exc:
        segmentation.segment(red_frame(), request())
    assert exc.value.code is ErrorCode.SEGMENTATION_PROCESS_CRASHED
    assert exc.value.job_id == "j1"
    assert connection.closed
    assert not process.alive
    assert slot.closed and slot.unlinked


def test_cancel_while_dead_child_cleanup_is_blocked_is_not_admitted() -> None:
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()

    class BarrierClient(SegmentationClient):
        def _discard_process_unlocked(
            self, *, graceful: bool, job_id: str | None = None
        ) -> None:
            cleanup_entered.set()
            assert release_cleanup.wait(timeout=1)
            super()._discard_process_unlocked(graceful=graceful, job_id=job_id)

    segmentation = BarrierClient(
        {"mode": "success"}, child_target=fake_segmentation_child
    )
    _install_process_doubles(
        segmentation, _ProcessDouble(alive=False), _ConnectionDouble(), _SlotDouble()
    )
    caught: list[AppError] = []
    thread = threading.Thread(
        target=lambda: _capture_segment_error(segmentation, caught)
    )
    thread.start()
    assert cleanup_entered.wait(timeout=1)
    cancel_result = segmentation.cancel("j1")
    release_cleanup.set()
    thread.join(timeout=1)
    assert not cancel_result
    assert caught and caught[0].job_id == "j1"
    assert segmentation.active_job_id is None


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
    assert slot.close_calls == 1
    assert slot.unlink_calls == 1

    segmentation.close()
    assert slot.unlinked
    assert slot.close_calls == 1
    assert slot.unlink_calls == 2
    assert segmentation._slot is None


def test_slot_close_failure_and_successful_unlink_are_retried_without_reunlink() -> (
    None
):
    segmentation = client()
    slot = _SlotDouble(close_failures=1)
    _install_process_doubles(
        segmentation, _ProcessDouble(alive=False), _ConnectionDouble(), slot
    )

    with pytest.raises(AppError) as exc:
        segmentation.close()
    assert exc.value.code is ErrorCode.SEGMENTATION_CLEANUP_FAILED
    assert segmentation._slot is slot
    assert slot.close_calls == 1
    assert slot.unlink_calls == 1
    assert slot.unlinked

    segmentation.close()
    assert slot.close_calls == 2
    assert slot.unlink_calls == 1
    assert segmentation._slot is None


def test_connection_close_failure_retains_handle_until_retry() -> None:
    segmentation = client()
    connection = _ConnectionDouble(close_failures=1)
    _install_process_doubles(
        segmentation, _ProcessDouble(alive=False), connection, _SlotDouble()
    )

    with pytest.raises(AppError) as exc:
        segmentation.close()
    assert exc.value.code is ErrorCode.SEGMENTATION_CLEANUP_FAILED
    assert segmentation._connection is connection
    assert connection.close_calls == 1

    segmentation.close()
    assert connection.close_calls == 2
    assert segmentation._connection is None


def test_residual_connection_blocks_start_until_cleanup_retry_succeeds() -> None:
    segmentation = client()
    connection = _ConnectionDouble(close_failures=1)
    _install_process_doubles(
        segmentation, _ProcessDouble(alive=False), connection, _SlotDouble()
    )

    with pytest.raises(AppError) as exc:
        segmentation.start()
    assert exc.value.code is ErrorCode.SEGMENTATION_CLEANUP_FAILED
    assert segmentation._connection is connection
    assert not segmentation.is_running

    segmentation.start()
    assert connection.close_calls == 2
    assert segmentation.is_running
    segmentation.close()


def test_dead_process_close_failure_retains_handle_until_retry() -> None:
    segmentation = client()
    process = _ProcessDouble(alive=False, close_failures=1)
    _install_process_doubles(segmentation, process, _ConnectionDouble(), _SlotDouble())

    with pytest.raises(AppError) as exc:
        segmentation.close()
    assert exc.value.code is ErrorCode.SEGMENTATION_CLEANUP_FAILED
    assert segmentation._process is process
    assert process.actions == ["close"]

    segmentation.close()
    assert process.actions == ["close", "close"]
    assert segmentation._process is None


def test_cleanup_failure_preserves_active_job_id_in_error() -> None:
    segmentation = client()
    _install_process_doubles(
        segmentation,
        _ProcessDouble(alive=False),
        _ConnectionDouble(close_failures=1),
        _SlotDouble(),
    )
    segmentation._publish_active(request())

    with pytest.raises(AppError) as exc:
        segmentation.close()
    assert exc.value.code is ErrorCode.SEGMENTATION_CLEANUP_FAILED
    assert exc.value.job_id == "j1"
    assert segmentation.active_job_id is None
    segmentation.close()


def _start_production_loop(inference: object) -> tuple[object, threading.Thread]:
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


def _assert_failure_then_late_cancel_ack(
    parent: object, thread: threading.Thread, job_id: str = "j1"
) -> SegmentFailure:
    assert parent.poll(1)  # type: ignore[attr-defined]
    failure = decode_child_message(
        parent.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES)  # type: ignore[attr-defined]
    )
    assert isinstance(failure, SegmentFailure)
    parent.send_bytes(  # type: ignore[attr-defined]
        encode_parent_message(CancelRequest(PROTOCOL_VERSION, job_id))
    )
    assert parent.poll(1)  # type: ignore[attr-defined]
    acknowledgement = decode_child_message(
        parent.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES)  # type: ignore[attr-defined]
    )
    assert acknowledgement == CancelAck(PROTOCOL_VERSION, job_id)
    assert thread.is_alive()
    return failure


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


def test_production_loop_acks_queued_cancel_before_queued_shutdown() -> None:
    entered = threading.Event()
    release = threading.Event()

    def inference(frame: np.ndarray, _session: object) -> np.ndarray:
        entered.set()
        assert release.wait(timeout=1)
        return np.zeros(frame.shape[:2] + (4,), dtype=np.uint8)

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
        parent.send_bytes(encode_parent_message(Shutdown(PROTOCOL_VERSION)))
        release.set()
        assert parent.poll(1)
        assert decode_child_message(
            parent.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES)
        ) == CancelAck(PROTOCOL_VERSION, "j1")
        thread.join(timeout=1)
        assert not thread.is_alive()
        if parent.poll():
            with pytest.raises(EOFError):
                parent.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES)
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
        failure = _assert_failure_then_late_cancel_ack(parent, thread)
        assert failure.error["code"] == ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH
        assert not inference_called
        parent.send_bytes(encode_parent_message(Shutdown(PROTOCOL_VERSION)))
        thread.join(timeout=1)
        assert not thread.is_alive()
    finally:
        parent.close()


def test_production_loop_source_buffer_failure_keeps_cancel_ack_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InvalidBufferSlot:
        name = "invalid-buffer"
        size = 16

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.buf = memoryview(bytearray(1))

        def close(self) -> None:
            return

    monkeypatch.setattr(segmentation_host_module, "SharedMemory", InvalidBufferSlot)
    parent, thread = _start_production_loop(
        lambda frame, _session: np.zeros(frame.shape[:2] + (4,), dtype=np.uint8)
    )
    try:
        parent.send_bytes(
            encode_parent_message(
                SegmentRequest(
                    PROTOCOL_VERSION,
                    "j1",
                    "r1",
                    SharedFrame("invalid-buffer", (2, 2, 3), "uint8", 12),
                )
            )
        )
        failure = _assert_failure_then_late_cancel_ack(parent, thread)
        assert failure.error["code"] == ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH
        parent.send_bytes(encode_parent_message(Shutdown(PROTOCOL_VERSION)))
        thread.join(timeout=1)
        assert not thread.is_alive()
    finally:
        parent.close()


@pytest.mark.parametrize("failure_mode", ["acquire", "copy", "write"])
def test_production_loop_output_buffer_failure_acks_late_cancel_without_stale_state(
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    class OutputFailSlot:
        name = "output-fail-slot"
        size = 16
        failed = False

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.storage = memoryview(bytearray(16))
            self.buf_accesses = 0

        @property
        def buf(self) -> memoryview:
            self.buf_accesses += 1
            if (
                self.buf_accesses == 2
                and not type(self).failed
                and failure_mode == "acquire"
            ):
                type(self).failed = True
                raise BufferError("injected output buffer acquisition failure")
            if (
                self.buf_accesses == 2
                and not type(self).failed
                and failure_mode == "write"
            ):
                type(self).failed = True
                return memoryview(bytes(16))
            return self.storage

        def close(self) -> None:
            return

    monkeypatch.setattr(segmentation_host_module, "SharedMemory", OutputFailSlot)

    class CopyFailArray(np.ndarray):
        def tobytes(self, order: str = "C") -> bytes:
            raise BufferError("injected result copy failure")

    def inference(frame: np.ndarray, _session: object) -> np.ndarray:
        result = np.zeros(frame.shape[:2] + (4,), dtype=np.uint8)
        if failure_mode == "copy" and not OutputFailSlot.failed:
            OutputFailSlot.failed = True
            return result.view(CopyFailArray)
        return result

    parent, thread = _start_production_loop(inference)
    wire_slot = SharedFrame("output-fail-slot", (2, 2, 3), "uint8", 12)
    try:
        parent.send_bytes(
            encode_parent_message(
                SegmentRequest(PROTOCOL_VERSION, "j1", "r1", wire_slot)
            )
        )
        failure = _assert_failure_then_late_cancel_ack(parent, thread)
        assert failure.error["code"] == ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH

        parent.send_bytes(
            encode_parent_message(
                SegmentRequest(PROTOCOL_VERSION, "j1", "r2", wire_slot)
            )
        )
        assert parent.poll(1)
        response = decode_child_message(parent.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES))
        assert response == SegmentResponse(
            PROTOCOL_VERSION, "j1", "r2", (2, 2, 4), "uint8", 16
        )
        parent.send_bytes(encode_parent_message(Shutdown(PROTOCOL_VERSION)))
        thread.join(timeout=1)
        assert not thread.is_alive()
    finally:
        parent.close()


class _CustomBytes(bytes):
    pass


@pytest.mark.parametrize(
    "invalid_payload",
    [
        pytest.param(b"\x11" * 15, id="short"),
        pytest.param(b"\x22" * 17, id="long"),
        pytest.param(bytearray(b"\x33" * 16), id="non-bytes"),
        pytest.param(_CustomBytes(b"\x44" * 16), id="bytes-subclass"),
    ],
)
def test_production_loop_rejects_invalid_tobytes_payload_before_output_write(
    monkeypatch: pytest.MonkeyPatch,
    invalid_payload: object,
) -> None:
    class OutputSlot:
        name = "output-payload-slot"
        size = 32
        instances: list[OutputSlot] = []

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.storage = memoryview(bytearray(b"\xa5" * self.size))
            type(self).instances.append(self)

        @property
        def buf(self) -> memoryview:
            return self.storage

        def close(self) -> None:
            return

    class InvalidPayloadArray(np.ndarray):
        def tobytes(self, order: str = "C") -> bytes:
            return invalid_payload  # type: ignore[return-value]

    inference_calls = 0

    def inference(frame: np.ndarray, _session: object) -> np.ndarray:
        nonlocal inference_calls
        inference_calls += 1
        result = np.zeros(frame.shape[:2] + (4,), dtype=np.uint8)
        if inference_calls == 1:
            return result.view(InvalidPayloadArray)
        return result

    monkeypatch.setattr(segmentation_host_module, "SharedMemory", OutputSlot)
    parent, thread = _start_production_loop(inference)
    wire_slot = SharedFrame("output-payload-slot", (2, 2, 3), "uint8", 12)
    try:
        parent.send_bytes(
            encode_parent_message(
                SegmentRequest(PROTOCOL_VERSION, "j1", "r1", wire_slot)
            )
        )
        failure = _assert_failure_then_late_cancel_ack(parent, thread)
        assert failure.job_id == "j1"
        assert failure.request_id == "r1"
        assert failure.error["code"] == ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH
        assert bytes(OutputSlot.instances[0].storage) == b"\xa5" * 32

        parent.send_bytes(
            encode_parent_message(
                SegmentRequest(PROTOCOL_VERSION, "j1", "r2", wire_slot)
            )
        )
        assert parent.poll(1)
        response = decode_child_message(parent.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES))
        assert response == SegmentResponse(
            PROTOCOL_VERSION, "j1", "r2", (2, 2, 4), "uint8", 16
        )
        parent.send_bytes(encode_parent_message(Shutdown(PROTOCOL_VERSION)))
        thread.join(timeout=1)
        assert not thread.is_alive()
    finally:
        parent.close()


def test_production_loop_poll_transport_failure_does_not_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LocalSlot:
        name = "local-slot"
        size = 16

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.buf = memoryview(bytearray(16))

        def close(self) -> None:
            return

    class PollFailConnection:
        def __init__(self, wrapped: object) -> None:
            self.wrapped = wrapped

        def send_bytes(self, payload: bytes) -> None:
            self.wrapped.send_bytes(payload)  # type: ignore[attr-defined]

        def recv_bytes(self, max_length: int) -> bytes:
            return self.wrapped.recv_bytes(max_length)  # type: ignore[attr-defined,no-any-return]

        def poll(self, _timeout: float = 0.0) -> bool:
            raise OSError("injected child poll failure")

    monkeypatch.setattr(segmentation_host_module, "SharedMemory", LocalSlot)
    parent, child = Pipe(duplex=True)
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            _serve_segmentation_connection(
                PollFailConnection(child),
                object(),
                lambda frame, _session: np.zeros(
                    frame.shape[:2] + (4,), dtype=np.uint8
                ),
                process_id=123,
            )
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=serve)
    thread.start()
    assert decode_child_message(
        parent.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES)
    ) == WorkerReady(PROTOCOL_VERSION, CONTROL_JOB_ID, 123)
    parent.send_bytes(
        encode_parent_message(
            SegmentRequest(
                PROTOCOL_VERSION,
                "j1",
                "r1",
                SharedFrame("local-slot", (2, 2, 3), "uint8", 12),
            )
        )
    )
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert not errors
    parent.close()
    child.close()


@pytest.mark.parametrize(
    "wire_mutation",
    [
        lambda raw: raw.replace(b'"byte_length":12', b'"byte_length":20').replace(
            b'"shape":[2,2,3]', b'"shape":[2,2,5]'
        ),
        lambda raw: raw.replace(b'"dtype":"uint8"', b'"dtype":"float32"'),
    ],
)
def test_production_loop_rejects_descriptor_shape_dtype_and_byte_length(
    wire_mutation: object,
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
    failure = _assert_failure_then_late_cancel_ack(parent, thread)
    assert failure.error["code"] == ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH
    parent.send_bytes(encode_parent_message(Shutdown(PROTOCOL_VERSION)))
    thread.join(timeout=1)
    assert not thread.is_alive()
    parent.close()


def test_production_loop_attach_failure_still_acknowledges_late_cancel() -> None:
    parent, thread = _start_production_loop(
        lambda frame, _session: np.zeros(frame.shape[:2] + (4,), dtype=np.uint8)
    )
    parent.send_bytes(
        encode_parent_message(
            SegmentRequest(
                PROTOCOL_VERSION,
                "j1",
                "r1",
                SharedFrame("definitely-missing-slot", (2, 2, 3), "uint8", 12),
            )
        )
    )
    _assert_failure_then_late_cancel_ack(parent, thread)
    parent.send_bytes(encode_parent_message(Shutdown(PROTOCOL_VERSION)))
    thread.join(timeout=1)
    assert not thread.is_alive()
    parent.close()


def test_production_loop_inference_failure_still_acknowledges_late_cancel() -> None:
    def inference(_frame: np.ndarray, _session: object) -> np.ndarray:
        raise ValueError("injected inference failure")

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
        failure = _assert_failure_then_late_cancel_ack(parent, thread)
        assert failure.error["code"] == ErrorCode.INVALID_SEGMENTATION
        parent.send_bytes(encode_parent_message(Shutdown(PROTOCOL_VERSION)))
        thread.join(timeout=1)
        assert not thread.is_alive()
    finally:
        parent.close()
        slot.close()
        slot.unlink()


def test_late_cancel_after_invalid_result_has_no_stale_effect_on_next_request() -> None:
    drain_completed = threading.Event()
    release_after_late_cancel = threading.Event()
    call_count = 0

    class PollBarrierConnection:
        def __init__(self, wrapped: object) -> None:
            self.wrapped = wrapped
            self.poll_calls = 0

        def send_bytes(self, payload: bytes) -> None:
            self.wrapped.send_bytes(payload)  # type: ignore[attr-defined]

        def recv_bytes(self, max_length: int) -> bytes:
            return self.wrapped.recv_bytes(max_length)  # type: ignore[attr-defined,no-any-return]

        def poll(self, timeout: float = 0.0) -> bool:
            result = self.wrapped.poll(timeout)  # type: ignore[attr-defined]
            self.poll_calls += 1
            if self.poll_calls == 1:
                drain_completed.set()
                assert release_after_late_cancel.wait(timeout=1)
            return result  # type: ignore[no-any-return]

    parent, child = Pipe(duplex=True)

    def inference(frame: np.ndarray, _session: object) -> np.ndarray:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return np.zeros(frame.shape[:2] + (3,), dtype=np.uint8)
        return np.zeros(frame.shape[:2] + (4,), dtype=np.uint8)

    wrapped = PollBarrierConnection(child)
    thread = threading.Thread(
        target=_serve_segmentation_connection,
        args=(wrapped, object(), inference),
        kwargs={"process_id": 123},
    )
    thread.start()
    assert decode_child_message(
        parent.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES)
    ) == WorkerReady(PROTOCOL_VERSION, CONTROL_JOB_ID, 123)
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
        assert drain_completed.wait(timeout=1)
        parent.send_bytes(encode_parent_message(CancelRequest(PROTOCOL_VERSION, "j1")))
        release_after_late_cancel.set()
        failure = decode_child_message(parent.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES))
        assert isinstance(failure, SegmentFailure)
        assert decode_child_message(
            parent.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES)
        ) == CancelAck(PROTOCOL_VERSION, "j1")

        parent.send_bytes(
            encode_parent_message(
                SegmentRequest(
                    PROTOCOL_VERSION,
                    "j1",
                    "r2",
                    SharedFrame(slot.name, frame.shape, "uint8", frame.nbytes),
                )
            )
        )
        response = decode_child_message(parent.recv_bytes(MAX_PROTOCOL_MESSAGE_BYTES))
        assert response == SegmentResponse(
            PROTOCOL_VERSION, "j1", "r2", (2, 2, 4), "uint8", 16
        )
        parent.send_bytes(encode_parent_message(Shutdown(PROTOCOL_VERSION)))
        thread.join(timeout=1)
        assert not thread.is_alive()
    finally:
        parent.close()
        slot.close()
        slot.unlink()


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
