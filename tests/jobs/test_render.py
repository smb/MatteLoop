from __future__ import annotations

import errno
import gc
import os
import stat
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from PIL import Image

import rembggui.jobs.render as render_module
import rembggui.jobs.workspace as workspace_module
from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.specs import CollisionPolicy, FramingSpec, SamplingSpec
from rembggui.core.state import JobKind
from rembggui.core.webp import EncodeSummary, encode_lossless_webp, validate_webp
from rembggui.jobs.context import JobTerminalState
from rembggui.jobs.models.cache_fs import BoundDirectoryCloseError, UnsafeCacheError
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
    work_dir = directory / f"job-{label}"
    work_dir.mkdir()
    source = directory / f"source-{label}.png"
    with Image.new("RGBA", (128, 128), (10, 20, 30, 40)) as image:
        image.save(source)
    path = publisher.candidate_path(target, f"job-{label}", work_dir)
    summary = encode_lossless_webp((source,), (100,), path)
    return ValidatedCandidate.validate(path, summary)


def test_candidate_path_is_owned_by_the_explicit_private_work_directory(
    tmp_path: Path,
) -> None:
    publisher = AtomicOutputPublisher()
    output = tmp_path / "exports" / "output.webp"
    work_dir = tmp_path / ".rembggui-work" / "scratch" / "render-private"
    work_dir.mkdir(parents=True)

    candidate = publisher.candidate_path(output, "render-private", work_dir)

    assert candidate.parent == work_dir
    assert candidate.name == "output.candidate.webp"
    assert not tuple(output.parent.glob("*.candidate"))
    with pytest.raises(ValueError, match="job-owned"):
        publisher.candidate_path(output, "different-job", work_dir)


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
    assert "scratch/render-half-open" in encoder.calls[0][2].as_posix()
    assert not (tmp_path / ".rembggui-work" / "scratch" / "render-half-open").exists()
    assert artifact.ownership_peak <= 3
    assert artifact.ownership_current == 0
    assert validate_webp(artifact.output_path, 2, 1000).lossless


def test_validated_candidate_is_closed_before_private_scratch_cleanup(
    tmp_path: Path,
) -> None:
    class TrackingEncoder(FakeEncoder):
        validated: ValidatedCandidate | None = None

        def encode(self, *args, **kwargs):
            self.validated = super().encode(*args, **kwargs)
            return self.validated

    encoder = TrackingEncoder()

    class TrackingWorkspace(FilesystemWorkspacePort):
        def cleanup_job(self, output_directory: Path, job_id: str) -> bool:
            assert encoder.validated is not None
            with pytest.raises(OSError):
                os.fstat(encoder.validated._descriptor)
            return super().cleanup_job(output_directory, job_id)

    service = render_service(encoder=encoder, workspace=TrackingWorkspace())

    artifact = service.render(
        request(tmp_path), job(tmp_path, "candidate-owner", JobKind.RENDER)
    )

    assert artifact.output_path.exists()
    assert encoder.validated is not None
    assert not encoder.validated.path.exists()


@pytest.mark.parametrize(
    "native_error",
    [
        UnsafeCacheError("synthetic reparse failure"),
        BoundDirectoryCloseError(OSError("synthetic CloseHandle failure"), None),
    ],
)
def test_publisher_maps_native_workspace_errors_to_output_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_error: BaseException,
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    old_bytes = b"old-output"
    target.write_bytes(old_bytes)
    candidate = _validated_candidate(publisher, target, tmp_path, "native-error")

    def fail_open(_path: Path):
        raise native_error

    monkeypatch.setattr(render_module.PublicationDirectory, "open", fail_open)

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(candidate, target, CollisionPolicy.REPLACE)
    finally:
        candidate.close()

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert exc.value.stage == "output"
    assert target.read_bytes() == old_bytes


def test_successful_commit_surfaces_retryable_publication_close_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RetryOwner:
        closed = False

        def close(self) -> None:
            self.closed = True

    owner = RetryOwner()
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    candidate = _validated_candidate(publisher, target, tmp_path, "close-success")
    candidate_bytes = candidate.path.read_bytes()

    def fail_close(_self, primary=None):
        if primary is not None:
            setattr(
                primary,
                "_rembggui_bound_directory_close_owners",
                (owner,),
            )
            return
        error = UnsafeCacheError("synthetic publication CloseHandle failure")
        setattr(
            error,
            "_rembggui_bound_directory_close_owners",
            (owner,),
        )
        raise error

    monkeypatch.setattr(render_module.PublicationDirectory, "close", fail_close)

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(candidate, target, CollisionPolicy.CHOOSE_ANOTHER_NAME)
    finally:
        candidate.close()

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert target.read_bytes() == candidate_bytes
    gc.collect()
    assert not owner.closed
    assert workspace_module._drain_attached_bound_directory_closes(exc.value) == 1
    assert owner.closed


