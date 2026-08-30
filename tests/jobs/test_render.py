from __future__ import annotations

import errno
import os
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from PIL import Image

import rembggui.jobs.render as render_module
from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.specs import CollisionPolicy, FramingSpec, SamplingSpec
from rembggui.core.state import JobKind
from rembggui.core.webp import EncodeSummary, encode_lossless_webp, validate_webp
from rembggui.jobs.context import JobTerminalState
from rembggui.jobs.render import (
    AtomicOutputPublisher,
    FilesystemWorkspacePort,
    PillowWebPEncoder,
    PreparedSegmentation,
    RenderService,
    ValidatedCandidate,
)
from tests.jobs.render_support import (
    FakeClock,
    FakeDiskProbe,
    FakeEncoder,
    FakeSegmenter,
    FakeSource,
    job,
    render_service,
    request,
)


def _validated_candidate(
    publisher: AtomicOutputPublisher,
    target: Path,
    directory: Path,
    label: str,
) -> ValidatedCandidate:
    source = directory / f"source-{label}.png"
    with Image.new("RGBA", (128, 128), (10, 20, 30, 40)) as image:
        image.save(source)
    path = publisher.candidate_path(target, f"job-{label}")
    summary = encode_lossless_webp((source,), (100,), path)
    return ValidatedCandidate.validate(path, summary)


def test_render_samples_half_open_range_and_uses_private_encoder_inputs(
    tmp_path,
) -> None:
    source = FakeSource()
    segmenter = FakeSegmenter()
    encoder = FakeEncoder()
    service = RenderService(
        source=source,
        segmentation=PreparedSegmentation(
            segmenter,
            "birefnet-portrait",
            "ab" * 32,
            "2.0.72",
            frozenset({"standard"}),
        ),
        workspace=FilesystemWorkspacePort(),
        encoder=encoder,
        disk_probe=FakeDiskProbe(),
        clock=FakeClock(),
        output_publisher=AtomicOutputPublisher(),
    )

    artifact = service.render(
        request(tmp_path), job(tmp_path, "render-half-open", JobKind.RENDER)
    )

    assert source.decode_calls == [Fraction(0), Fraction(1, 2)]
    assert artifact.requested_timestamps == (Fraction(0), Fraction(1, 2))
    assert artifact.delays_ms == (500, 500)
    assert artifact.frame_count == 2
    encoded_paths = encoder.calls[0][0]
    assert all("scratch/render-half-open" in path.as_posix() for path in encoded_paths)
    assert all(
        artifact.cut_workspace.path not in path.parents for path in encoded_paths
    )
    assert artifact.ownership_peak <= 3
    assert artifact.ownership_current == 0
    assert validate_webp(artifact.output_path, 2, 1000).lossless


def test_render_encodes_and_validates_a_real_lossless_webp(tmp_path) -> None:
    artifact = render_service(encoder=PillowWebPEncoder()).render(
        request(tmp_path), job(tmp_path, "real-webp", JobKind.RENDER)
    )

    info = validate_webp(artifact.output_path, 2, 1000)
    assert info.lossless
    assert info.has_alpha
    assert (info.width, info.height) == (128, 128)
    assert artifact.ownership_peak <= 3
    assert artifact.ownership_current == 0


@pytest.mark.parametrize(
    ("fps", "expected_count", "expected_delays"),
    [
        (1, 1, (1000,)),
        (2, 2, (500, 500)),
        (7, 7, (143, 143, 143, 142, 143, 143, 143)),
    ],
)
def test_render_handles_one_single_and_many_frames(
    tmp_path, fps: int, expected_count: int, expected_delays: tuple[int, ...]
) -> None:
    render_request = replace(
        request(tmp_path), sampling=SamplingSpec(Fraction(0), Fraction(1), fps)
    )

    artifact = render_service().render(
        render_request, job(tmp_path, f"frames-{fps}", JobKind.RENDER)
    )

    assert artifact.frame_count == expected_count
    assert artifact.delays_ms == expected_delays


