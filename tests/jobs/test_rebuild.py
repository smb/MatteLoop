from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from fractions import Fraction

import pytest
from PIL import Image

from matteloop.core.errors import AppError, ErrorCode, ValidationError
from matteloop.core.specs import (
    CropSpec,
    FramingSpec,
    MismatchMode,
    ResizeSpec,
    SamplingSpec,
    TransformSpec,
)
from matteloop.core.state import JobKind
from matteloop.core.timebase import webp_delays
from matteloop.core.webp import validate_webp
from matteloop.jobs.render import (
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
    FakeSourceInfo,
    binding,
    job,
    render_service,
    request,
)


class _LongerSource(FakeSource):
    """A source whose probed duration covers an 8-frame, 3 fps sampling grid."""

    def probe(self, path, context):
        del path, context
        self.probe_calls += 1
        return FakeSourceInfo(duration=Fraction(3))


def test_rebuild_never_probes_hashes_decodes_or_segments_source(tmp_path) -> None:
    workspace = FilesystemWorkspacePort()
    segmenter = FakeSegmenter()
    binding = PreparedSegmentation(
        segmenter,
        "birefnet-portrait",
        "ab" * 32,
        "2.0.75",
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
    assert not tuple(tmp_path.glob(".output.webp.*.candidate"))
    assert not (tmp_path / ".matteloop-work" / "scratch" / "rebuild-failure").exists()
    assert not any(
        "output-candidate retained" in note
        for note in getattr(exc.value, "__notes__", ())
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
        cuts_output / ".matteloop-work" / "scratch" / "cross-output-success"
    ).exists()
    assert not (
        rebuilt_output / ".matteloop-work" / "scratch" / "cross-output-success"
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
        cuts_output / ".matteloop-work" / "scratch" / "cross-output-failure"
    ).exists()
    assert not (
        rebuilt_output / ".matteloop-work" / "scratch" / "cross-output-failure"
    ).exists()


def _rebuild_service(workspace: FilesystemWorkspacePort, encoder) -> RenderService:
    return RenderService(
        source=ExplodingSource(),
        segmentation=binding(FakeSegmenter()),
        workspace=workspace,
        encoder=encoder,
        disk_probe=FakeDiskProbe(),
        clock=FakeClock(),
        output_publisher=AtomicOutputPublisher(),
    )


def test_identity_transform_rebuild_is_byte_identical(tmp_path) -> None:
    """AC 1: an explicit identity ``TransformSpec`` and the ``RenderRequest``
    default must reach the encoder with the same paths and delays and produce
    a byte-identical WebP."""
    workspace = FilesystemWorkspacePort()
    seed_request = request(tmp_path)
    original = render_service(workspace=workspace).render(
        seed_request, job(tmp_path, "seed-identity", JobKind.RENDER)
    )

    explicit_encoder = FakeEncoder()
    _rebuild_service(workspace, explicit_encoder).rebuild(
        replace(
            seed_request,
            rebuild=True,
            transform=TransformSpec(),
            output=replace(seed_request.output, filename="explicit.webp"),
        ),
        original.cut_workspace,
        job(tmp_path, "rebuild-explicit", JobKind.REBUILD),
    )

    default_encoder = FakeEncoder()
    _rebuild_service(workspace, default_encoder).rebuild(
        replace(
            seed_request,
            rebuild=True,
            output=replace(seed_request.output, filename="default.webp"),
        ),
        original.cut_workspace,
        job(tmp_path, "rebuild-default", JobKind.REBUILD),
    )

    explicit_bytes = (tmp_path / "explicit.webp").read_bytes()
    default_bytes = (tmp_path / "default.webp").read_bytes()
    assert explicit_bytes == default_bytes

    explicit_paths, explicit_delays, _ = explicit_encoder.calls[0]
    default_paths, default_delays, _ = default_encoder.calls[0]
    assert [path.name for path in explicit_paths] == [
        path.name for path in default_paths
    ]
    assert explicit_delays == default_delays


def test_trim_keeps_exactly_the_selected_frames_and_their_delays(tmp_path) -> None:
    """AC 2 / E2 / T7: the kept delays are a *slice* of the full non-uniform
    grid, not a recomputation over the kept count."""
    workspace = FilesystemWorkspacePort()
    seed_request = request(
        tmp_path, sampling=SamplingSpec(Fraction(0), Fraction(8, 3), 3)
    )
    original = render_service(source=_LongerSource(), workspace=workspace).render(
        seed_request, job(tmp_path, "seed-trim", JobKind.RENDER)
    )
    full_delays = webp_delays(8, 3)
    expected = full_delays[2:6]
    # The regression this test guards against: a recomputed grid over just
    # the kept count would sum to the same total but not match term-by-term.
    assert expected != webp_delays(4, 3)
    assert sum(expected) == sum(webp_delays(4, 3))

    rebuild_encoder = FakeEncoder()
    artifact = _rebuild_service(workspace, rebuild_encoder).rebuild(
        replace(
            seed_request,
            rebuild=True,
            transform=TransformSpec(first_frame=2, last_frame=5),
            output=replace(seed_request.output, filename="trimmed.webp"),
        ),
        original.cut_workspace,
        job(tmp_path, "rebuild-trim", JobKind.REBUILD),
    )

    assert artifact.frame_count == 4
    assert artifact.delays_ms == expected
    assert rebuild_encoder.calls[0][1] == expected
    info = validate_webp(
        artifact.output_path, expected_frames=4, expected_duration_ms=sum(expected)
    )
    assert info.frames == 4
    assert info.delays_ms == expected


@pytest.mark.parametrize(
    ("transform", "fragment"),
    [
        (TransformSpec(first_frame=8), "first_frame 8 exceeds last_frame 7"),
        (
            TransformSpec(last_frame=9),
            "last_frame 9 exceeds the last stored frame 7",
        ),
    ],
    ids=["fewer-than-one-frame", "last-frame-beyond-cut"],
)
def test_trim_outside_the_cut_is_rejected_before_any_file_is_written(
    tmp_path, transform: TransformSpec, fragment: str
) -> None:
    """AC 3 / E3: an out-of-bounds trim raises INVALID_TRANSFORM before the
    framed-inputs directory is created, and the old output is untouched."""
    workspace = FilesystemWorkspacePort()
    seed_request = request(
        tmp_path, sampling=SamplingSpec(Fraction(0), Fraction(8, 3), 3)
    )
    original = render_service(source=_LongerSource(), workspace=workspace).render(
        seed_request, job(tmp_path, "seed-bad-trim", JobKind.RENDER)
    )
    before = original.output_path.read_bytes()

    with pytest.raises(ValidationError) as exc:
        _rebuild_service(workspace, FakeEncoder()).rebuild(
            replace(seed_request, rebuild=True, transform=transform),
            original.cut_workspace,
            job(
                tmp_path,
                f"rebuild-bad-trim-{transform.first_frame}-{transform.last_frame}",
                JobKind.REBUILD,
            ),
        )

    assert exc.value.code is ErrorCode.INVALID_TRANSFORM
    assert fragment in str(exc.value)
    assert original.output_path.read_bytes() == before
    assert not list(tmp_path.rglob("framed-inputs"))


def test_crop_and_resize_change_output_dimensions(tmp_path) -> None:
    """Crop then resize (stretch) produces the resolved final canvas size."""
    workspace = FilesystemWorkspacePort()
    seed_request = request(tmp_path)
    original = render_service(workspace=workspace).render(
        seed_request, job(tmp_path, "seed-crop-resize", JobKind.RENDER)
    )

    artifact = _rebuild_service(workspace, FakeEncoder()).rebuild(
        replace(
            seed_request,
            rebuild=True,
            transform=TransformSpec(
                crop=CropSpec(0, 0, 64, 64),
                resize=ResizeSpec(width=256, height=128, mismatch=MismatchMode.STRETCH),
            ),
        ),
        original.cut_workspace,
        job(tmp_path, "rebuild-crop-resize", JobKind.REBUILD),
    )

    assert (artifact.width, artifact.height) == (256, 128)
    info = validate_webp(
        artifact.output_path,
        expected_frames=artifact.frame_count,
        expected_duration_ms=sum(artifact.delays_ms),
    )
    assert (info.width, info.height) == (256, 128)


def test_transform_trim_does_not_affect_the_framed_size(tmp_path) -> None:
    """E15: the framed size (and the union it is derived from) is computed
    over every stored frame, independent of the transform's trim."""
    workspace = FilesystemWorkspacePort()
    seed_request = replace(
        request(tmp_path),
        framing=FramingSpec(True, Decimal("2"), 64, Decimal("1")),
    )
    original = render_service(workspace=workspace).render(
        seed_request, job(tmp_path, "seed-trim-union", JobKind.RENDER)
    )

    full_range = _rebuild_service(workspace, FakeEncoder()).rebuild(
        replace(
            seed_request,
            rebuild=True,
            output=replace(seed_request.output, filename="full.webp"),
        ),
        original.cut_workspace,
        job(tmp_path, "rebuild-full-range", JobKind.REBUILD),
    )
    trimmed = _rebuild_service(workspace, FakeEncoder()).rebuild(
        replace(
            seed_request,
            rebuild=True,
            transform=TransformSpec(first_frame=0, last_frame=0),
            output=replace(seed_request.output, filename="trimmed.webp"),
        ),
        original.cut_workspace,
        job(tmp_path, "rebuild-trimmed-range", JobKind.REBUILD),
    )

    assert (trimmed.width, trimmed.height) == (full_range.width, full_range.height)


def test_transforms_never_touch_stored_cut_frames(tmp_path) -> None:
    """AC 7 / E10: any number of transforms and rebuilds leave the stored
    cut PNGs' sha256 (per the manifest) unchanged."""
    workspace = FilesystemWorkspacePort()
    seed_request = request(tmp_path)
    original = render_service(workspace=workspace).render(
        seed_request, job(tmp_path, "seed-sha", JobKind.RENDER)
    )
    before_hashes = tuple(
        frame.sha256 for frame in workspace.validate(original.cut_workspace).frames
    )

    transforms = (
        TransformSpec(),
        TransformSpec(first_frame=1),
        TransformSpec(
            crop=CropSpec(0, 0, 64, 64),
            resize=ResizeSpec(width=200, height=150, mismatch=MismatchMode.PAD),
        ),
    )
    for index, transform in enumerate(transforms):
        _rebuild_service(workspace, FakeEncoder()).rebuild(
            replace(
                seed_request,
                rebuild=True,
                transform=transform,
                output=replace(seed_request.output, filename=f"out-{index}.webp"),
            ),
            original.cut_workspace,
            job(tmp_path, f"rebuild-sha-{index}", JobKind.REBUILD),
        )

    after_hashes = tuple(
        frame.sha256 for frame in workspace.validate(original.cut_workspace).frames
    )
    assert after_hashes == before_hashes
