"""Render-service wiring that reports the stages visible in the Qt job dialog."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import cast

import numpy as np
from PIL import Image

from rembggui.core.rgba import RgbaOwnershipTracker
from rembggui.core.specs import RenderRequest
from rembggui.jobs.context import JobContext
from rembggui.jobs.protocol import SegmentRequest
from rembggui.jobs.render import (
    AtomicOutputPublisher,
    EncoderPort,
    FilesystemWorkspacePort,
    LocalSourcePort,
    PillowWebPEncoder,
    PreparedSegmentation,
    RenderArtifact,
    RenderService,
    SegmentationPort,
    SourcePort,
    SystemClock,
    SystemDiskProbe,
    ValidatedCandidate,
    WorkspacePort,
)
from rembggui.jobs.source import DecodedFrame, SourceInfo
from rembggui.jobs.workspace import CutFrame, CutWorkspace


class _StageReporter:
    def __init__(self, context: JobContext) -> None:
        self._context = context

    def report(self, stage: str) -> None:
        self._context.progress(stage, 0)


class _SourceStagePort:
    def __init__(self, delegate: SourcePort, reporter: _StageReporter) -> None:
        self._delegate = delegate
        self._reporter = reporter

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    def decode(
        self,
        path: Path,
        timestamp: Fraction,
        request_id: int,
        source_info: SourceInfo,
        context: JobContext,
        ownership: RgbaOwnershipTracker,
    ) -> DecodedFrame:
        self._reporter.report("Decode")
        return self._delegate.decode(
            path, timestamp, request_id, source_info, context, ownership
        )


class _SegmentationStagePort:
    def __init__(self, delegate: SegmentationPort, reporter: _StageReporter) -> None:
        self._delegate = delegate
        self._reporter = reporter

    def segment(self, frame: np.ndarray, request: SegmentRequest) -> np.ndarray:
        self._reporter.report("Segmentation")
        return self._delegate.segment(frame, request)


class _WorkspaceStagePort:
    def __init__(self, delegate: WorkspacePort, reporter: _StageReporter) -> None:
        self._delegate = delegate
        self._reporter = reporter

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    def stage(
        self, workspace: CutWorkspace, index: int, image: Image.Image
    ) -> CutFrame:
        self._reporter.report("Post-process")
        return self._delegate.stage(workspace, index, image)

    def read_cut(
        self,
        workspace: CutWorkspace,
        index: int,
        ownership: RgbaOwnershipTracker,
    ) -> Image.Image:
        self._reporter.report("Post-process")
        return self._delegate.read_cut(workspace, index, ownership)


class _EncoderStagePort:
    def __init__(self, delegate: EncoderPort, reporter: _StageReporter) -> None:
        self._delegate = delegate
        self._reporter = reporter

    def encode(
        self,
        frame_paths: tuple[Path, ...],
        delays_ms: tuple[int, ...],
        destination: Path,
        *,
        work_dir: Path,
        max_bytes: int | None,
        context: JobContext,
        ownership: RgbaOwnershipTracker,
    ) -> ValidatedCandidate:
        self._reporter.report("Encode")
        candidate = self._delegate.encode(
            frame_paths,
            delays_ms,
            destination,
            work_dir=work_dir,
            max_bytes=max_bytes,
            context=context,
            ownership=ownership,
        )
        self._reporter.report("Validate")
        return candidate


def render_prepared(
    prepared: PreparedSegmentation,
    request: RenderRequest,
    context: JobContext,
) -> RenderArtifact:
    """Run the existing service with stage-reporting adapters around its ports."""
    reporter = _StageReporter(context)
    segmentation = PreparedSegmentation(
        _SegmentationStagePort(prepared.port, reporter),
        prepared.model_id,
        prepared.model_weight_sha256,
        prepared.rembg_version,
        prepared.supported_edge_modes,
    )
    service = RenderService(
        source=cast(SourcePort, _SourceStagePort(LocalSourcePort(), reporter)),
        segmentation=segmentation,
        workspace=cast(
            WorkspacePort, _WorkspaceStagePort(FilesystemWorkspacePort(), reporter)
        ),
        encoder=cast(EncoderPort, _EncoderStagePort(PillowWebPEncoder(), reporter)),
        disk_probe=SystemDiskProbe(),
        clock=SystemClock(),
        output_publisher=AtomicOutputPublisher(),
    )
    return service.render(request, context)