def test_publication_open_error_transfers_its_retryable_close_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RetryOwner:
        closed = False

        def close(self) -> None:
            self.closed = True

    owner = RetryOwner()
    native_error = OSError(errno.EIO, "synthetic publication-parent open failure")
    setattr(
        native_error,
        "_rembggui_bound_directory_close_owners",
        (owner,),
    )
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    candidate = _validated_candidate(publisher, target, tmp_path, "open-owner")

    def fail_open(_path: Path):
        raise native_error

    monkeypatch.setattr(render_module.PublicationDirectory, "open", fail_open)

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(candidate, target, CollisionPolicy.REPLACE)
    finally:
        candidate.close()

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert getattr(exc.value, "_rembggui_bound_directory_close_owners") == (owner,)
    assert not owner.closed
    assert workspace_module._drain_attached_bound_directory_closes(exc.value) == 1
    assert owner.closed


def test_primary_publish_error_is_preserved_while_close_owner_is_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RetryOwner:
        def close(self) -> None:
            pass

    owner = RetryOwner()
    primary = AppError(
        ErrorCode.INVALID_OUTPUT,
        "publish",
        "error.output.failed",
        "synthetic primary publication failure",
        "retry-output",
    )
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    candidate = _validated_candidate(publisher, target, tmp_path, "close-primary")

    def fail_commit(*_args):
        raise primary

    actual_close = render_module.PublicationDirectory.close

    def attach_owner_then_close(self, received_primary=None):
        assert received_primary is primary
        setattr(
            received_primary,
            "_rembggui_bound_directory_close_owners",
            (owner,),
        )
        return actual_close(self, received_primary)

    monkeypatch.setattr(render_module, "_rename_no_replace", fail_commit)
    monkeypatch.setattr(
        render_module.PublicationDirectory, "close", attach_owner_then_close
    )

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(candidate, target, CollisionPolicy.CHOOSE_ANOTHER_NAME)
    finally:
        candidate.close()

    assert exc.value is primary
    assert getattr(primary, "_rembggui_bound_directory_close_owners") == (owner,)
    assert workspace_module._drain_attached_bound_directory_closes(primary) == 1


def test_private_stage_preparation_closes_directory_with_original_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = RuntimeError("synthetic private-stage preparation failure")
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    candidate = _validated_candidate(publisher, target, tmp_path, "stage-primary")
    received: list[BaseException | None] = []
    actual_close = render_module.RecoveryDirectory.close

    def fail_stage(*_args, **_kwargs):
        raise primary

    def observe_close(self, received_primary=None):
        received.append(received_primary)
        return actual_close(self, received_primary)

    monkeypatch.setattr(render_module, "_stage_bound_recovery_copy", fail_stage)
    monkeypatch.setattr(render_module.RecoveryDirectory, "close", observe_close)

    try:
        with pytest.raises(RuntimeError) as exc:
            publisher.publish(candidate, target, CollisionPolicy.CHOOSE_ANOTHER_NAME)
    finally:
        candidate.close()

    assert exc.value is primary
    assert received == [primary]


@pytest.mark.parametrize(
    "policy", [CollisionPolicy.REPLACE, CollisionPolicy.CHOOSE_ANOTHER_NAME]
)
def test_publication_syncs_source_then_target_after_cross_directory_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: CollisionPolicy,
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    if policy is CollisionPolicy.REPLACE:
        target.write_bytes(b"old-output")
    candidate = _validated_candidate(
        publisher, target, tmp_path, f"sync-{policy.value}"
    )
    events: list[str] = []
    committed = False
    actual_replace = render_module.PublicationDirectory.replace_from
    actual_no_replace = render_module._rename_no_replace
    actual_source_sync = render_module.RecoveryDirectory.fsync
    actual_target_sync = render_module.PublicationDirectory.fsync

    def observe_replace(self, source_directory, source, destination):
        nonlocal committed
        result = actual_replace(self, source_directory, source, destination)
        committed = True
        events.append("commit")
        return result

    def observe_no_replace(
        source_directory, source, destination_directory, destination
    ):
        nonlocal committed
        result = actual_no_replace(
            source_directory, source, destination_directory, destination
        )
        committed = True
        events.append("commit")
        return result

    def observe_source_sync(self):
        if committed:
            events.append("source-sync")
        return actual_source_sync(self)

    def observe_target_sync(self):
        if committed:
            events.append("target-sync")
        return actual_target_sync(self)

    monkeypatch.setattr(
        render_module.PublicationDirectory, "replace_from", observe_replace
    )
    monkeypatch.setattr(render_module, "_rename_no_replace", observe_no_replace)
    monkeypatch.setattr(render_module.RecoveryDirectory, "fsync", observe_source_sync)
    monkeypatch.setattr(
        render_module.PublicationDirectory, "fsync", observe_target_sync
    )

    try:
        publisher.publish(candidate, target, policy)
    finally:
        candidate.close()

    assert events == ["commit", "source-sync", "target-sync"]


