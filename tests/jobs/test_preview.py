from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from fractions import Fraction

import numpy as np
import pytest

from matteloop.core.errors import ErrorCode, ValidationError
from matteloop.core.specs import EdgeMode, FramingSpec, SegmentationSpec
from matteloop.core.state import JobKind
from matteloop.jobs.render import (
    FilesystemWorkspacePort,
    PreparedSegmentation,
    PreviewService,
)
from tests.jobs.render_support import (
    FakeClock,
    FakeSegmenter,
    FakeSource,
    binding,
    job,
    render_service,
    request,
)


def test_preview_matches_render_cut_before_global_framing(tmp_path) -> None:
    from matteloop.jobs.render import (
        AtomicOutputPublisher,
        RenderService,
    )
    from tests.jobs.render_support import FakeDiskProbe, FakeEncoder

    source = FakeSource()
    segmenter = FakeSegmenter()
    binding = PreparedSegmentation(
        segmenter,
        "birefnet-portrait",
        "ab" * 32,
        "2.0.75",
        frozenset({"standard", "decontaminate", "alpha_matting"}),
    )
    workspace = FilesystemWorkspacePort()
    preview_service = PreviewService(
        source=source,
        segmentation=binding,
        workspace=workspace,
        clock=FakeClock(),
    )
    encoder = FakeEncoder()
    render_service = RenderService(
        source=source,
        segmentation=binding,
        workspace=workspace,
        encoder=encoder,
        disk_probe=FakeDiskProbe(),
        clock=FakeClock(),
        output_publisher=AtomicOutputPublisher(),
    )
    render_request = request(tmp_path)

    preview = preview_service.preview(
        render_request, Fraction(0), job(tmp_path, "p1", JobKind.PREVIEW)
    )
    artifact = render_service.render(
        render_request, job(tmp_path, "r1", JobKind.RENDER)
    )

    with artifact.cut_workspace.read_promoted_cut(0) as cut:
        assert preview.pre_global_trim_rgba.tobytes() == cut.tobytes()


def test_preview_fingerprint_tracks_the_prepared_model_weight(tmp_path) -> None:
    request_with_same_user_settings = request(tmp_path)
    source = FakeSource()

    first = PreviewService(
        source=source,
        segmentation=PreparedSegmentation(
            FakeSegmenter(),
            "birefnet-portrait",
            "ab" * 32,
            "2.0.75",
            frozenset({"standard"}),
        ),
        workspace=FilesystemWorkspacePort(),
        clock=FakeClock(),
    ).preview(
        request_with_same_user_settings,
        Fraction(0),
        job(tmp_path, "weight-a", JobKind.PREVIEW),
    )
    second = PreviewService(
        source=source,
        segmentation=PreparedSegmentation(
            FakeSegmenter(),
            "birefnet-portrait",
            "cd" * 32,
            "2.0.75",
            frozenset({"standard"}),
        ),
        workspace=FilesystemWorkspacePort(),
        clock=FakeClock(),
    ).preview(
        request_with_same_user_settings,
        Fraction(0),
        job(tmp_path, "weight-b", JobKind.PREVIEW),
    )

    assert first.pre_global_trim_rgba == second.pre_global_trim_rgba
    assert first.fingerprint != second.fingerprint


def test_preview_reports_requested_and_vfr_actual_pts_outside_export_range(
    tmp_path,
) -> None:
    source = FakeSource()
    source.actual_pts[Fraction(1, 4)] = Fraction(1, 5)
    segmenter = FakeSegmenter()
    service = PreviewService(
        source=source,
        segmentation=PreparedSegmentation(
            segmenter,
            "birefnet-portrait",
            "ab" * 32,
            "2.0.75",
            frozenset({"standard"}),
        ),
        workspace=FilesystemWorkspacePort(),
        clock=FakeClock(),
    )
    render_request = replace(
        request(tmp_path),
        sampling=replace(
            request(tmp_path).sampling,
            start=Fraction(1, 2),
            end=Fraction(1),
        ),
    )

    preview = service.preview(
        render_request,
        Fraction(1, 4),
        job(tmp_path, "p-vfr", JobKind.PREVIEW),
    )

    assert preview.requested_timestamp == Fraction(1, 4)
    assert preview.actual_pts == Fraction(1, 5)
    assert preview.outside_export_range
    assert preview.pre_global_trim_rgba.owner_is_bytes