def test_empty_frame_is_retained_when_range_union_is_nonempty(tmp_path) -> None:
    class FirstEmptySegmenter(FakeSegmenter):
        def segment(self, frame, request):
            if not self.calls:
                self.calls.append(request)
                return __import__("numpy").zeros(frame.shape[:2] + (4,), dtype="uint8")
            return super().segment(frame, request)

    render_request = replace(
        request(tmp_path),
        framing=FramingSpec(True, Decimal("2"), 40, Decimal("1")),
    )

    artifact = render_service(segmenter=FirstEmptySegmenter()).render(
        render_request, job(tmp_path, "one-empty", JobKind.RENDER)
    )

    assert artifact.frame_count == 2
    assert artifact.manifest.union_metadata is not None


@pytest.mark.parametrize("visible_size", [None, 10])
def test_invalid_range_framing_fails_after_cut_promotion_and_preserves_output(
    tmp_path, visible_size: int | None
) -> None:
    class SmallOrEmptySegmenter(FakeSegmenter):
        def segment(self, frame, request):
            import numpy as np

            self.calls.append(request)
            result = np.zeros(frame.shape[:2] + (4,), dtype=np.uint8)
            if visible_size is not None:
                result[:visible_size, :visible_size, 3] = 255
            return result

    render_request = replace(
        request(tmp_path),
        framing=FramingSpec(True, Decimal("2"), 0, Decimal("1")),
    )
    render_request.output.path.write_bytes(b"old-output")

    with pytest.raises(AppError) as exc:
        render_service(segmenter=SmallOrEmptySegmenter()).render(
            render_request, job(tmp_path, "invalid-framing", JobKind.RENDER)
        )

    expected = (
        ErrorCode.INVALID_FRAMING
        if visible_size is None
        else ErrorCode.INVALID_FINAL_DIMENSIONS
    )
    assert exc.value.code is expected
    assert render_request.output.path.read_bytes() == b"old-output"
    durable = _only_durable_workspace(render_request.output.directory)
    assert FilesystemWorkspacePort().validate(durable).frame_count == 2


def test_impossible_size_after_promotion_keeps_output_and_cuts(tmp_path) -> None:
    class ImpossibleEncoder(FakeEncoder):
        def encode(self, *args, **kwargs):
            destination = args[2]
            destination.write_bytes(b"partial-candidate")
            raise AppError(
                ErrorCode.IMPOSSIBLE_SIZE,
                "auto-fit",
                "error.webp.impossible-size",
                "synthetic impossible target",
                "increase-size-limit",
            )

    render_request = request(tmp_path)
    render_request.output.path.write_bytes(b"old-output")

    with pytest.raises(AppError) as exc:
        render_service(encoder=ImpossibleEncoder()).render(
            render_request, job(tmp_path, "impossible", JobKind.RENDER)
        )

    assert exc.value.code is ErrorCode.IMPOSSIBLE_SIZE
    assert render_request.output.path.read_bytes() == b"old-output"
    assert (
        FilesystemWorkspacePort()
        .validate(_only_durable_workspace(render_request.output.directory))
        .frame_count
        == 2
    )
    assert not tuple(tmp_path.glob(".output.webp.*.candidate"))


def test_low_disk_preflight_is_advisory(tmp_path) -> None:
    class LowDisk(FakeDiskProbe):
        def available_bytes(self, _directory: Path) -> int:
            return 0

    artifact = render_service(disk_probe=LowDisk()).render(
        request(tmp_path), job(tmp_path, "low-disk", JobKind.RENDER)
    )

    assert any("advisory disk estimate" in note for note in artifact.notes)
    assert artifact.output_path.exists()