def test_target_sync_failure_after_replace_rolls_back_with_durable_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    old_bytes = b"old-output"
    target.write_bytes(old_bytes)
    candidate = _validated_candidate(publisher, target, tmp_path, "sync-rollback")
    events: list[str] = []
    phase = "prepare"
    target_failed = False
    actual_replace = render_module.PublicationDirectory.replace_from
    actual_source_sync = render_module.RecoveryDirectory.fsync
    actual_target_sync = render_module.PublicationDirectory.fsync

    def observe_replace(self, source_directory, source, destination):
        nonlocal phase
        result = actual_replace(self, source_directory, source, destination)
        phase = "rollback" if ".restore-" in source else "commit"
        events.append(phase)
        return result

    def observe_source_sync(self):
        if phase != "prepare":
            events.append(f"{phase}-source-sync")
        return actual_source_sync(self)

    def fail_target_sync_once(self):
        nonlocal target_failed
        if phase == "commit" and not target_failed:
            target_failed = True
            events.append("commit-target-sync-failed")
            raise OSError(errno.EIO, "synthetic target directory flush failure")
        if phase != "prepare":
            events.append(f"{phase}-target-sync")
        return actual_target_sync(self)

    monkeypatch.setattr(
        render_module.PublicationDirectory, "replace_from", observe_replace
    )
    monkeypatch.setattr(render_module.RecoveryDirectory, "fsync", observe_source_sync)
    monkeypatch.setattr(
        render_module.PublicationDirectory, "fsync", fail_target_sync_once
    )

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(candidate, target, CollisionPolicy.REPLACE)
    finally:
        candidate.close()

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert target.read_bytes() == old_bytes
    assert events == [
        "commit",
        "commit-source-sync",
        "commit-target-sync-failed",
        "commit-source-sync",
        "rollback",
        "rollback-source-sync",
        "rollback-target-sync",
    ]


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
    assert not (tmp_path / ".rembggui-work" / "scratch" / "impossible").exists()


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

    actual_replace = render_module.PublicationDirectory.replace_from
    failed_once = False

    def fail_replace(self, source_directory, source, destination):
        nonlocal failed_once
        if destination == target.name and not failed_once:
            failed_once = True
            raise OSError(error_number, "synthetic publication failure")
        return actual_replace(self, source_directory, source, destination)

    monkeypatch.setattr(
        render_module.PublicationDirectory, "replace_from", fail_replace
    )

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
    actual_rename = render_module._rename_no_replace

    def create_racer_then_rename(
        source_directory,
        source,
        destination_directory,
        destination,
    ):
        destination_directory.path_for(destination).write_bytes(b"racer")
        actual_rename(
            source_directory,
            source,
            destination_directory,
            destination,
        )

    monkeypatch.setattr(render_module, "_rename_no_replace", create_racer_then_rename)

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(candidate, target, policy)
    finally:
        candidate.close()

    assert exc.value.stage == "publish"
    assert target.read_bytes() == b"racer"
    assert candidate.path.read_bytes() == candidate_bytes
    retained_stages = tuple((tmp_path / ".rembggui-publish").glob("*.publish"))
    assert len(retained_stages) == 1
    assert retained_stages[0].read_bytes() == candidate_bytes