def test_trim_preview_uses_local_estimate_until_matching_union_exists(
    tmp_path,
) -> None:
    source = FakeSource()
    segmenter = FakeSegmenter()
    workspace = FilesystemWorkspacePort()
    prepared = binding(segmenter)
    service = PreviewService(
        source=source,
        segmentation=prepared,
        workspace=workspace,
        clock=FakeClock(),
    )
    render_request = replace(
        request(tmp_path),
        framing=FramingSpec(True, Decimal("2"), 40, Decimal("1")),
    )

    estimated = service.preview(
        render_request, Fraction(0), job(tmp_path, "estimate", JobKind.PREVIEW)
    )

    assert estimated.local_bounds_estimate is not None
    assert estimated.applied_global_bounds is None
    assert not estimated.global_bounds_exact
    assert estimated.display_rgba.size == (128, 128)

    render_service(
        source=source,
        segmenter=segmenter,
        workspace=workspace,
    ).render(render_request, job(tmp_path, "seed-union", JobKind.RENDER))
    exact = service.preview(
        render_request, Fraction(0), job(tmp_path, "exact", JobKind.PREVIEW)
    )

    assert exact.applied_global_bounds == estimated.local_bounds_estimate
    assert exact.global_bounds_exact
    assert exact.display_rgba.size == (144, 160)


def test_unsupported_model_edge_pair_fails_before_probe_or_inference(
    tmp_path,
) -> None:
    source = FakeSource()
    segmenter = FakeSegmenter()
    service = PreviewService(
        source=source,
        segmentation=PreparedSegmentation(
            segmenter,
            "birefnet-portrait",
            "ab" * 32,
            "2.0.75",
            frozenset({"standard"}),
        ),
        workspace=FilesystemWorkspacePort(),
        clock=FakeClock(),
    )
    render_request = replace(
        request(tmp_path),
        segmentation=SegmentationSpec(
            "birefnet-portrait", EdgeMode.DECONTAMINATE_COLORS
        ),
    )

    with pytest.raises(ValidationError) as exc:
        service.preview(
            render_request,
            Fraction(0),
            job(tmp_path, "unsupported", JobKind.PREVIEW),
        )

    assert exc.value.code is ErrorCode.INVALID_SEGMENTATION
    assert source.probe_calls == 0
    assert segmenter.calls == []


def test_decontaminate_is_deterministic_local_rgba_postprocess(tmp_path) -> None:
    class EdgeSegmenter(FakeSegmenter):
        def segment(self, frame, request):
            del frame
            self.calls.append(request)
            result = np.zeros((128, 128, 4), dtype=np.uint8)
            result[0, 0] = (10, 20, 30, 0)
            result[0, 1] = (25, 50, 100, 128)
            result[0, 2] = (7, 8, 9, 255)
            return result

    segmenter = EdgeSegmenter()
    service = PreviewService(
        source=FakeSource(),
        segmentation=binding(segmenter),
        workspace=FilesystemWorkspacePort(),
        clock=FakeClock(),
    )
    render_request = replace(
        request(tmp_path),
        segmentation=SegmentationSpec(
            "birefnet-portrait", EdgeMode.DECONTAMINATE_COLORS
        ),
    )

    preview = service.preview(
        render_request, Fraction(0), job(tmp_path, "edge", JobKind.PREVIEW)
    )
    pixels = preview.pre_global_trim_rgba.tobytes()

    assert pixels[0:4] == bytes((0, 0, 0, 0))
    assert pixels[4:8] == bytes((50, 100, 199, 128))
    assert pixels[8:12] == bytes((7, 8, 9, 255))
    assert segmenter.calls[0].options.edge_mode == "decontaminate"


def test_preview_rejects_source_end_and_reports_injected_elapsed_time(
    tmp_path,
) -> None:
    class TickingClock(FakeClock):
        def __init__(self) -> None:
            self.values = iter((100, 250, 300))

        def time_ns(self) -> int:
            return next(self.values)

    source = FakeSource()
    service = PreviewService(
        source=source,
        segmentation=binding(FakeSegmenter()),
        workspace=FilesystemWorkspacePort(),
        clock=TickingClock(),
    )

    preview = service.preview(
        request(tmp_path), Fraction(0), job(tmp_path, "timed", JobKind.PREVIEW)
    )
    assert preview.processing_duration_ns == 150

    with pytest.raises(ValidationError) as exc:
        service.preview(
            request(tmp_path),
            Fraction(2),
            job(tmp_path, "source-end", JobKind.PREVIEW),
        )
    assert exc.value.code is ErrorCode.INVALID_SAMPLING
