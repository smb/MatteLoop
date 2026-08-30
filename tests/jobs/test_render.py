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
from rembggui.jobs.context import JobTerminalState
from rembggui.jobs.render import (
    AtomicOutputPublisher,
    FilesystemWorkspacePort,
    PillowWebPEncoder,
    PreparedSegmentation,
    RenderService,
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
    assert artifact.output_path.read_bytes() == b"validated-webp-candidate"


def test_render_encodes_and_validates_a_real_lossless_webp(tmp_path) -> None:
    from rembggui.core.webp import validate_webp

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
    candidate = tmp_path / ".candidate"
    target = tmp_path / "output.webp"
    candidate.write_bytes(b"new")
    target.write_bytes(b"old")

    def fail_replace(_source, _target):
        raise OSError(error_number, "synthetic publication failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(AppError) as exc:
        AtomicOutputPublisher().publish(candidate, target, CollisionPolicy.REPLACE)

    assert exc.value.retry_action == retry_action
    assert target.read_bytes() == b"old"
    assert candidate.read_bytes() == b"new"


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
    candidate = publisher.candidate_path(target, "job")
    candidate.write_bytes(b"candidate")
    actual_link = os.link

    def create_racer_then_link(source, destination):
        Path(destination).write_bytes(b"racer")
        actual_link(source, destination)

    monkeypatch.setattr(os, "link", create_racer_then_link)

    with pytest.raises(AppError) as exc:
        publisher.publish(candidate, target, policy)

    assert exc.value.stage == "publish"
    assert target.read_bytes() == b"racer"
    assert candidate.read_bytes() == b"candidate"


def test_replace_policy_commits_candidate_atomically(tmp_path) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    candidate = publisher.candidate_path(target, "job")
    target.write_bytes(b"old")
    candidate.write_bytes(b"new")

    assert publisher.publish(candidate, target, CollisionPolicy.REPLACE) == target
    assert target.read_bytes() == b"new"
    assert not candidate.exists()


def test_no_clobber_candidate_cleanup_failure_is_returned_as_a_note(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = AtomicOutputPublisher()
    target = tmp_path / "output.webp"
    candidate = publisher.candidate_path(target, "job")
    candidate.write_bytes(b"new")
    notes: list[str] = []
    actual_unlink = Path.unlink

    def fail_candidate_unlink(path: Path, *args, **kwargs):
        if path == candidate:
            raise OSError(errno.EACCES, "synthetic candidate cleanup failure")
        return actual_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_candidate_unlink)

    assert (
        publisher.publish(
            candidate,
            target,
            CollisionPolicy.CHOOSE_ANOTHER_NAME,
            cleanup_notes=notes,
        )
        == target
    )
    assert target.read_bytes() == b"new"
    assert candidate.read_bytes() == b"new"
    assert notes == [
        "additional output-candidate cleanup failure: "
        "[Errno 13] synthetic candidate cleanup failure"
    ]


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
