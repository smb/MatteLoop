from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from PIL import Image

from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.specs import FramingSpec
from rembggui.core.state import JobKind
from rembggui.jobs.render import (
    AtomicOutputPublisher,
    FilesystemWorkspacePort,
    PreparedSegmentation,
    RenderService,
)
from tests.jobs.render_support import (
    ExplodingSource,
    FakeClock,
    FakeDiskProbe,
    FakeEncoder,
    FakeSegmenter,
    FakeSource,
    binding,
    job,
    render_service,
    request,
)


def test_rebuild_never_probes_hashes_decodes_or_segments_source(tmp_path) -> None:
    workspace = FilesystemWorkspacePort()
    segmenter = FakeSegmenter()
    binding = PreparedSegmentation(
        segmenter,
        "birefnet-portrait",
        "ab" * 32,
        "2.0.72",
        frozenset({"standard"}),
    )
    first = RenderService(
        source=FakeSource(),
        segmentation=binding,
        workspace=workspace,
        encoder=FakeEncoder(),
        disk_probe=FakeDiskProbe(),
        clock=FakeClock(),
        output_publisher=AtomicOutputPublisher(),
    )
    render_request = request(tmp_path)
    original = first.render(render_request, job(tmp_path, "seed-cuts", JobKind.RENDER))
    calls_before = len(segmenter.calls)
    render_request.source.unlink()
    rebuild_encoder = FakeEncoder()
    rebuild = RenderService(
        source=ExplodingSource(),
        segmentation=binding,
        workspace=workspace,
        encoder=rebuild_encoder,
        disk_probe=FakeDiskProbe(),
        clock=FakeClock(),
        output_publisher=AtomicOutputPublisher(),
    )

    artifact = rebuild.rebuild(
        replace(render_request, rebuild=True),
        original.cut_workspace,
        job(tmp_path, "rebuild-only", JobKind.REBUILD),
    )

    assert len(segmenter.calls) == calls_before
    assert artifact.frame_count == original.frame_count
    assert original.actual_pts == original.requested_timestamps
    assert artifact.actual_pts is None
    assert all(
        "scratch/rebuild-only" in path.as_posix()
        for path in rebuild_encoder.calls[0][0]
    )


def test_edit_after_snapshot_affects_next_rebuild_only(tmp_path) -> None:
    workspace = FilesystemWorkspacePort()
    segmenter = FakeSegmenter()
    prepared = binding(segmenter)
    render_request = request(tmp_path)
    original = render_service(segmenter=segmenter, workspace=workspace).render(
        render_request, job(tmp_path, "seed-edit", JobKind.RENDER)
    )

    class PixelEncoder(FakeEncoder):
        def __init__(self) -> None:
            super().__init__()
            self.first_pixels: list[tuple[int, int, int, int]] = []

        def encode(self, frame_paths, delays_ms, destination, **kwargs):
            with Image.open(frame_paths[0]) as image:
                self.first_pixels.append(image.getpixel((0, 0)))
            return super().encode(frame_paths, delays_ms, destination, **kwargs)

    class EditAfterSnapshotWorkspace(FilesystemWorkspacePort):
        def snapshot_rebuild(self, durable, scratch_directory, context):
            snapshot = super().snapshot_rebuild(durable, scratch_directory, context)
            with Image.new("RGBA", (128, 128), (5, 200, 80, 255)) as edited:
                edited.save(durable.path / "frame-000000.png")
            return snapshot

    first_encoder = PixelEncoder()
    first = RenderService(
        source=ExplodingSource(),
        segmentation=prepared,
        workspace=EditAfterSnapshotWorkspace(),
        encoder=first_encoder,
        disk_probe=FakeDiskProbe(),
        clock=FakeClock(),
        output_publisher=AtomicOutputPublisher(),
    )
    rebuild_request = replace(render_request, rebuild=True)

    first.rebuild(
        rebuild_request,
        original.cut_workspace,
        job(tmp_path, "snapshot-before-edit", JobKind.REBUILD),
    )
    second_encoder = PixelEncoder()
    second = RenderService(
        source=ExplodingSource(),
        segmentation=prepared,
        workspace=workspace,
        encoder=second_encoder,
        disk_probe=FakeDiskProbe(),
        clock=FakeClock(),
        output_publisher=AtomicOutputPublisher(),
    )
    second.rebuild(
        rebuild_request,
        original.cut_workspace,
        job(tmp_path, "next-sees-edit", JobKind.REBUILD),
    )

    assert first_encoder.first_pixels == [(0, 0, 0, 0)]
    assert second_encoder.first_pixels == [(5, 200, 80, 255)]


