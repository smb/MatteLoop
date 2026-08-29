from __future__ import annotations

import pickle
import threading
import time
from multiprocessing import active_children
from multiprocessing.shared_memory import SharedMemory

import numpy as np
import pytest

import rembggui.app as app_module
from rembggui.core.errors import AppError, ErrorCode
from rembggui.jobs.protocol import (
    PROTOCOL_VERSION,
    CancelAck,
    CancelRequest,
    SegmentRequest,
    SegmentResponse,
    SharedFrame,
    Shutdown,
)
from rembggui.jobs.segmentation_host import SegmentationClient
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


def test_protocol_dataclasses_are_frozen_spawn_serializable() -> None:
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
    )
    assert tuple(pickle.loads(pickle.dumps(item)) for item in messages) == messages
    with pytest.raises((AttributeError, TypeError)):
        messages[0].job_id = "changed"  # type: ignore[misc]


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
        "wrong-byte-length",
        "late-response",
        "malformed-error",
        "unsolicited-ack",
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
    segmentation = SegmentationClient(
        unpicklable_model_spec(), child_target=fake_segmentation_child
    )
    with pytest.raises(AppError) as exc:
        segmentation.start()
    assert exc.value.code is ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH
    assert not segmentation.is_running


def test_malformed_request_invalidates_the_process_before_transport() -> None:
    segmentation = client()
    segmentation.start()
    with pytest.raises(AppError) as exc:
        segmentation.segment(red_frame(), SegmentRequest(999, "j1", "r1"))
    assert exc.value.code is ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH
    assert not segmentation.is_running