@pytest.mark.parametrize("boundary", ["decode", "segment"])
def test_cancel_before_promotion_discards_stage_and_preserves_output(
    tmp_path, boundary: str
) -> None:
    context = job(tmp_path, f"cancel-{boundary}", JobKind.RENDER)

    class CancellingSource(FakeSource):
        def decode(self, *args, **kwargs):
            decoded = super().decode(*args, **kwargs)
            context.request_cancel()
            return decoded

    class CancellingSegmenter(FakeSegmenter):
        def segment(self, frame, request):
            result = super().segment(frame, request)
            context.request_cancel()
            return result

    render_request = request(tmp_path)
    render_request.output.path.write_bytes(b"old-output")
    source = CancellingSource() if boundary == "decode" else FakeSource()
    segmenter = CancellingSegmenter() if boundary == "segment" else FakeSegmenter()

    with pytest.raises(AppError) as exc:
        render_service(source=source, segmenter=segmenter).render(
            render_request, context
        )

    assert exc.value.code is ErrorCode.JOB_CANCELLED
    assert context.terminal_state is JobTerminalState.CANCELLED
    assert render_request.output.path.read_bytes() == b"old-output"
    cuts = tmp_path / ".rembggui-work" / "cuts"
    assert not cuts.exists() or not tuple(cuts.iterdir())