def test_snapshot_save_race_passes_structured_failure_without_source_io(
    tmp_path,
) -> None:
    render_request = request(tmp_path)
    original = render_service().render(
        render_request, job(tmp_path, "seed-race", JobKind.RENDER)
    )
    render_request.source.unlink()
    expected = AppError(
        ErrorCode.CUTS_CHANGED_DURING_SNAPSHOT,
        "cut-snapshot",
        "error.cuts.changed-during-snapshot",
        "synthetic editor save during snapshot",
        "retry-rebuild",
    )

    class RacingWorkspace(FilesystemWorkspacePort):
        def snapshot_rebuild(self, workspace, scratch_directory, context):
            del workspace, scratch_directory, context
            raise expected

    service = RenderService(
        source=ExplodingSource(),
        segmentation=binding(FakeSegmenter()),
        workspace=RacingWorkspace(),
        encoder=FakeEncoder(),
        disk_probe=FakeDiskProbe(),
        clock=FakeClock(),
        output_publisher=AtomicOutputPublisher(),
    )

    with pytest.raises(AppError) as exc:
        service.rebuild(
            replace(render_request, rebuild=True),
            original.cut_workspace,
            job(tmp_path, "save-race", JobKind.REBUILD),
        )

    assert exc.value is expected


def test_union_invalidation_recomputes_from_snapshot_and_cas_loss_is_nonfatal(
    tmp_path,
) -> None:
    workspace = FilesystemWorkspacePort()
    render_request = replace(
        request(tmp_path),
        framing=FramingSpec(True, Decimal("2"), 64, Decimal("1")),
    )
    original = render_service(workspace=workspace).render(
        render_request, job(tmp_path, "seed-union", JobKind.RENDER)
    )
    with Image.new("RGBA", (128, 128), (0, 0, 0, 0)) as edited:
        for x in range(16, 112):
            for y in range(40, 88):
                edited.putpixel((x, y), (20, 100, 220, 255))
        edited.save(original.cut_workspace.path / "frame-000000.png")

    class LosingCasWorkspace(FilesystemWorkspacePort):
        def compare_and_set_union(self, workspace, expected_hashes, metadata):
            del workspace, expected_hashes, metadata
            return False

    artifact = RenderService(
        source=ExplodingSource(),
        segmentation=binding(FakeSegmenter()),
        workspace=LosingCasWorkspace(),
        encoder=FakeEncoder(),
        disk_probe=FakeDiskProbe(),
        clock=FakeClock(),
        output_publisher=AtomicOutputPublisher(),
    ).rebuild(
        replace(render_request, rebuild=True),
        original.cut_workspace,
        job(tmp_path, "cas-loss", JobKind.REBUILD),
    )

    assert artifact.output_path.exists()
    assert "union metadata CAS lost" in " ".join(artifact.notes)


def test_rebuild_encode_failure_preserves_edited_cuts_and_old_output(
    tmp_path,
) -> None:
    render_request = request(tmp_path)
    original = render_service().render(
        render_request, job(tmp_path, "seed-failure", JobKind.RENDER)
    )
    edited_path = original.cut_workspace.path / "frame-000000.png"
    with Image.new("RGBA", (128, 128), (99, 88, 77, 255)) as edited:
        edited.save(edited_path)
    old_output = b"known-good-output"
    render_request.output.path.write_bytes(old_output)

    class FailingEncoder(FakeEncoder):
        def encode(self, frame_paths, delays_ms, destination, **kwargs):
            del frame_paths, delays_ms, kwargs
            destination.write_bytes(b"partial")
            raise AppError(
                ErrorCode.INVALID_OUTPUT,
                "encode",
                "error.output.failed",
                "synthetic encode failure",
                "retry-output",
            )

    with pytest.raises(AppError) as exc:
        render_service(source=ExplodingSource(), encoder=FailingEncoder()).rebuild(
            replace(render_request, rebuild=True),
            original.cut_workspace,
            job(tmp_path, "rebuild-failure", JobKind.REBUILD),
        )

    assert render_request.output.path.read_bytes() == old_output
    with Image.open(edited_path) as persisted:
        assert persisted.getpixel((0, 0)) == (99, 88, 77, 255)
    retained_candidates = tuple(tmp_path.glob(".output.webp.*.candidate"))
    assert len(retained_candidates) == 1
    assert retained_candidates[0].read_bytes() == b"partial"
    assert any(
        "foreign or unverified output-candidate retained" in note
        for note in exc.value.__notes__
    )