def test_repeated_no_clobber_collisions_reuse_one_bounded_private_stage(
    tmp_path,
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    target.write_bytes(b"existing-output")

    for label in ("first-collision", "second-collision"):
        candidate = _validated_candidate(publisher, target, tmp_path, label)
        try:
            with pytest.raises(AppError):
                publisher.publish(
                    candidate,
                    target,
                    CollisionPolicy.CHOOSE_ANOTHER_NAME,
                )
        finally:
            candidate.close()

    assert target.read_bytes() == b"existing-output"
    assert len(tuple((tmp_path / ".rembggui-publish").glob("*.publish"))) == 1


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
    assert candidate.path.read_bytes() == candidate_bytes


def test_candidate_validation_uses_the_held_file_not_a_swappable_helper_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    valid = _validated_candidate(publisher, target, tmp_path, "valid-source")
    valid.close()
    valid_path = valid.path
    valid_bytes = valid_path.read_bytes()
    invalid_work = tmp_path / "invalid-source"
    invalid_work.mkdir()
    invalid_path = publisher.candidate_path(target, "invalid-source", invalid_work)
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
    actual_replace_from = render_module.PublicationDirectory.replace_from
    actual_replace = os.replace
    swapped = False

    def swap_candidate_then_replace(self, source_directory, source, destination):
        nonlocal swapped
        result = actual_replace_from(self, source_directory, source, destination)
        if destination == target.name and not swapped:
            actual_replace(attacker, target)
            swapped = True
        return result

    monkeypatch.setattr(
        render_module.PublicationDirectory,
        "replace_from",
        swap_candidate_then_replace,
    )

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(candidate, target, CollisionPolicy.REPLACE)
    finally:
        candidate.close()

    assert swapped
    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert target.read_bytes() == b"old-output"


def test_replace_rejects_a_destination_swap_during_held_sha_verification(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    old_bytes = b"old-output"
    target.write_bytes(old_bytes)
    candidate = _validated_candidate(publisher, target, tmp_path, "sha-swap-replace")
    foreign = tmp_path / ".foreign-after-sha"
    foreign.write_bytes(b"foreign-after-sha")
    actual_sha = render_module._sha256_descriptor
    actual_replace = os.replace
    actual_prepare = render_module._prepare_publication_stage
    swapped = False
    staged_identity = None

    def capture_stage(*args, **kwargs):
        nonlocal staged_identity
        staged = actual_prepare(*args, **kwargs)
        staged_identity = staged.identity
        return staged

    def swap_destination_after_hash(descriptor: int) -> str:
        nonlocal swapped
        digest = actual_sha(descriptor)
        if (
            not swapped
            and descriptor != candidate._descriptor
            and target.exists()
            and staged_identity is not None
            and os.fstat(descriptor).st_ino == staged_identity.inode
        ):
            actual_replace(foreign, target)
            swapped = True
        return digest

    monkeypatch.setattr(
        render_module, "_sha256_descriptor", swap_destination_after_hash
    )
    monkeypatch.setattr(render_module, "_prepare_publication_stage", capture_stage)

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(candidate, target, CollisionPolicy.REPLACE)
    finally:
        candidate.close()

    assert swapped
    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert target.read_bytes() == old_bytes


def test_replace_without_previous_output_never_unlinks_a_foreign_commit_mismatch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    candidate = _validated_candidate(publisher, target, tmp_path, "absent-rollback")
    foreign = tmp_path / ".foreign-commit"
    foreign_bytes = b"foreign-commit"
    foreign.write_bytes(foreign_bytes)
    actual_replace = os.replace
    actual_replace_from = render_module.PublicationDirectory.replace_from
    swapped = False

    def swap_candidate_at_commit(self, source_directory, source, destination):
        nonlocal swapped
        result = actual_replace_from(self, source_directory, source, destination)
        if destination == target.name:
            actual_replace(foreign, target)
            swapped = True
        return result

    monkeypatch.setattr(
        render_module.PublicationDirectory, "replace_from", swap_candidate_at_commit
    )

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(candidate, target, CollisionPolicy.REPLACE)
    finally:
        candidate.close()

    assert swapped
    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert target.read_bytes() == foreign_bytes


@pytest.mark.parametrize("failure_errno", [errno.ENOSPC, errno.EACCES])
def test_replace_retains_precommit_recovery_when_restore_cannot_allocate(
    tmp_path, monkeypatch: pytest.MonkeyPatch, failure_errno: int
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    old_bytes = b"old-output-needing-durable-recovery"
    target.write_bytes(old_bytes)
    candidate = _validated_candidate(publisher, target, tmp_path, "recovery-enospc")
    foreign = tmp_path / ".foreign-commit"
    foreign.write_bytes(b"foreign-commit")
    actual_replace = os.replace
    actual_replace_from = render_module.PublicationDirectory.replace_from
    actual_stage = render_module._stage_bound_recovery_copy

    def swap_candidate_at_commit(self, source_directory, source, destination):
        result = actual_replace_from(self, source_directory, source, destination)
        if destination == target.name:
            actual_replace(foreign, target)
        return result

    def fail_restore_copy(directory, name, *args, **kwargs):
        if ".restore-" in name:
            raise OSError(failure_errno, "synthetic restore allocation failure")
        return actual_stage(directory, name, *args, **kwargs)

    monkeypatch.setattr(
        render_module.PublicationDirectory, "replace_from", swap_candidate_at_commit
    )
    monkeypatch.setattr(
        render_module,
        "_stage_bound_recovery_copy",
        fail_restore_copy,
    )

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(candidate, target, CollisionPolicy.REPLACE)
    finally:
        candidate.close()

    recovery_files = tuple((tmp_path / ".rembggui-recovery").glob("*.recovery"))
    assert exc.value.stage == "publish-rollback"
    assert exc.value.retry_action == "recover-output"
    assert len(recovery_files) == 1
    assert recovery_files[0].read_bytes() == old_bytes
    assert str(recovery_files[0]) in exc.value.technical_detail


@pytest.mark.parametrize(
    ("failure_errno", "retry_action"),
    [
        (errno.ENOSPC, "free-disk-space"),
        (errno.EACCES, "choose-writable-output"),
    ],
)
def test_recovery_preparation_failure_aborts_before_destructive_replace(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    failure_errno: int,
    retry_action: str,
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    old_bytes = b"old-output"
    target.write_bytes(old_bytes)
    candidate = _validated_candidate(publisher, target, tmp_path, "recovery-prepare")
    actual_stage = render_module._stage_bound_recovery_copy
    candidate_commit_attempted = False

    def fail_recovery_link(source_path, destination_path, **kwargs):
        del source_path, destination_path, kwargs
        raise OSError(failure_errno, "synthetic recovery link failure")

    def fail_recovery_copy(directory, name, *args, **kwargs):
        if name.endswith(".recovery-pending"):
            raise OSError(failure_errno, "synthetic recovery copy failure")
        return actual_stage(directory, name, *args, **kwargs)

    actual_replace = os.replace

    def observe_replace(source_path, destination_path, **kwargs):
        nonlocal candidate_commit_attempted
        if (
            Path(source_path).name == candidate.path.name
            and Path(destination_path).name == target.name
        ):
            candidate_commit_attempted = True
        return actual_replace(source_path, destination_path, **kwargs)

    monkeypatch.setattr(os, "link", fail_recovery_link)
    monkeypatch.setattr(
        render_module,
        "_stage_bound_recovery_copy",
        fail_recovery_copy,
    )
    monkeypatch.setattr(os, "replace", observe_replace)

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(candidate, target, CollisionPolicy.REPLACE)
    finally:
        candidate.close()

    assert exc.value.retry_action == retry_action
    assert not candidate_commit_attempted
    assert target.read_bytes() == old_bytes


def test_hard_linked_recovery_file_is_fsynced_before_destructive_replace(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    target.write_bytes(b"old-output")
    old_inode = target.stat().st_ino
    candidate = _validated_candidate(publisher, target, tmp_path, "recovery-file-fsync")
    actual_fsync = os.fsync
    actual_replace_from = render_module.PublicationDirectory.replace_from
    regular_fsync_inodes: list[int] = []
    events: list[str] = []

    def observe_fsync(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if stat.S_ISREG(info.st_mode):
            regular_fsync_inodes.append(info.st_ino)
            if info.st_ino == old_inode:
                events.append("recovery-file-fsync")
        actual_fsync(descriptor)

    def observe_replace(self, source_directory, source, destination):
        if destination == target.name:
            events.append("destructive-commit")
        return actual_replace_from(self, source_directory, source, destination)

    monkeypatch.setattr(os, "fsync", observe_fsync)
    monkeypatch.setattr(
        render_module.PublicationDirectory, "replace_from", observe_replace
    )

    try:
        publisher.publish(candidate, target, CollisionPolicy.REPLACE)
    finally:
        candidate.close()

    assert old_inode in regular_fsync_inodes
    assert events.index("recovery-file-fsync") < events.index("destructive-commit")


def test_recovery_directory_fsync_failure_leaves_no_pending_after_next_success(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    old_bytes = b"old-output"
    target.write_bytes(old_bytes)
    actual_fsync_directory = render_module._fsync_directory
    failed_once = False

    def fail_first_recovery_directory_fsync(directory: Path) -> None:
        nonlocal failed_once
        if directory.name == ".rembggui-recovery" and not failed_once:
            failed_once = True
            raise OSError(errno.EIO, "synthetic recovery-directory fsync failure")
        actual_fsync_directory(directory)

    monkeypatch.setattr(
        render_module,
        "_fsync_directory",
        fail_first_recovery_directory_fsync,
    )

    first = _validated_candidate(publisher, target, tmp_path, "recovery-fsync-first")
    try:
        with pytest.raises(AppError):
            publisher.publish(first, target, CollisionPolicy.REPLACE)
    finally:
        first.close()

    assert failed_once
    assert target.read_bytes() == old_bytes
    recovery_directory = tmp_path / ".rembggui-recovery"
    assert tuple(recovery_directory.glob("*.recovery-pending"))

    second = _validated_candidate(publisher, target, tmp_path, "recovery-fsync-second")
    try:
        publisher.publish(second, target, CollisionPolicy.REPLACE)
    finally:
        second.close()

    assert not tuple(recovery_directory.glob("*.recovery-pending"))


def test_recovery_namespace_swap_never_modifies_the_foreign_replacement(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    old_bytes = b"old-output"
    target.write_bytes(old_bytes)
    recovery_directory = tmp_path / ".rembggui-recovery"
    recovery_directory.mkdir(mode=0o700)
    foreign_directory = tmp_path / ".foreign-recovery-directory"
    foreign_directory.mkdir(mode=0o700)
    sentinel = foreign_directory / "sentinel"
    sentinel_bytes = b"foreign-namespace-content"
    sentinel.write_bytes(sentinel_bytes)
    moved_recovery = tmp_path / ".original-recovery-directory"
    candidate = _validated_candidate(publisher, target, tmp_path, "namespace-swap")
    actual_link = os.link
    actual_replace = os.replace
    swapped = False

    def swap_namespace_then_link(source, destination, **kwargs):
        nonlocal swapped
        if not swapped:
            actual_replace(recovery_directory, moved_recovery)
            actual_replace(foreign_directory, recovery_directory)
            swapped = True
        return actual_link(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", swap_namespace_then_link)

    try:
        with pytest.raises(AppError):
            publisher.publish(candidate, target, CollisionPolicy.REPLACE)
    finally:
        candidate.close()

    assert swapped
    assert target.read_bytes() == old_bytes
    assert tuple(path.name for path in recovery_directory.iterdir()) == ("sentinel",)
    assert (recovery_directory / "sentinel").read_bytes() == sentinel_bytes


def test_replace_parent_swap_restores_original_and_never_modifies_foreign_parent(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_parent = tmp_path / "output-parent"
    output_parent.mkdir()
    target = output_parent / "output.webp"
    old_bytes = b"old-output"
    target.write_bytes(old_bytes)
    publisher = AtomicOutputPublisher()
    candidate = _validated_candidate(
        publisher,
        target,
        output_parent,
        "parent-swap-replace",
    )
    foreign_parent = tmp_path / "foreign-parent"
    foreign_parent.mkdir()
    foreign_output_bytes = b"foreign-output"
    (foreign_parent / target.name).write_bytes(foreign_output_bytes)
    moved_parent = tmp_path / "moved-output-parent"
    actual_replace = os.replace
    actual_replace_from = render_module.PublicationDirectory.replace_from
    swapped = False

    def swap_parent_at_candidate_commit(self, source_directory, source, destination):
        nonlocal swapped
        if not swapped and destination == target.name:
            actual_replace(output_parent, moved_parent)
            actual_replace(foreign_parent, output_parent)
            swapped = True
        return actual_replace_from(self, source_directory, source, destination)

    monkeypatch.setattr(
        render_module.PublicationDirectory,
        "replace_from",
        swap_parent_at_candidate_commit,
    )

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(candidate, target, CollisionPolicy.REPLACE)
    finally:
        candidate.close()

    assert swapped
    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert (output_parent / target.name).read_bytes() == foreign_output_bytes
    assert (moved_parent / target.name).read_bytes() == old_bytes


def test_no_clobber_parent_swap_never_consumes_foreign_stage_or_creates_output(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_parent = tmp_path / "output-parent"
    output_parent.mkdir()
    target = output_parent / "output.webp"
    publisher = AtomicOutputPublisher()
    candidate = _validated_candidate(
        publisher,
        target,
        output_parent,
        "parent-swap-no-clobber",
    )
    foreign_parent = tmp_path / "foreign-parent"
    foreign_parent.mkdir()
    moved_parent = tmp_path / "moved-output-parent"
    foreign_stage_bytes = b"foreign-private-stage"
    actual_rename = render_module._rename_no_replace
    actual_replace = os.replace
    swapped = False
    foreign_stage_name: str | None = None

    def swap_parent_at_no_clobber_commit(*args):
        nonlocal foreign_stage_name, swapped
        original_stages = tuple((output_parent / ".rembggui-publish").glob("*.publish"))
        assert len(original_stages) == 1
        foreign_stage_name = original_stages[0].name
        foreign_stages = foreign_parent / ".rembggui-publish"
        foreign_stages.mkdir(mode=0o700)
        (foreign_stages / foreign_stage_name).write_bytes(foreign_stage_bytes)
        actual_replace(output_parent, moved_parent)
        actual_replace(foreign_parent, output_parent)
        swapped = True
        return actual_rename(*args)

    monkeypatch.setattr(
        render_module,
        "_rename_no_replace",
        swap_parent_at_no_clobber_commit,
    )

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(
                candidate,
                target,
                CollisionPolicy.CHOOSE_ANOTHER_NAME,
            )
    finally:
        candidate.close()

    assert swapped
    assert foreign_stage_name is not None
    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert not (output_parent / target.name).exists()
    assert (
        output_parent / ".rembggui-publish" / foreign_stage_name
    ).read_bytes() == foreign_stage_bytes


def test_unsafe_recovery_namespace_is_reported_as_an_output_error(tmp_path) -> None:
    target = tmp_path / "output.webp"
    old_bytes = b"old-output"
    target.write_bytes(old_bytes)
    recovery_directory = tmp_path / ".rembggui-recovery"
    recovery_directory.mkdir(mode=0o755)
    publisher = AtomicOutputPublisher()
    candidate = _validated_candidate(
        publisher,
        target,
        tmp_path,
        "unsafe-recovery-error",
    )

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(candidate, target, CollisionPolicy.REPLACE)
    finally:
        candidate.close()

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert exc.value.stage == "output"
    assert exc.value.retry_action == "retry-output"
    assert target.read_bytes() == old_bytes


def test_recovery_descriptor_closes_when_shadow_preparation_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    old_bytes = b"old-output"
    target.write_bytes(old_bytes)
    candidate = _validated_candidate(publisher, target, tmp_path, "shadow-failure")
    recovery_descriptors: list[int] = []

    def fail_shadow(recovery) -> None:
        recovery_descriptors.append(recovery.descriptor)
        raise RuntimeError("synthetic shadow preparation failure")

    monkeypatch.setattr(render_module, "_prepare_recovery_shadow", fail_shadow)

    try:
        with pytest.raises(RuntimeError, match="synthetic shadow preparation failure"):
            publisher.publish(candidate, target, CollisionPolicy.REPLACE)
    finally:
        candidate.close()

    assert target.read_bytes() == old_bytes
    assert recovery_descriptors
    for descriptor in recovery_descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_current_recovery_slot_is_reused_without_a_pending_hardlink(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    old_bytes = b"old-output"
    target.write_bytes(old_bytes)
    first = _validated_candidate(publisher, target, tmp_path, "recovery-reuse-first")
    actual_prepare = render_module._prepare_publication_stage
    failed_once = False

    def fail_after_recovery(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise AppError(
                ErrorCode.INVALID_OUTPUT,
                "output",
                "error.output.failed",
                "synthetic post-recovery failure",
                "retry-output",
            )
        return actual_prepare(*args, **kwargs)

    monkeypatch.setattr(
        render_module,
        "_prepare_publication_stage",
        fail_after_recovery,
    )
    try:
        with pytest.raises(AppError):
            publisher.publish(first, target, CollisionPolicy.REPLACE)
    finally:
        first.close()

    assert target.read_bytes() == old_bytes
    recovery_directory = tmp_path / ".rembggui-recovery"
    recovery = tuple(recovery_directory.glob("*.recovery"))
    assert len(recovery) == 1
    assert recovery[0].read_bytes() == old_bytes

    monkeypatch.setattr(
        render_module,
        "_prepare_publication_stage",
        actual_prepare,
    )
    second = _validated_candidate(publisher, target, tmp_path, "recovery-reuse-second")
    try:
        publisher.publish(second, target, CollisionPolicy.REPLACE)
    finally:
        second.close()

    assert not tuple(recovery_directory.glob("*.recovery-pending"))


def test_replace_retries_when_restored_output_is_swapped_during_held_sha(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    old_bytes = b"old-output"
    target.write_bytes(old_bytes)
    candidate = _validated_candidate(publisher, target, tmp_path, "restore-sha-swap")
    commit_attacker = tmp_path / ".commit-attacker"
    commit_attacker.write_bytes(b"invalid-commit")
    restore_attacker = tmp_path / ".restore-attacker"
    restore_attacker.write_bytes(b"restore-attacker")
    actual_replace = os.replace
    actual_replace_from = render_module.PublicationDirectory.replace_from
    actual_sha = render_module._sha256_descriptor
    commit_swapped = False
    restore_swapped = False

    def swap_commit(self, source_directory, source, destination):
        nonlocal commit_swapped
        result = actual_replace_from(self, source_directory, source, destination)
        if not commit_swapped and destination == target.name:
            actual_replace(commit_attacker, target)
            commit_swapped = True
        return result

    def swap_restored_destination_after_hash(descriptor: int) -> str:
        nonlocal restore_swapped
        digest = actual_sha(descriptor)
        descriptor_stat = os.fstat(descriptor)
        if (
            commit_swapped
            and not restore_swapped
            and target.exists()
            and descriptor_stat.st_size == len(old_bytes)
            and target.stat().st_ino == descriptor_stat.st_ino
        ):
            actual_replace(restore_attacker, target)
            restore_swapped = True
        return digest

    monkeypatch.setattr(render_module.PublicationDirectory, "replace_from", swap_commit)
    monkeypatch.setattr(
        render_module,
        "_sha256_descriptor",
        swap_restored_destination_after_hash,
    )

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(candidate, target, CollisionPolicy.REPLACE)
    finally:
        candidate.close()

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert commit_swapped
    assert restore_swapped
    assert target.read_bytes() == old_bytes


def test_rollback_reports_when_recovery_path_is_swapped_during_held_sha(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    target.write_bytes(b"old-output")
    candidate = _validated_candidate(publisher, target, tmp_path, "recovery-sha-swap")
    commit_attacker = tmp_path / ".commit-attacker"
    commit_attacker.write_bytes(b"invalid-commit")
    recovery_attacker = tmp_path / ".recovery-attacker"
    recovery_attacker_bytes = b"foreign-recovery"
    recovery_attacker.write_bytes(recovery_attacker_bytes)
    actual_prepare = render_module._prepare_durable_recovery
    actual_stage = render_module._stage_bound_recovery_copy
    actual_replace = os.replace
    actual_replace_from = render_module.PublicationDirectory.replace_from
    actual_sha = render_module._sha256_descriptor
    recovery = None
    recovery_ready = False
    recovery_swapped = False

    def capture_recovery(*args, **kwargs):
        nonlocal recovery, recovery_ready
        recovery = actual_prepare(*args, **kwargs)
        recovery_ready = True
        return recovery

    def fail_restore_copy(directory, name, *args, **kwargs):
        if ".restore-" in name:
            raise OSError(errno.ENOSPC, "synthetic restore ENOSPC")
        return actual_stage(directory, name, *args, **kwargs)

    def swap_candidate_at_commit(self, source_directory, source, destination):
        result = actual_replace_from(self, source_directory, source, destination)
        if destination == target.name and not recovery_ready:
            raise AssertionError("recovery must exist before publication")
        if destination == target.name and source.endswith(".publish"):
            actual_replace(commit_attacker, target)
        return result

    def swap_recovery_after_hash(descriptor: int) -> str:
        nonlocal recovery_swapped
        digest = actual_sha(descriptor)
        if (
            recovery_ready
            and not recovery_swapped
            and recovery is not None
            and descriptor == recovery.descriptor
        ):
            actual_replace(recovery_attacker, recovery.path)
            recovery_swapped = True
        return digest

    monkeypatch.setattr(
        render_module,
        "_prepare_durable_recovery",
        capture_recovery,
    )
    monkeypatch.setattr(
        render_module,
        "_stage_bound_recovery_copy",
        fail_restore_copy,
    )
    monkeypatch.setattr(
        render_module.PublicationDirectory, "replace_from", swap_candidate_at_commit
    )
    monkeypatch.setattr(render_module, "_sha256_descriptor", swap_recovery_after_hash)

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(candidate, target, CollisionPolicy.REPLACE)
    finally:
        candidate.close()

    assert recovery is not None
    assert recovery_swapped
    assert exc.value.stage == "publish-rollback"
    assert recovery.path.read_bytes() == recovery_attacker_bytes
    recovery_shadow = recovery.path.with_suffix(".recovery-shadow")
    assert recovery_shadow.read_bytes() == b"old-output"
    assert str(recovery_shadow) in exc.value.technical_detail


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
    actual_replace_from = render_module.PublicationDirectory.replace_from
    actual_stage = render_module._stage_bound_recovery_copy
    actual_close = os.close
    publisher_descriptors: list[int] = []
    closed_descriptors: list[int] = []
    candidate_swapped = False
    rollback_swap_count = 0

    def observe_stage(directory, name, *args, **kwargs):
        descriptor, identity = actual_stage(directory, name, *args, **kwargs)
        if ".restore-" in name:
            publisher_descriptors.append(descriptor)
        return descriptor, identity

    def observe_close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        actual_close(descriptor)

    def swap_publication_and_rollback(self, source_directory, source, destination):
        nonlocal candidate_swapped, rollback_swap_count
        if source.endswith(".publish") and destination == target.name:
            result = actual_replace_from(self, source_directory, source, destination)
            actual_replace(candidate_attacker, target)
            candidate_swapped = True
            return result
        if (
            destination == target.name
            and ".restore-" in source
            and rollback_swap_count < rollback_swaps
        ):
            actual_replace(
                rollback_attackers[rollback_swap_count],
                source_directory.path_for(source),
            )
            rollback_swap_count += 1
        return actual_replace_from(self, source_directory, source, destination)

    monkeypatch.setattr(
        render_module,
        "_stage_bound_recovery_copy",
        observe_stage,
    )
    monkeypatch.setattr(render_module.os, "close", observe_close)
    monkeypatch.setattr(
        render_module.PublicationDirectory,
        "replace_from",
        swap_publication_and_rollback,
    )

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
    actual_rename = render_module._rename_no_replace
    swapped = False
    notes: list[str] = []

    def swap_candidate_then_rename(
        source_directory,
        source,
        destination_directory,
        destination,
    ):
        nonlocal swapped
        os.replace(attacker, candidate.path)
        swapped = True
        return actual_rename(
            source_directory,
            source,
            destination_directory,
            destination,
        )

    monkeypatch.setattr(
        render_module,
        "_rename_no_replace",
        swap_candidate_then_rename,
    )

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
    assert notes == []
    assert not tuple((tmp_path / ".rembggui-publish").glob("*.publish"))


def test_no_clobber_never_unlinks_a_concurrent_output_after_reservation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    candidate = _validated_candidate(publisher, target, tmp_path, "foreign-writer")
    foreign = tmp_path / ".foreign-writer"
    foreign_bytes = b"concurrent-writer-output"
    foreign.write_bytes(foreign_bytes)
    actual_rename = render_module._rename_no_replace
    actual_replace = os.replace
    actual_prepare = render_module._prepare_publication_stage
    staged_descriptors: list[int] = []

    def observe_stage(*args, **kwargs):
        staged = actual_prepare(*args, **kwargs)
        staged_descriptors.append(staged.descriptor)
        return staged

    def replace_reserved_output(
        source_directory,
        source,
        destination_directory,
        destination,
    ):
        result = actual_rename(
            source_directory,
            source,
            destination_directory,
            destination,
        )
        actual_replace(foreign, target)
        return result

    monkeypatch.setattr(render_module, "_rename_no_replace", replace_reserved_output)
    monkeypatch.setattr(render_module, "_prepare_publication_stage", observe_stage)

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
    assert not tuple((tmp_path / ".rembggui-publish").glob("*.publish"))


def test_no_clobber_rejects_a_destination_swap_during_held_sha_verification(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    candidate = _validated_candidate(publisher, target, tmp_path, "sha-swap-link")
    foreign = tmp_path / ".foreign-after-sha"
    foreign_bytes = b"foreign-after-sha"
    foreign.write_bytes(foreign_bytes)
    actual_sha = render_module._sha256_descriptor
    actual_replace = os.replace
    swapped = False

    def swap_destination_after_hash(descriptor: int) -> str:
        nonlocal swapped
        digest = actual_sha(descriptor)
        if (
            not swapped
            and descriptor != candidate._descriptor
            and target.exists()
            and os.fstat(descriptor).st_ino != candidate.identity.inode
        ):
            actual_replace(foreign, target)
            swapped = True
        return digest

    monkeypatch.setattr(
        render_module, "_sha256_descriptor", swap_destination_after_hash
    )

    try:
        with pytest.raises(AppError) as exc:
            publisher.publish(
                candidate,
                target,
                CollisionPolicy.CHOOSE_ANOTHER_NAME,
            )
    finally:
        candidate.close()

    assert swapped
    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert target.read_bytes() == foreign_bytes


def test_no_clobber_success_never_unlinks_a_staged_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    candidate = _validated_candidate(publisher, target, tmp_path, "consume-stage")
    actual_unlink = Path.unlink
    attempted_stage_unlink = False

    def reject_stage_unlink(path: Path, *args, **kwargs):
        nonlocal attempted_stage_unlink
        if path.suffix == ".publish":
            attempted_stage_unlink = True
            raise AssertionError("staged publication must be consumed, not unlinked")
        return actual_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", reject_stage_unlink)

    try:
        assert (
            publisher.publish(
                candidate,
                target,
                CollisionPolicy.CHOOSE_ANOTHER_NAME,
            )
            == target
        )
    finally:
        candidate.close()

    assert not attempted_stage_unlink


def test_publisher_never_reopens_the_caller_owned_candidate_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    candidate = _validated_candidate(publisher, target, tmp_path, "cleanup-swap")
    candidate_bytes = candidate.path.read_bytes()
    notes: list[str] = []

    def reject_path_reopen(_path: Path):
        raise AssertionError("post-publication candidate paths are diagnostic-only")

    monkeypatch.setattr(render_module, "_path_identity", reject_path_reopen)

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
    assert notes == []


def test_no_clobber_leaves_caller_owned_candidate_unchanged_without_note(
    tmp_path,
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    candidate = _validated_candidate(publisher, target, tmp_path, "cleanup")
    candidate_bytes = candidate.path.read_bytes()
    notes: list[str] = []

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
    assert notes == []
    assert not tuple((tmp_path / ".rembggui-publish").glob("*.publish"))


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
