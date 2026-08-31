from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image

from matteloop.core.rgba import RgbaOwnershipTracker
from matteloop.core.specs import (
    CollisionPolicy,
    CropSpec,
    EdgeMode,
    FramingSpec,
    OutputSpec,
    RenderRequest,
    SamplingSpec,
    SegmentationSpec,
)
from matteloop.core.state import JobKind
from matteloop.core.webp import encode_lossless_webp
from matteloop.jobs.context import CancellationState, JobContext
from matteloop.jobs.protocol import SegmentRequest
from matteloop.jobs.render import (
    AtomicOutputPublisher,
    FilesystemWorkspacePort,
    PreparedSegmentation,
    RenderService,
    ValidatedCandidate,
)
from matteloop.jobs.source import DecodedFrame, SourceRevision


@dataclass(frozen=True)
class FakeSourceInfo:
    width: int = 128
    height: int = 128
    duration: Fraction = Fraction(2)
    revision: SourceRevision = SourceRevision(1, 2, 100, 4, 5)


class FakeSource:
    def __init__(self) -> None:
        self.probe_calls = 0
        self.hash_calls = 0
        self.decode_calls: list[Fraction] = []
        self.actual_pts: dict[Fraction, Fraction] = {}

    def probe(self, _path: Path, _context: JobContext) -> FakeSourceInfo:
        self.probe_calls += 1
        return FakeSourceInfo()

    def provisional_fingerprint(self, path: Path, _context: JobContext) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def complete_sha256(self, path: Path, _context: JobContext) -> str:
        self.hash_calls += 1
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def decode(
        self,
        _path: Path,
        timestamp: Fraction,
        request_id: int,
        _source_info: FakeSourceInfo,
        _context: JobContext,
        ownership: RgbaOwnershipTracker,
    ) -> DecodedFrame:
        self.decode_calls.append(timestamp)
        red = int(timestamp * 90) % 256
        image = Image.new("RGBA", (128, 128), (red, 60, 90, 255))
        ownership.register(image)
        return DecodedFrame(
            image,
            timestamp,
            self.actual_pts.get(timestamp, timestamp),
            request_id,
            FakeSourceInfo().revision,
        )


class ExplodingSource(FakeSource):
    def _explode(self) -> None:
        raise AssertionError("Rebuild touched source I/O")

    def probe(self, _path: Path, _context: JobContext) -> FakeSourceInfo:
        self._explode()

    def provisional_fingerprint(self, path: Path, _context: JobContext) -> str:
        del path
        self._explode()

    def complete_sha256(self, path: Path, _context: JobContext) -> str:
        del path
        self._explode()

    def decode(
        self,
        _path: Path,
        timestamp: Fraction,
        request_id: int,
        _source_info: FakeSourceInfo,
        _context: JobContext,
        ownership: RgbaOwnershipTracker,
    ) -> DecodedFrame:
        del timestamp, request_id, ownership
        self._explode()


class FakeSegmenter:
    def __init__(self) -> None:
        self.calls: list[SegmentRequest] = []

    def segment(self, frame: np.ndarray, request: SegmentRequest) -> np.ndarray:
        self.calls.append(request)
        result = np.zeros(frame.shape[:2] + (4,), dtype=np.uint8)
        result[24:104, 32:96, :3] = frame[24:104, 32:96, :3]
        result[24:104, 32:96, 3] = 255
        return result


class FakeEncoder:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Path, ...], tuple[int, ...], Path]] = []

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
        del work_dir, max_bytes, context
        paths = tuple(frame_paths)
        delays = tuple(delays_ms)
        self.calls.append((paths, delays, destination))
        summary = encode_lossless_webp(
            paths,
            delays,
            destination,
            rgba_ownership_tracker=ownership,
        )
        return ValidatedCandidate.validate(
            destination,
            summary,
            ownership=ownership,
        )


class FakeDiskProbe:
    def available_bytes(self, _directory: Path) -> int:
        return 10**12


class FakeClock:
    def time_ns(self) -> int:
        return 123_456_789


def binding(
    segmenter: FakeSegmenter,
    *,
    supported: frozenset[str] = frozenset(
        {"standard", "decontaminate", "alpha_matting"}
    ),
) -> PreparedSegmentation:
    return PreparedSegmentation(
        segmenter,
        "birefnet-portrait",
        "ab" * 32,
        "2.0.72",
        supported,
    )


def render_service(
    *,
    source: FakeSource | ExplodingSource | None = None,
    segmenter: FakeSegmenter | None = None,
    workspace: FilesystemWorkspacePort | None = None,
    encoder: FakeEncoder | None = None,
    disk_probe: FakeDiskProbe | None = None,
    output_publisher: AtomicOutputPublisher | None = None,
) -> RenderService:
    chosen_segmenter = segmenter if segmenter is not None else FakeSegmenter()
    return RenderService(
        source=source if source is not None else FakeSource(),
        segmentation=binding(chosen_segmenter),
        workspace=workspace if workspace is not None else FilesystemWorkspacePort(),
        encoder=encoder if encoder is not None else FakeEncoder(),
        disk_probe=disk_probe if disk_probe is not None else FakeDiskProbe(),
        clock=FakeClock(),
        output_publisher=(
            output_publisher
            if output_publisher is not None
            else AtomicOutputPublisher()
        ),
    )


def request(tmp_path: Path, **changes: object) -> RenderRequest:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake-video")
    base = RenderRequest(
        source=source,
        sampling=SamplingSpec(Fraction(0), Fraction(1), 2),
        crop=CropSpec(0, 0, 128, 128),
        segmentation=SegmentationSpec("birefnet-portrait", EdgeMode.STANDARD),
        framing=FramingSpec(False, Decimal("2"), 0, Decimal("1")),
        output=OutputSpec(
            tmp_path,
            "output.webp",
            collision_policy=CollisionPolicy.REPLACE,
        ),
    )
    return replace(base, **changes)


def job(tmp_path: Path, job_id: str, kind: JobKind) -> JobContext:
    return JobContext(
        job_id,
        kind,
        tmp_path / "job-work",
        lambda _event: None,
        CancellationState(),
    )