@pytest.mark.parametrize("boundary", ["promotion", "framing", "encode"])
def test_cancel_after_promotion_keeps_valid_cuts_and_old_output(
    tmp_path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    context = job(tmp_path, f"cancel-{boundary}", JobKind.RENDER)

    class CancellingWorkspace(FilesystemWorkspacePort):
        def promote_render(self, *args, **kwargs):
            promoted = super().promote_render(*args, **kwargs)
            if boundary == "promotion":
                context.request_cancel()
            return promoted

    class CancellingEncoder(FakeEncoder):
        def encode(self, *args, **kwargs):
            summary = super().encode(*args, **kwargs)
            if boundary == "encode":
                context.request_cancel()
            return summary

    if boundary == "framing":
        actual_persist = render_module._persist_framed_png

        def persist_then_cancel(path, image):
            actual_persist(path, image)
            context.request_cancel()

        monkeypatch.setattr(render_module, "_persist_framed_png", persist_then_cancel)

    render_request = request(tmp_path)
    render_request.output.path.write_bytes(b"old-output")

    with pytest.raises(AppError) as exc:
        render_service(
            workspace=CancellingWorkspace(), encoder=CancellingEncoder()
        ).render(render_request, context)

    assert exc.value.code is ErrorCode.JOB_CANCELLED
    assert context.terminal_state is JobTerminalState.CANCELLED
    assert render_request.output.path.read_bytes() == b"old-output"
    assert (
        FilesystemWorkspacePort()
        .validate(_only_durable_workspace(render_request.output.directory))
        .frame_count
        == 2
    )


def test_child_crash_passes_through_without_automatic_restart(tmp_path) -> None:
    expected = AppError(
        ErrorCode.SEGMENTATION_PROCESS_CRASHED,
        "segmentation",
        "error.segmentation.crashed",
        "synthetic child crash",
        "restart-worker",
    )

    class CrashingSegmenter(FakeSegmenter):
        def segment(self, frame, request):
            del frame
            self.calls.append(request)
            raise expected

    segmenter = CrashingSegmenter()

    with pytest.raises(AppError) as exc:
        render_service(segmenter=segmenter).render(
            request(tmp_path), job(tmp_path, "child-crash", JobKind.RENDER)
        )

    assert exc.value is expected
    assert len(segmenter.calls) == 1


def test_stale_source_before_staging_preserves_old_output(tmp_path) -> None:
    class StaleSource(FakeSource):
        def complete_sha256(self, path, context):
            del path, context
            raise AppError(
                ErrorCode.SOURCE_CHANGED,
                "source-hash",
                "error.source.changed",
                "synthetic concurrent source edit",
                "reload-source",
            )

    render_request = request(tmp_path)
    render_request.output.path.write_bytes(b"old-output")

    with pytest.raises(AppError) as exc:
        render_service(source=StaleSource()).render(
            render_request, job(tmp_path, "stale-source", JobKind.RENDER)
        )

    assert exc.value.code is ErrorCode.SOURCE_CHANGED
    assert render_request.output.path.read_bytes() == b"old-output"
    assert not (tmp_path / ".rembggui-work").exists()


@pytest.mark.parametrize(
    ("error_number", "retry_action"),
    [
        (errno.ENOSPC, "free-disk-space"),
        (getattr(errno, "EDQUOT", errno.ENOSPC), "free-disk-space"),
        (errno.EACCES, "choose-writable-output"),
        (getattr(errno, "EROFS", errno.EACCES), "choose-writable-output"),
        (errno.EIO, "retry-output"),
    ],
)
def test_atomic_replace_maps_real_publish_failures_without_clobbering(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
    retry_action: str,
) -> None:
    target = tmp_path / "output.webp"
    publisher = AtomicOutputPublisher()
    candidate = _validated_candidate(publisher, target, tmp_path, "errno")
    candidate_bytes = candidate.path.read_bytes()
    target.write_bytes(b"old")

    def fail_replace(_source, _target):
        raise OSError(error_number, "synthetic publication failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(candidate, target, CollisionPolicy.REPLACE)
    finally:
        candidate.close()

    assert exc.value.retry_action == retry_action
    assert target.read_bytes() == b"old"
    assert candidate.path.read_bytes() == candidate_bytes


@pytest.mark.parametrize(
    "policy", [CollisionPolicy.CHOOSE_ANOTHER_NAME, CollisionPolicy.CANCEL]
)
def test_no_clobber_policies_lose_atomic_collision_race_safely(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    policy: CollisionPolicy,
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    candidate = _validated_candidate(publisher, target, tmp_path, policy.value)
    candidate_bytes = candidate.path.read_bytes()
    actual_link = os.link

    def create_racer_then_link(source, destination, **kwargs):
        Path(destination).write_bytes(b"racer")
        actual_link(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", create_racer_then_link)

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(candidate, target, policy)
    finally:
        candidate.close()

    assert exc.value.stage == "publish"
    assert target.read_bytes() == b"racer"
    assert candidate.path.read_bytes() == candidate_bytes


def test_replace_policy_commits_candidate_atomically(tmp_path) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    candidate = _validated_candidate(publisher, target, tmp_path, "replace")
    candidate_bytes = candidate.path.read_bytes()
    target.write_bytes(b"old")

    try:
        assert publisher.publish(candidate, target, CollisionPolicy.REPLACE) == target
    finally:
        candidate.close()
    assert target.read_bytes() == candidate_bytes
    assert not candidate.path.exists()


def test_candidate_validation_uses_the_held_file_not_a_swappable_helper_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    valid = _validated_candidate(publisher, target, tmp_path, "valid-source")
    valid.close()
    valid_path = valid.path
    valid_bytes = valid_path.read_bytes()
    invalid_path = publisher.candidate_path(target, "invalid-source")
    invalid_path.write_bytes(b"X" + valid_bytes[1:])
    summary = EncodeSummary(
        invalid_path,
        valid.summary.width,
        valid.summary.height,
        valid.summary.frames,
        valid.summary.duration_ms,
        len(valid_bytes),
    )
    actual_validate = render_module.validate_webp
    opened_descriptors: list[int] = []
    actual_open = render_module._open_held_file

    def observe_open(path: Path) -> int:
        descriptor = actual_open(path)
        opened_descriptors.append(descriptor)
        return descriptor

    def swap_path_before_reopened_validation(source, *args, **kwargs):
        if isinstance(source, Path) and source.suffix == ".validation":
            os.replace(valid_path, source)
        return actual_validate(source, *args, **kwargs)

    monkeypatch.setattr(render_module, "_open_held_file", observe_open)
    monkeypatch.setattr(
        render_module,
        "validate_webp",
        swap_path_before_reopened_validation,
    )

    with pytest.raises(AppError) as exc:
        ValidatedCandidate.validate(invalid_path, summary)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert opened_descriptors
    for descriptor in opened_descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_replace_rolls_back_if_validated_candidate_is_swapped_at_commit(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    target.write_bytes(b"old-output")
    candidate = _validated_candidate(publisher, target, tmp_path, "swap-replace")
    attacker = tmp_path / ".attacker"
    attacker.write_bytes(b"attacker-not-webp")
    actual_replace = os.replace
    swapped = False

    def swap_candidate_then_replace(source_path, destination_path):
        nonlocal swapped
        if Path(source_path) == candidate.path and Path(destination_path) == target:
            actual_replace(attacker, candidate.path)
            swapped = True
        return actual_replace(source_path, destination_path)

    monkeypatch.setattr(os, "replace", swap_candidate_then_replace)

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(candidate, target, CollisionPolicy.REPLACE)
    finally:
        candidate.close()

    assert swapped
    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert target.read_bytes() == b"old-output"


@pytest.mark.parametrize("rollback_swaps", [1, 2])
def test_replace_recreates_rollback_from_held_old_output_after_temp_swaps(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    rollback_swaps: int,
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    old_bytes = b"known-good-old-output"
    target.write_bytes(old_bytes)
    candidate = _validated_candidate(publisher, target, tmp_path, "held-rollback")
    candidate_attacker = tmp_path / ".candidate-attacker"
    candidate_attacker.write_bytes(b"candidate-attacker")
    rollback_attackers = []
    for index in range(rollback_swaps):
        attacker = tmp_path / f".rollback-attacker-{index}"
        attacker.write_bytes(f"rollback-attacker-{index}".encode())
        rollback_attackers.append(attacker)
    actual_replace = os.replace
    actual_open = render_module._open_held_file
    actual_close = os.close
    publisher_descriptors: list[int] = []
    closed_descriptors: list[int] = []
    candidate_swapped = False
    rollback_swap_count = 0

    def observe_open(path: Path) -> int:
        descriptor = actual_open(path)
        publisher_descriptors.append(descriptor)
        return descriptor

    def observe_close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        actual_close(descriptor)

    def swap_publication_and_rollback(source_path, destination_path):
        nonlocal candidate_swapped, rollback_swap_count
        source = Path(source_path)
        destination = Path(destination_path)
        if source == candidate.path and destination == target:
            actual_replace(candidate_attacker, candidate.path)
            candidate_swapped = True
        elif (
            destination == target
            and source.suffix in {".rollback", ".restore"}
            and rollback_swap_count < rollback_swaps
        ):
            actual_replace(rollback_attackers[rollback_swap_count], source)
            rollback_swap_count += 1
        return actual_replace(source, destination)

    monkeypatch.setattr(render_module, "_open_held_file", observe_open)
    monkeypatch.setattr(render_module.os, "close", observe_close)
    monkeypatch.setattr(os, "replace", swap_publication_and_rollback)

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(candidate, target, CollisionPolicy.REPLACE)

        assert exc.value.code is ErrorCode.INVALID_OUTPUT
        assert candidate_swapped
        assert rollback_swap_count == rollback_swaps
        assert target.read_bytes() == old_bytes
        assert set(publisher_descriptors) <= set(closed_descriptors)
        assert not tuple(tmp_path.glob(".output.webp.*.rollback"))
        assert not tuple(tmp_path.glob(".output.webp.*.restore"))
    finally:
        candidate.close()


def test_no_clobber_publishes_held_copy_when_candidate_path_is_swapped(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    candidate = _validated_candidate(publisher, target, tmp_path, "swap-link")
    candidate_bytes = candidate.path.read_bytes()
    attacker = tmp_path / ".attacker"
    attacker_bytes = b"attacker-not-webp"
    attacker.write_bytes(attacker_bytes)
    actual_link = os.link
    swapped = False
    notes: list[str] = []

    def swap_candidate_then_link(source_path, destination_path, **kwargs):
        nonlocal swapped
        os.replace(attacker, candidate.path)
        swapped = True
        return actual_link(source_path, destination_path, **kwargs)

    monkeypatch.setattr(os, "link", swap_candidate_then_link)

    try:
        assert (
            publisher.publish(
                candidate,
                target,
                CollisionPolicy.CHOOSE_ANOTHER_NAME,
                cleanup_notes=notes,
            )
            == target
        )
    finally:
        candidate.close()

    assert swapped
    assert target.read_bytes() == candidate_bytes
    assert candidate.path.read_bytes() == attacker_bytes
    assert any("foreign output-candidate path retained" in note for note in notes)
    assert not tuple(tmp_path.glob(".output.webp.*.publish"))


def test_no_clobber_never_unlinks_a_concurrent_output_after_reservation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    candidate = _validated_candidate(publisher, target, tmp_path, "foreign-writer")
    foreign = tmp_path / ".foreign-writer"
    foreign_bytes = b"concurrent-writer-output"
    foreign.write_bytes(foreign_bytes)
    actual_link = os.link
    actual_replace = os.replace
    actual_stage = render_module._stage_descriptor_copy
    staged_descriptors: list[int] = []

    def observe_stage(*args, **kwargs):
        staged = actual_stage(*args, **kwargs)
        staged_descriptors.append(staged.descriptor)
        return staged

    def replace_reserved_output(source_path, destination_path, **kwargs):
        result = actual_link(source_path, destination_path, **kwargs)
        actual_replace(foreign, target)
        return result

    monkeypatch.setattr(os, "link", replace_reserved_output)
    monkeypatch.setattr(render_module, "_stage_descriptor_copy", observe_stage)

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(
                candidate,
                target,
                CollisionPolicy.CHOOSE_ANOTHER_NAME,
            )
    finally:
        candidate.close()

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert target.read_bytes() == foreign_bytes
    assert staged_descriptors
    for descriptor in staged_descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert not tuple(tmp_path.glob(".output.webp.*.publish"))


def test_no_clobber_candidate_cleanup_failure_is_returned_as_a_note(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    candidate = _validated_candidate(publisher, target, tmp_path, "cleanup")
    candidate_bytes = candidate.path.read_bytes()
    notes: list[str] = []
    actual_unlink = Path.unlink

    def fail_candidate_unlink(path: Path, *args, **kwargs):
        if path == candidate.path:
            raise OSError(errno.EACCES, "synthetic candidate cleanup failure")
        return actual_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_candidate_unlink)

    try:
        assert (
            publisher.publish(
                candidate,
                target,
                CollisionPolicy.CHOOSE_ANOTHER_NAME,
                cleanup_notes=notes,
            )
            == target
        )
    finally:
        candidate.close()
    assert target.read_bytes() == candidate_bytes
    assert candidate.path.read_bytes() == candidate_bytes
    assert notes == [
        "additional output-candidate cleanup failure: "
        "[Errno 13] synthetic candidate cleanup failure"
    ]
    assert not tuple(tmp_path.glob(".output.webp.*.publish"))


def test_framed_png_cleanup_failure_is_not_primary(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "frame.png"

    def fail_save(self, path, *args, **kwargs):
        del self, path, args, kwargs
        raise OSError(errno.ENOSPC, "synthetic frame write failure")

    def fail_unlink(self: Path, *args, **kwargs):
        del self, args, kwargs
        raise OSError(errno.EACCES, "synthetic temp cleanup failure")

    monkeypatch.setattr(Image.Image, "save", fail_save)
    monkeypatch.setattr(Path, "unlink", fail_unlink)

    with Image.new("RGBA", (128, 128)) as image:
        with pytest.raises(AppError) as exc:
            render_module._persist_framed_png(output, image)

    assert exc.value.retry_action == "free-disk-space"
    assert exc.value.__notes__ == [
        "additional framed-PNG cleanup failure: "
        "[Errno 13] synthetic temp cleanup failure"
    ]


def test_all_fallible_artifact_identity_work_finishes_before_publish(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    render_request = request(tmp_path)
    render_request.output.path.write_bytes(b"old-output")

    def fail_fingerprint(*_args, **_kwargs):
        raise ValueError("synthetic identity failure")

    monkeypatch.setattr(render_module, "render_fingerprint", fail_fingerprint)

    with pytest.raises(ValueError, match="identity failure"):
        render_service().render(
            render_request, job(tmp_path, "identity-before-publish", JobKind.RENDER)
        )

    assert render_request.output.path.read_bytes() == b"old-output"
    assert not tuple(tmp_path.glob(".output.webp.*.candidate"))
    assert (
        FilesystemWorkspacePort()
        .validate(_only_durable_workspace(render_request.output.directory))
        .frame_count
        == 2
    )


def _only_durable_workspace(output_directory: Path):
    cuts = output_directory / ".rembggui-work" / "cuts"
    durable_paths = tuple(
        path for path in cuts.iterdir() if not path.name.startswith(".")
    )
    assert len(durable_paths) == 1
    return FilesystemWorkspacePort().open_promoted(
        output_directory, durable_paths[0].name
    )