def test_rebuild_low_disk_estimate_is_advisory(tmp_path) -> None:
    render_request = request(tmp_path)
    original = render_service().render(
        render_request, job(tmp_path, "seed-low-disk", JobKind.RENDER)
    )

    class LowDisk(FakeDiskProbe):
        def available_bytes(self, _directory) -> int:
            return 0

    artifact = RenderService(
        source=ExplodingSource(),
        segmentation=binding(FakeSegmenter()),
        workspace=FilesystemWorkspacePort(),
        encoder=FakeEncoder(),
        disk_probe=LowDisk(),
        clock=FakeClock(),
        output_publisher=AtomicOutputPublisher(),
    ).rebuild(
        replace(render_request, rebuild=True),
        original.cut_workspace,
        job(tmp_path, "rebuild-low-disk", JobKind.REBUILD),
    )

    assert any("advisory disk estimate" in note for note in artifact.notes)


def test_cross_output_rebuild_cleans_the_actual_snapshot_owner(tmp_path) -> None:
    cuts_output = tmp_path / "cuts-output"
    rebuilt_output = tmp_path / "rebuilt-output"
    cuts_output.mkdir()
    rebuilt_output.mkdir()
    original_request = request(cuts_output)
    original = render_service().render(
        original_request, job(tmp_path, "seed-cross-output", JobKind.RENDER)
    )
    rebuild_request = replace(
        original_request,
        rebuild=True,
        output=replace(original_request.output, directory=rebuilt_output),
    )

    artifact = render_service(source=ExplodingSource()).rebuild(
        rebuild_request,
        original.cut_workspace,
        job(tmp_path, "cross-output-success", JobKind.REBUILD),
    )

    assert artifact.output_path.parent == rebuilt_output
    assert original.cut_workspace.path.is_dir()
    assert not (
        cuts_output / ".rembggui-work" / "scratch" / "cross-output-success"
    ).exists()
    assert not (
        rebuilt_output / ".rembggui-work" / "scratch" / "cross-output-success"
    ).exists()


def test_cross_output_rebuild_failure_cleans_snapshot_and_preserves_state(
    tmp_path,
) -> None:
    cuts_output = tmp_path / "cuts-output"
    rebuilt_output = tmp_path / "rebuilt-output"
    cuts_output.mkdir()
    rebuilt_output.mkdir()
    original_request = request(cuts_output)
    original = render_service().render(
        original_request, job(tmp_path, "seed-cross-failure", JobKind.RENDER)
    )
    rebuild_request = replace(
        original_request,
        rebuild=True,
        output=replace(original_request.output, directory=rebuilt_output),
    )
    rebuild_request.output.path.write_bytes(b"old-output")

    class FailingEncoder(FakeEncoder):
        def encode(self, frame_paths, delays_ms, destination, **kwargs):
            del frame_paths, delays_ms, kwargs
            destination.write_bytes(b"unpublished")
            raise AppError(
                ErrorCode.INVALID_OUTPUT,
                "encode",
                "error.output.failed",
                "synthetic cross-output encode failure",
                "retry-output",
            )

    with pytest.raises(AppError):
        render_service(source=ExplodingSource(), encoder=FailingEncoder()).rebuild(
            rebuild_request,
            original.cut_workspace,
            job(tmp_path, "cross-output-failure", JobKind.REBUILD),
        )

    assert rebuild_request.output.path.read_bytes() == b"old-output"
    assert original.cut_workspace.path.is_dir()
    assert not (
        cuts_output / ".rembggui-work" / "scratch" / "cross-output-failure"
    ).exists()
    assert not (
        rebuilt_output / ".rembggui-work" / "scratch" / "cross-output-failure"
    ).exists()
