"""Preview, normal-render, and source-free Rebuild orchestration.

The two render modes converge before framing and never encode durable editable
cuts directly::

    normal:  source -> shared cut pipeline -> staged cuts -> private snapshot
                                                   |              |
                                                   v              v
                                             durable promote   framing -> encode

    rebuild: durable cuts -----------------> private snapshot -> framing -> encode

``_produce_cut_frame`` is the only decode/crop/segment/edge-cleanup path.  The
second pass reads one snapshot cut at a time under one immutable FramingPlan.
"""

from __future__ import annotations

import errno
import gc
import hashlib
import importlib
import os
import shutil
import stat
import tempfile
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Protocol, cast

import numpy as np
from PIL import Image

from rembggui.core.errors import AppError, ErrorCode, ValidationError
from rembggui.core.fingerprints import (
    PIPELINE_SCHEMA_VERSION,
    REMBG_VERSION,
    complete_source_sha256,
    cut_cache_key_from_inputs,
    cut_cache_key_inputs,
    preview_fingerprint,
    provisional_source_fingerprint,
    render_fingerprint,
    union_fingerprint,
)
from rembggui.core.geometry import (
    FramingPlan,
    PixelBounds,
    alpha_bounds,
    apply_framing,
    apply_source_crop,
)
from rembggui.core.rgba import RgbaOwnershipTracker
from rembggui.core.specs import (
    CollisionPolicy,
    EdgeMode,
    RenderRequest,
    catalog_edge_mode,
)
from rembggui.core.timebase import sample_times, webp_delays
from rembggui.core.webp import (
    EncodeSummary,
    WebPInfo,
    encode_lossless_webp,
    fit_webp_to_size,
    validate_webp,
)
from rembggui.jobs.context import JobContext, JobTerminalState
from rembggui.jobs.protocol import PROTOCOL_VERSION, SegmentOptions, SegmentRequest
from rembggui.jobs.source import DecodedFrame, SourceInfo, decode_frame, probe_source
from rembggui.jobs.workspace import (
    CutFrame,
    CutManifest,
    CutUnionMetadata,
    CutWorkspace,
    cleanup_scratch,
    compare_and_set_union_metadata,
    detect_external_edits,
    discard_staged_set,
    promote_for_render,
    snapshot_for_rebuild,
    stage_cut,
    validate_cut_set,
)


class SourcePort(Protocol):
    def probe(self, path: Path, context: JobContext) -> SourceInfo: ...

    def provisional_fingerprint(self, path: Path, context: JobContext) -> str: ...

    def complete_sha256(self, path: Path, context: JobContext) -> str: ...

    def decode(
        self,
        path: Path,
        timestamp: Fraction,
        request_id: int,
        source_info: SourceInfo,
        context: JobContext,
        ownership: RgbaOwnershipTracker,
    ) -> DecodedFrame: ...


class SegmentationPort(Protocol):
    def segment(self, frame: np.ndarray, request: SegmentRequest) -> np.ndarray: ...


class WorkspacePort(Protocol):
    def open_promoted(self, output_directory: Path, cache_key: str) -> CutWorkspace: ...

    def create_staging(
        self, output_directory: Path, cache_key: str, job_id: str
    ) -> CutWorkspace: ...

    def stage(
        self, workspace: CutWorkspace, index: int, image: Image.Image
    ) -> CutFrame: ...

    def promote_render(
        self,
        workspace: CutWorkspace,
        manifest: CutManifest,
        scratch_directory: Path,
        context: JobContext,
    ) -> tuple[CutWorkspace, CutWorkspace, CutManifest]: ...

    def validate(self, workspace: CutWorkspace) -> CutManifest: ...

    def detect_edits(self, workspace: CutWorkspace) -> CutManifest: ...

    def snapshot_rebuild(
        self, workspace: CutWorkspace, scratch_directory: Path, context: JobContext
    ) -> CutWorkspace: ...

    def read_cut(
        self,
        workspace: CutWorkspace,
        index: int,
        ownership: RgbaOwnershipTracker,
    ) -> Image.Image: ...

    def discard_stage(self, workspace: CutWorkspace) -> bool: ...

    def cleanup_job(self, output_directory: Path, job_id: str) -> bool: ...

    def compare_and_set_union(
        self,
        workspace: CutWorkspace,
        expected_hashes: Sequence[str],
        metadata: CutUnionMetadata,
    ) -> bool: ...


class EncoderPort(Protocol):
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
    ) -> ValidatedCandidate: ...


class DiskProbe(Protocol):
    def available_bytes(self, directory: Path) -> int: ...


class Clock(Protocol):
    def time_ns(self) -> int: ...


class OutputPublisher(Protocol):
    def candidate_path(self, destination: Path, job_id: str) -> Path: ...

    def publish(
        self,
        candidate: ValidatedCandidate,
        destination: Path,
        policy: CollisionPolicy,
        *,
        cleanup_notes: list[str] | None = None,
    ) -> Path: ...


@dataclass(frozen=True, slots=True)
class PreparedSegmentation:
    port: SegmentationPort
    model_id: str
    model_weight_sha256: str
    rembg_version: str
    supported_edge_modes: frozenset[str]

    def __post_init__(self) -> None:
        if not callable(getattr(self.port, "segment", None)):
            raise TypeError("prepared segmentation port must provide segment()")
        if not isinstance(self.model_id, str) or not self.model_id:
            raise ValueError("prepared model ID must be non-empty")
        if not _is_sha256(self.model_weight_sha256):
            raise ValueError("prepared model weight identity must be SHA-256")
        if not isinstance(self.rembg_version, str) or not self.rembg_version:
            raise ValueError("prepared rembg version must be non-empty")
        if (
            not isinstance(self.supported_edge_modes, frozenset)
            or not self.supported_edge_modes
            or not self.supported_edge_modes
            <= frozenset({"standard", "decontaminate", "alpha_matting"})
        ):
            raise ValueError("prepared edge modes must use pinned catalog IDs")

    def validate_for(self, request: RenderRequest) -> None:
        mode = catalog_edge_mode(request.segmentation.edge_mode)
        if (
            request.segmentation.model_id != self.model_id
            or self.rembg_version != REMBG_VERSION
            or mode not in self.supported_edge_modes
        ):
            raise ValidationError(
                ErrorCode.INVALID_SEGMENTATION,
                "segmentation",
                "prepared model and edge compatibility must match the request",
            )


@dataclass(frozen=True, slots=True)
class ImmutableRgba:
    width: int
    height: int
    data: bytes

    def __post_init__(self) -> None:
        if (
            type(self.width) is not int
            or type(self.height) is not int
            or self.width < 1
            or self.height < 1
            or type(self.data) is not bytes
            or len(self.data) != self.width * self.height * 4
        ):
            raise ValueError("immutable RGBA bytes do not match their dimensions")

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    @property
    def owner_is_bytes(self) -> bool:
        return type(self.data) is bytes

    def tobytes(self) -> bytes:
        return self.data

    def to_image(self) -> Image.Image:
        return Image.frombytes("RGBA", self.size, self.data)


@dataclass(frozen=True, slots=True)
class PreviewResult:
    fingerprint: str
    requested_timestamp: Fraction
    actual_pts: Fraction
    pre_global_trim_rgba: ImmutableRgba
    display_rgba: ImmutableRgba
    local_bounds_estimate: PixelBounds | None
    applied_global_bounds: PixelBounds | None
    global_bounds_exact: bool
    outside_export_range: bool
    processing_duration_ns: int


@dataclass(frozen=True, slots=True)
class RenderArtifact:
    output_path: Path
    fingerprint: str
    cut_workspace: CutWorkspace
    manifest: CutManifest
    frame_count: int
    width: int
    height: int
    file_size: int
    duration_ms: int
    requested_timestamps: tuple[Fraction, ...]
    actual_pts: tuple[Fraction, ...] | None
    delays_ms: tuple[int, ...]
    ownership_peak: int
    ownership_current: int
    rebuilt: bool
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateFileIdentity:
    """Stable filesystem identity recorded while a candidate handle is held."""

    device: int
    inode: int
    size: int
    modified_ns: int


@dataclass(frozen=True, slots=True, init=False)
class ValidatedCandidate:
    """A WebP whose validated bytes stay bound to an open file description.

    The publisher compares the final directory entry with this held descriptor
    after its atomic commit.  A pathname swap therefore cannot substitute bytes
    between WebP validation and publication.
    """

    path: Path
    summary: EncodeSummary
    identity: CandidateFileIdentity
    sha256: str
    _descriptor: int = field(repr=False, compare=False)

    @classmethod
    def validate(
        cls,
        path: Path,
        summary: EncodeSummary,
        *,
        ownership: RgbaOwnershipTracker | None = None,
    ) -> ValidatedCandidate:
        if not isinstance(path, Path) or not isinstance(summary, EncodeSummary):
            raise TypeError("candidate path and encode summary are required")
        if summary.destination != path:
            raise _output_error("encode summary does not identify its candidate")
        descriptor = _open_held_file(path)
        try:
            before = _candidate_identity(os.fstat(descriptor))
            _require_regular_nonempty(before)
            if _path_identity(path) != before:
                raise _output_error("candidate path changed before WebP validation")
            digest = _sha256_descriptor(descriptor)
            info = _validate_held_candidate_webp(
                path,
                descriptor,
                before,
                summary,
                ownership,
            )
            after = _candidate_identity(os.fstat(descriptor))
            if (
                after != before
                or _path_identity(path) != before
                or _sha256_descriptor(descriptor) != digest
            ):
                raise _output_error("candidate changed during WebP validation")
            expected = (
                summary.width,
                summary.height,
                summary.frames,
                summary.duration_ms,
                summary.file_size,
            )
            actual = (
                info.width,
                info.height,
                info.frames,
                info.duration_ms,
                info.file_size,
            )
            if actual != expected or before.size != summary.file_size:
                raise _output_error("encode summary does not match the validated WebP")
            candidate = object.__new__(cls)
            object.__setattr__(candidate, "path", path)
            object.__setattr__(candidate, "summary", summary)
            object.__setattr__(candidate, "identity", before)
            object.__setattr__(candidate, "sha256", digest)
            object.__setattr__(candidate, "_descriptor", descriptor)
            return candidate
        except BaseException as error:
            close_error: OSError | None = None
            try:
                os.close(descriptor)
            except OSError as caught:
                close_error = caught
            if isinstance(error, OSError):
                wrapped = _map_output_os_error(
                    error, "cannot bind validated output candidate"
                )
                if close_error is not None:
                    wrapped.add_note(
                        f"additional candidate-handle cleanup failure: {close_error}"
                    )
                raise wrapped from error
            if close_error is not None:
                error.add_note(
                    f"additional candidate-handle cleanup failure: {close_error}"
                )
            raise

    def close(self) -> None:
        os.close(self._descriptor)


class LocalSourcePort:
    def probe(self, path: Path, context: JobContext) -> SourceInfo:
        context.checkpoint("source-probe")
        result = probe_source(path)
        context.checkpoint("source-probe")
        return result

    def provisional_fingerprint(self, path: Path, context: JobContext) -> str:
        context.checkpoint("source-fingerprint")
        result = provisional_source_fingerprint(path)
        context.checkpoint("source-fingerprint")
        return result

    def complete_sha256(self, path: Path, context: JobContext) -> str:
        try:
            return complete_source_sha256(
                path,
                is_cancelled=lambda: context.cancellation.requested,
            )
        except AppError as error:
            if error.code is ErrorCode.JOB_CANCELLED:
                context.checkpoint("source-hash")
            raise

    def decode(
        self,
        path: Path,
        timestamp: Fraction,
        request_id: int,
        source_info: SourceInfo,
        context: JobContext,
        ownership: RgbaOwnershipTracker,
    ) -> DecodedFrame:
        try:
            return decode_frame(
                path,
                timestamp,
                request_id,
                is_cancelled=lambda: context.cancellation.requested,
                expected_revision=source_info.revision,
                validation_proof=source_info.validation_proof,
                rgba_ownership_tracker=ownership,
            )
        except AppError as error:
            if error.code is ErrorCode.JOB_CANCELLED:
                context.checkpoint("decode")
            raise


class FilesystemWorkspacePort:
    def open_promoted(self, output_directory: Path, cache_key: str) -> CutWorkspace:
        return CutWorkspace.open(output_directory, cache_key)

    def create_staging(
        self, output_directory: Path, cache_key: str, job_id: str
    ) -> CutWorkspace:
        return CutWorkspace.create_staging(output_directory, cache_key, job_id)

    def stage(
        self, workspace: CutWorkspace, index: int, image: Image.Image
    ) -> CutFrame:
        return stage_cut(workspace, index, image)

    def promote_render(
        self,
        workspace: CutWorkspace,
        manifest: CutManifest,
        scratch_directory: Path,
        context: JobContext,
    ) -> tuple[CutWorkspace, CutWorkspace, CutManifest]:
        try:
            return promote_for_render(
                workspace,
                manifest,
                scratch_directory,
                cancelled=lambda: context.cancellation.requested,
            )
        except AppError as error:
            if error.code is ErrorCode.JOB_CANCELLED:
                context.checkpoint("cut-promotion")
            raise

    def validate(self, workspace: CutWorkspace) -> CutManifest:
        return validate_cut_set(workspace)

    def detect_edits(self, workspace: CutWorkspace) -> CutManifest:
        return detect_external_edits(workspace)

    def snapshot_rebuild(
        self, workspace: CutWorkspace, scratch_directory: Path, context: JobContext
    ) -> CutWorkspace:
        try:
            return snapshot_for_rebuild(
                workspace,
                scratch_directory,
                cancelled=lambda: context.cancellation.requested,
            )
        except AppError as error:
            if error.code is ErrorCode.JOB_CANCELLED:
                context.checkpoint("cut-snapshot")
            raise

    def read_cut(
        self,
        workspace: CutWorkspace,
        index: int,
        ownership: RgbaOwnershipTracker,
    ) -> Image.Image:
        return workspace.read_promoted_cut(index, rgba_ownership_tracker=ownership)

    def discard_stage(self, workspace: CutWorkspace) -> bool:
        return discard_staged_set(workspace)

    def cleanup_job(self, output_directory: Path, job_id: str) -> bool:
        return cleanup_scratch(output_directory, job_id)

    def compare_and_set_union(
        self,
        workspace: CutWorkspace,
        expected_hashes: Sequence[str],
        metadata: CutUnionMetadata,
    ) -> bool:
        return compare_and_set_union_metadata(workspace, expected_hashes, metadata)


class PillowWebPEncoder:
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
        context.checkpoint("encode")
        if max_bytes is None:
            summary = encode_lossless_webp(
                frame_paths,
                delays_ms,
                destination,
                rgba_ownership_tracker=ownership,
            )
        else:
            try:
                fit_webp_to_size(
                    frame_paths,
                    delays_ms,
                    max_bytes,
                    work_dir,
                    destination,
                    is_cancelled=lambda: context.cancellation.requested,
                    rgba_ownership_tracker=ownership,
                )
            except AppError as error:
                if error.code is ErrorCode.JOB_CANCELLED:
                    context.checkpoint("auto-fit")
                raise
            info = validate_webp(
                destination,
                len(frame_paths),
                sum(delays_ms) if len(frame_paths) > 1 else 0,
                rgba_ownership_tracker=ownership,
            )
            summary = EncodeSummary(
                destination,
                info.width,
                info.height,
                info.frames,
                info.duration_ms,
                info.file_size,
            )
        return ValidatedCandidate.validate(
            destination,
            summary,
            ownership=ownership,
        )


class SystemDiskProbe:
    def available_bytes(self, directory: Path) -> int:
        return shutil.disk_usage(directory).free


class SystemClock:
    def time_ns(self) -> int:
        return time.time_ns()


class AtomicOutputPublisher:
    """Publish validated sibling candidates with one filesystem linearization."""

    def candidate_path(self, destination: Path, job_id: str) -> Path:
        if (
            not isinstance(destination, Path)
            or not isinstance(job_id, str)
            or not job_id
        ):
            raise TypeError("destination and job_id are required")
        return destination.parent / f".{destination.name}.{uuid.uuid4().hex}.candidate"

    def publish(
        self,
        candidate: ValidatedCandidate,
        destination: Path,
        policy: CollisionPolicy,
        *,
        cleanup_notes: list[str] | None = None,
    ) -> Path:
        if (
            not isinstance(candidate, ValidatedCandidate)
            or not isinstance(destination, Path)
            or candidate.path.parent != destination.parent
            or candidate.path == destination
            or not isinstance(policy, CollisionPolicy)
            or (cleanup_notes is not None and not isinstance(cleanup_notes, list))
        ):
            raise _output_error("candidate must be a sibling and policy must be valid")
        backup: _RollbackBackup | None = None
        primary: BaseException | None = None
        try:
            _require_candidate_current(candidate)
            if policy is CollisionPolicy.REPLACE:
                backup = _snapshot_existing_output(destination)
                if backup is not None:
                    _require_existing_output_current(destination, backup)
                _require_candidate_current(candidate)
                os.replace(candidate.path, destination)
                try:
                    _require_published_candidate(candidate, destination)
                except BaseException as error:
                    _rollback_publication(destination, backup, error)
                    backup = None
                    raise
            else:
                try:
                    os.link(candidate.path, destination, follow_symlinks=False)
                except FileExistsError as error:
                    action = (
                        "choose-another-name"
                        if policy is CollisionPolicy.CHOOSE_ANOTHER_NAME
                        else "cancelled-by-collision-policy"
                    )
                    raise AppError(
                        ErrorCode.INVALID_OUTPUT,
                        "publish",
                        "error.output.collision",
                        "output target exists at atomic no-clobber commit",
                        action,
                    ) from error
                try:
                    _require_published_candidate(candidate, destination)
                except BaseException as error:
                    _remove_failed_no_clobber_output(destination, error)
                    raise
                try:
                    candidate.path.unlink()
                except OSError as error:
                    if cleanup_notes is not None:
                        cleanup_notes.append(
                            f"additional output-candidate cleanup failure: {error}"
                        )
        except AppError as error:
            primary = error
            raise
        except OSError as error:
            wrapped = _map_output_os_error(error, "atomic output publication failed")
            primary = wrapped
            raise wrapped from error
        except BaseException as error:
            primary = error
            raise
        finally:
            if backup is not None:
                _cleanup_publication_backup(backup.path, cleanup_notes, primary)
        return destination


class PreviewService:
    def __init__(
        self,
        *,
        source: SourcePort,
        segmentation: PreparedSegmentation,
        workspace: WorkspacePort,
        clock: Clock,
    ) -> None:
        self._source = source
        self._segmentation = segmentation
        self._workspace = workspace
        self._clock = clock

    def preview(
        self, request: RenderRequest, playhead: Fraction, context: JobContext
    ) -> PreviewResult:
        started_ns = self._clock.time_ns()
        try:
            request.validate()
            self._segmentation.validate_for(request)
            source_info = self._source.probe(request.source, context)
            request.validate_for_source(
                source_info.width, source_info.height, source_info.duration
            )
            if (
                not isinstance(playhead, Fraction)
                or playhead < 0
                or playhead >= source_info.duration
            ):
                raise ValidationError(
                    ErrorCode.INVALID_SAMPLING,
                    "preview",
                    "preview playhead must be within the source duration",
                )
            identity = self._source.provisional_fingerprint(request.source, context)
            fingerprint = preview_fingerprint(
                request,
                playhead,
                source_fingerprint=identity,
                model_weight_sha256=self._segmentation.model_weight_sha256,
                rembg_version=self._segmentation.rembg_version,
                pipeline_schema_version=PIPELINE_SCHEMA_VERSION,
            )
            tracker = RgbaOwnershipTracker((request.crop.width, request.crop.height))
            cut, actual_pts = _produce_cut_frame(
                self._source,
                self._segmentation,
                request,
                playhead,
                0,
                source_info,
                context,
                tracker,
            )
            try:
                local_bounds = alpha_bounds(cut, request.framing.alpha_threshold)
                applied_bounds: PixelBounds | None = None
                exact = False
                if not request.framing.trim:
                    display = apply_framing(
                        cut,
                        FramingPlan(
                            cut.size,
                            padding=request.framing.padding,
                            stretch_x=request.framing.stretch_x,
                        ),
                    )
                else:
                    applied_bounds = self._matching_union(request, context)
                    if applied_bounds is None:
                        display = cut.copy()
                    else:
                        exact = True
                        display = apply_framing(
                            cut,
                            FramingPlan(
                                cut.size,
                                global_bounds=applied_bounds,
                                padding=request.framing.padding,
                                stretch_x=request.framing.stretch_x,
                            ),
                        )
                try:
                    result = PreviewResult(
                        fingerprint,
                        playhead,
                        actual_pts,
                        _immutable_rgba(cut),
                        _immutable_rgba(display),
                        local_bounds,
                        applied_bounds,
                        exact,
                        not request.sampling.contains(playhead),
                        max(0, self._clock.time_ns() - started_ns),
                    )
                finally:
                    display.close()
            finally:
                cut.close()
            context.commit_if_not_cancelled(lambda: None)
            return result
        except BaseException:
            _finish_failed_context(context)
            raise

    def _matching_union(
        self, request: RenderRequest, context: JobContext
    ) -> PixelBounds | None:
        try:
            source_sha = self._source.complete_sha256(request.source, context)
            inputs = cut_cache_key_inputs(
                request,
                source_sha256=source_sha,
                model_weight_sha256=self._segmentation.model_weight_sha256,
                rembg_version=self._segmentation.rembg_version,
            )
            cache_key = cut_cache_key_from_inputs(inputs)
            workspace = self._workspace.open_promoted(
                request.output.directory, cache_key
            )
            manifest = self._workspace.validate(workspace)
            metadata = manifest.union_metadata
            if (
                metadata is None
                or metadata.fingerprint != union_fingerprint(request, cut_key=cache_key)
                or metadata.alpha_threshold
                != _decimal_text(request.framing.alpha_threshold)
            ):
                return None
            return PixelBounds(*metadata.bounds)
        except AppError as error:
            if error.code in {
                ErrorCode.CUT_MANIFEST_INVALID,
                ErrorCode.CUT_SET_INVALID,
                ErrorCode.CUT_WORKSPACE_UNSAFE,
            }:
                return None
            raise


class RenderService:
    def __init__(
        self,
        *,
        source: SourcePort,
        segmentation: PreparedSegmentation,
        workspace: WorkspacePort,
        encoder: EncoderPort,
        disk_probe: DiskProbe,
        clock: Clock,
        output_publisher: OutputPublisher,
    ) -> None:
        self._source = source
        self._segmentation = segmentation
        self._workspace = workspace
        self._encoder = encoder
        self._disk_probe = disk_probe
        self._clock = clock
        self._output_publisher = output_publisher

    def render(self, request: RenderRequest, context: JobContext) -> RenderArtifact:
        staged: CutWorkspace | None = None
        scratch_owners: list[Path] = []
        primary: BaseException | None = None
        notes: list[str] = []
        try:
            request.validate()
            if request.rebuild:
                raise ValidationError(
                    ErrorCode.INVALID_RENDER_REQUEST,
                    "render",
                    "normal render cannot use a rebuild request",
                )
            self._segmentation.validate_for(request)
            source_info = self._source.probe(request.source, context)
            request.validate_for_source(
                source_info.width, source_info.height, source_info.duration
            )
            timestamps = sample_times(
                request.sampling.start,
                request.sampling.end,
                request.sampling.fps,
            )
            delays = webp_delays(len(timestamps), request.sampling.fps)
            source_sha = self._source.complete_sha256(request.source, context)
            context.checkpoint("source-hash")
            inputs = cut_cache_key_inputs(
                request,
                source_sha256=source_sha,
                model_weight_sha256=self._segmentation.model_weight_sha256,
                rembg_version=self._segmentation.rembg_version,
            )
            cache_key = cut_cache_key_from_inputs(inputs)
            tracker = RgbaOwnershipTracker((request.crop.width, request.crop.height))
            self._advisory_disk_check(request, len(timestamps), notes)
            context.checkpoint("cut-staging")
            staged = self._workspace.create_staging(
                request.output.directory, cache_key, context.job_id
            )
            frame_records: list[CutFrame] = []
            actual_pts: list[Fraction] = []
            union: PixelBounds | None = None
            for index, timestamp in enumerate(timestamps):
                context.progress(
                    "render-cut",
                    index,
                    total=len(timestamps),
                    detail=f"Cut frame {index + 1} of {len(timestamps)}",
                )
                cut, actual = _produce_cut_frame(
                    self._source,
                    self._segmentation,
                    request,
                    timestamp,
                    index,
                    source_info,
                    context,
                    tracker,
                )
                try:
                    bounds = alpha_bounds(cut, request.framing.alpha_threshold)
                    union = _union_bounds(union, bounds)
                    frame_records.append(self._workspace.stage(staged, index, cut))
                    actual_pts.append(actual)
                finally:
                    cut.close()
                    del cut
                context.checkpoint("cut-stage")
            union_metadata = (
                None
                if union is None
                else CutUnionMetadata(
                    (union.left, union.top, union.right, union.bottom),
                    _decimal_text(request.framing.alpha_threshold),
                    union_fingerprint(request, cut_key=cache_key),
                )
            )
            manifest = CutManifest.create(
                cache_key_inputs=inputs,
                source_path=str(request.source),
                source_size_bytes=source_info.revision.size,
                source_mtime_ns=source_info.revision.mtime_ns,
                frames=frame_records,
                union_metadata=union_metadata,
                now_ns=self._clock.time_ns(),
            )
            scratch = staged.scratch_root / context.job_id
            scratch_owners.append(staged.output_directory)
            durable, private, promoted_manifest = self._workspace.promote_render(
                staged, manifest, scratch, context
            )
            staged = None
            context.checkpoint("cut-promotion")
            artifact = self._encode_snapshot(
                request,
                context,
                durable,
                private,
                promoted_manifest,
                timestamps,
                tuple(actual_pts),
                delays,
                tracker,
                union,
                notes,
                tuple(scratch_owners),
                rebuilt=False,
            )
            scratch_owners.clear()
            return artifact
        except BaseException as error:
            primary = error
            _finish_failed_context(context)
            raise
        finally:
            if staged is not None:
                _cleanup_stage(self._workspace, staged, primary)
            if scratch_owners:
                _cleanup_scratch_owners(
                    self._workspace,
                    scratch_owners,
                    context.job_id,
                    primary,
                    notes,
                )

    def rebuild(
        self,
        request: RenderRequest,
        cut_workspace: CutWorkspace,
        context: JobContext,
    ) -> RenderArtifact:
        scratch_owners: list[Path] = []
        primary: BaseException | None = None
        notes: list[str] = []
        try:
            request.validate()
            if not request.rebuild or request.regenerate:
                raise ValidationError(
                    ErrorCode.INVALID_RENDER_REQUEST,
                    "rebuild",
                    "Rebuild requires rebuild=True and regenerate=False",
                )
            self._segmentation.validate_for(request)
            durable_manifest = self._workspace.detect_edits(cut_workspace)
            inputs = cut_cache_key_inputs(
                request,
                source_sha256=durable_manifest.source_sha256,
                model_weight_sha256=self._segmentation.model_weight_sha256,
                rembg_version=self._segmentation.rembg_version,
            )
            expected_key = cut_cache_key_from_inputs(inputs)
            if expected_key != cut_workspace.cache_key:
                raise AppError(
                    ErrorCode.CUT_MANIFEST_INVALID,
                    "rebuild",
                    "error.cuts.manifest-invalid",
                    "rebuild request does not match the durable cut identity",
                    "choose-matching-cuts",
                    context.job_id,
                )
            timestamps = sample_times(
                request.sampling.start,
                request.sampling.end,
                request.sampling.fps,
            )
            if durable_manifest.frame_count != len(timestamps):
                raise AppError(
                    ErrorCode.CUT_SET_INVALID,
                    "rebuild",
                    "error.cuts.invalid",
                    "cut frame count does not match the rebuild sampling grid",
                    "choose-matching-cuts",
                    context.job_id,
                )
            delays = webp_delays(len(timestamps), request.sampling.fps)
            snapshot_width = durable_manifest.width
            snapshot_height = durable_manifest.height
            worst_case = request.framing.dimensions_after_padding_and_stretch(
                snapshot_width, snapshot_height
            )
            request.framing.validate_final_dimensions(*worst_case)
            if (snapshot_width, snapshot_height) != (
                request.crop.width,
                request.crop.height,
            ):
                raise AppError(
                    ErrorCode.CUT_MANIFEST_INVALID,
                    "rebuild",
                    "error.cuts.manifest-invalid",
                    "cut dimensions do not match the rebuild crop identity",
                    "choose-matching-cuts",
                    context.job_id,
                )
            self._advisory_disk_check(request, len(timestamps), notes, rebuild=True)
            scratch = cut_workspace.scratch_root / context.job_id
            scratch_owners.append(cut_workspace.output_directory)
            private = self._workspace.snapshot_rebuild(cut_workspace, scratch, context)
            snapshot_manifest = self._workspace.validate(private)
            tracker = RgbaOwnershipTracker(
                (snapshot_manifest.width, snapshot_manifest.height)
            )
            union = self._rebuild_union(
                request,
                context,
                cut_workspace,
                private,
                snapshot_manifest,
                notes,
                tracker,
            )
            artifact = self._encode_snapshot(
                request,
                context,
                cut_workspace,
                private,
                snapshot_manifest,
                timestamps,
                None,
                delays,
                tracker,
                union,
                notes,
                tuple(scratch_owners),
                rebuilt=True,
            )
            scratch_owners.clear()
            return artifact
        except BaseException as error:
            primary = error
            _finish_failed_context(context)
            raise
        finally:
            if scratch_owners:
                _cleanup_scratch_owners(
                    self._workspace,
                    scratch_owners,
                    context.job_id,
                    primary,
                    notes,
                )

    def _rebuild_union(
        self,
        request: RenderRequest,
        context: JobContext,
        durable: CutWorkspace,
        private: CutWorkspace,
        manifest: CutManifest,
        notes: list[str],
        tracker: RgbaOwnershipTracker,
    ) -> PixelBounds | None:
        metadata = manifest.union_metadata
        expected_fingerprint = union_fingerprint(request, cut_key=manifest.cache_key)
        if (
            metadata is not None
            and metadata.fingerprint == expected_fingerprint
            and metadata.alpha_threshold
            == _decimal_text(request.framing.alpha_threshold)
        ):
            return PixelBounds(*metadata.bounds)
        union: PixelBounds | None = None
        for index in range(manifest.frame_count):
            context.checkpoint("rebuild-union")
            image = self._workspace.read_cut(private, index, tracker)
            try:
                union = _union_bounds(
                    union,
                    alpha_bounds(image, request.framing.alpha_threshold),
                )
            finally:
                image.close()
                del image
        if union is not None:
            new_metadata = CutUnionMetadata(
                (union.left, union.top, union.right, union.bottom),
                _decimal_text(request.framing.alpha_threshold),
                expected_fingerprint,
            )
            try:
                won = self._workspace.compare_and_set_union(
                    durable,
                    tuple(frame.sha256 for frame in manifest.frames),
                    new_metadata,
                )
                if not won:
                    notes.append("union metadata CAS lost; rebuild used private result")
            except AppError as error:
                notes.append(f"union metadata cache update skipped: {error}")
        return union

    def _encode_snapshot(
        self,
        request: RenderRequest,
        context: JobContext,
        durable: CutWorkspace,
        private: CutWorkspace,
        manifest: CutManifest,
        timestamps: tuple[Fraction, ...],
        actual_pts: tuple[Fraction, ...] | None,
        delays: tuple[int, ...],
        tracker: RgbaOwnershipTracker,
        union: PixelBounds | None,
        notes: list[str],
        scratch_owners: tuple[Path, ...],
        *,
        rebuilt: bool,
    ) -> RenderArtifact:
        if request.framing.trim and union is None:
            raise ValidationError(
                ErrorCode.INVALID_FRAMING,
                "framing",
                "range-wide alpha union contains no visible pixels at this threshold",
            )
        plan = FramingPlan(
            (manifest.width, manifest.height),
            global_bounds=union if request.framing.trim else None,
            padding=request.framing.padding,
            stretch_x=request.framing.stretch_x,
        )
        tracker.include_size(plan.output_size)
        scratch = private.path.parent
        framed_directory = scratch / "framed-inputs"
        try:
            framed_directory.mkdir(exist_ok=False)
        except OSError as error:
            raise _map_output_os_error(
                error, "cannot create framed input directory"
            ) from error
        framed_paths: list[Path] = []
        for index in range(manifest.frame_count):
            context.checkpoint("framing")
            cut = self._workspace.read_cut(private, index, tracker)
            try:
                framed = apply_framing(cut, plan)
                tracker.register(framed)
            finally:
                cut.close()
                del cut
            try:
                path = framed_directory / f"frame-{index:06d}.png"
                _persist_framed_png(path, framed)
                framed_paths.append(path)
            finally:
                framed.close()
                del framed
            context.checkpoint("framing")
        candidate = self._output_publisher.candidate_path(
            request.output.path, context.job_id
        )
        artifact_fingerprint = render_fingerprint(request, cut_key=manifest.cache_key)
        summary: EncodeSummary | None = None
        validated: ValidatedCandidate | None = None
        published = False
        publish_error: BaseException | None = None
        try:
            validated = self._encoder.encode(
                tuple(framed_paths),
                delays,
                candidate,
                work_dir=scratch,
                max_bytes=request.output.max_bytes,
                context=context,
                ownership=tracker,
            )
            summary = validated.summary
            if validated.path != candidate or summary.destination != candidate:
                raise _output_error("encoder did not return its private candidate")
            context.checkpoint("encode")
            gc.collect()
            ownership_peak = tracker.peak
            ownership_current = tracker.current
            output_path = context.commit_if_not_cancelled(
                lambda: self._output_publisher.publish(
                    validated,
                    request.output.path,
                    request.output.collision_policy,
                    cleanup_notes=notes,
                )
            )
            published = True
            _cleanup_scratch_owners(
                self._workspace,
                scratch_owners,
                context.job_id,
                None,
                notes,
            )
        except BaseException as error:
            publish_error = error
            raise
        finally:
            if validated is not None:
                _close_validated_candidate(validated, publish_error, notes)
            if not published:
                _cleanup_candidate(candidate, publish_error, notes)
        assert summary is not None
        return RenderArtifact(
            output_path,
            artifact_fingerprint,
            durable,
            manifest,
            manifest.frame_count,
            summary.width,
            summary.height,
            summary.file_size,
            summary.duration_ms,
            timestamps,
            actual_pts,
            delays,
            ownership_peak,
            ownership_current,
            rebuilt,
            tuple(notes),
        )

    def _advisory_disk_check(
        self,
        request: RenderRequest,
        frame_count: int,
        notes: list[str],
        *,
        rebuild: bool = False,
    ) -> None:
        input_pixels = request.crop.width * request.crop.height * frame_count
        encoded_width, encoded_height = (
            request.framing.dimensions_after_padding_and_stretch(
                request.crop.width, request.crop.height
            )
        )
        encoded_pixels = encoded_width * encoded_height * frame_count
        cut_stage_bytes = (input_pixels * 4 * 110 + 99) // 100
        render_stage_bytes = (encoded_pixels * 4 * 110 + 99) // 100
        webp_upper_bytes = (encoded_pixels * 410 + 99) // 100
        snapshot_bytes = cut_stage_bytes if rebuild else 0
        estimate = (
            cut_stage_bytes
            + render_stage_bytes
            + 2 * webp_upper_bytes
            + snapshot_bytes
            + 512 * 1024 * 1024
        )
        try:
            available = self._disk_probe.available_bytes(request.output.directory)
        except OSError as error:
            notes.append(f"disk preflight unavailable: {error}")
            return
        if available < estimate:
            notes.append(
                f"advisory disk estimate {estimate} exceeds available {available}"
            )


def _produce_cut_frame(
    source: SourcePort,
    segmentation: PreparedSegmentation,
    request: RenderRequest,
    timestamp: Fraction,
    request_id: int,
    source_info: SourceInfo,
    context: JobContext,
    tracker: RgbaOwnershipTracker,
) -> tuple[Image.Image, Fraction]:
    """The exact shared decode/orient/crop -> segment -> edge-cleanup pipeline."""
    decoded = source.decode(
        request.source,
        timestamp,
        request_id,
        source_info,
        context,
        tracker,
    )
    actual_pts = decoded.actual_pts
    decoded_image = decoded.image
    try:
        context.checkpoint("decode")
        crop_bounds = PixelBounds.from_xywh(
            request.crop.x,
            request.crop.y,
            request.crop.width,
            request.crop.height,
        )
        cropped = apply_source_crop(decoded_image, crop_bounds)
        tracker.register(cropped)
    finally:
        decoded_image.close()
        del decoded, decoded_image
    try:
        input_frame = np.ascontiguousarray(np.asarray(cropped, dtype=np.uint8))
        tracker.register(input_frame)
        options = _segment_options(request)
        wire_request = SegmentRequest(
            PROTOCOL_VERSION,
            context.job_id,
            f"{context.job_id}:{request_id}",
            options=options,
        )
        try:
            segmented = segmentation.port.segment(input_frame, wire_request)
        except AppError:
            raise
        finally:
            del input_frame
    finally:
        cropped.close()
        del cropped
    context.checkpoint("segmentation")
    if (
        not isinstance(segmented, np.ndarray)
        or segmented.dtype != np.dtype(np.uint8)
        or segmented.shape != (request.crop.height, request.crop.width, 4)
        or not segmented.flags.c_contiguous
    ):
        raise AppError(
            ErrorCode.SEGMENTATION_PROTOCOL_MISMATCH,
            "segmentation",
            "error.segmentation.protocol-mismatch",
            "segmentation result is not exact contiguous crop-sized RGBA",
            "restart-segmentation-process",
            context.job_id,
        )
    tracker.register(segmented)
    if request.segmentation.edge_mode is EdgeMode.DECONTAMINATE_COLORS:
        _decontaminate_edge_colors_in_place(segmented)
    result = Image.frombytes(
        "RGBA",
        (request.crop.width, request.crop.height),
        segmented.tobytes(order="C"),
    )
    tracker.register(result)
    del segmented
    return result, actual_pts


def _decontaminate_edge_colors_in_place(rgba: np.ndarray) -> None:
    """Remove black-premultiplication from translucent edge RGB deterministically.

    Alpha is unchanged. Fully transparent pixels receive canonical black RGB;
    for ``0 < alpha < 255`` each channel is converted from black-premultiplied
    to straight color with integer half-up rounding and saturation. This is a
    local postprocess and never aliases rembg's ``post_process_mask`` behavior.
    """
    alpha = rgba[..., 3]
    rgba[alpha == 0, :3] = 0
    partial = (alpha > 0) & (alpha < 255)
    if not np.any(partial):
        return
    partial_alpha = alpha[partial].astype(np.uint32)
    colors = rgba[partial, :3].astype(np.uint32)
    straight = np.minimum(
        255,
        (colors * 255 + partial_alpha[:, None] // 2) // partial_alpha[:, None],
    )
    rgba[partial, :3] = straight.astype(np.uint8)


def _segment_options(request: RenderRequest) -> SegmentOptions:
    matting = request.segmentation.alpha_matting
    return SegmentOptions(
        catalog_edge_mode(request.segmentation.edge_mode),
        matting.foreground_threshold,
        matting.background_threshold,
        matting.erode_size,
    )


def _immutable_rgba(image: Image.Image) -> ImmutableRgba:
    return ImmutableRgba(image.width, image.height, bytes(image.tobytes()))


def _union_bounds(
    current: PixelBounds | None, added: PixelBounds | None
) -> PixelBounds | None:
    if added is None:
        return current
    if current is None:
        return added
    return PixelBounds(
        min(current.left, added.left),
        min(current.top, added.top),
        max(current.right, added.right),
        max(current.bottom, added.bottom),
    )


def _persist_framed_png(path: Path, image: Image.Image) -> None:
    temporary: Path | None = None
    primary: BaseException | None = None
    try:
        descriptor, raw = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        os.close(descriptor)
        temporary = Path(raw)
        image.save(temporary, format="PNG")
        with temporary.open("rb") as encoded:
            os.fsync(encoded.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as error:
        wrapped = _map_output_os_error(error, "cannot persist framed PNG")
        primary = wrapped
        raise wrapped from error
    except ValueError as error:
        wrapped = _output_error(f"cannot persist framed PNG: {error}")
        primary = wrapped
        raise wrapped from error
    except BaseException as error:
        primary = error
        raise
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError as error:
                if primary is not None:
                    primary.add_note(f"additional framed-PNG cleanup failure: {error}")


def _cleanup_stage(
    workspace: WorkspacePort,
    staged: CutWorkspace,
    primary: BaseException | None,
) -> None:
    try:
        workspace.discard_stage(staged)
    except BaseException as error:
        if primary is not None:
            primary.add_note(f"additional staged-cut cleanup failure: {error}")


def _cleanup_job(
    workspace: WorkspacePort,
    output_directory: Path,
    job_id: str,
    primary: BaseException | None,
    notes: list[str],
) -> None:
    try:
        workspace.cleanup_job(output_directory, job_id)
    except BaseException as error:
        detail = f"additional scratch cleanup failure: {error}"
        if primary is not None:
            primary.add_note(detail)
        else:
            notes.append(detail)


def _cleanup_scratch_owners(
    workspace: WorkspacePort,
    output_directories: Sequence[Path],
    job_id: str,
    primary: BaseException | None,
    notes: list[str],
) -> None:
    seen: set[Path] = set()
    for output_directory in output_directories:
        if output_directory in seen:
            continue
        seen.add(output_directory)
        _cleanup_job(workspace, output_directory, job_id, primary, notes)


@dataclass(frozen=True, slots=True)
class _RollbackBackup:
    path: Path
    source_identity: CandidateFileIdentity
    sha256: str


def _open_held_file(path: Path) -> int:
    """Open *path* without following a final symlink and allow rename on Windows."""
    if os.name != "nt":
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        return os.open(path, flags)

    ctypes = importlib.import_module("ctypes")
    msvcrt = importlib.import_module("msvcrt")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in {None, invalid_handle}:
        error_number = ctypes.get_last_error()
        raise OSError(error_number, os.strerror(error_number), path)
    try:
        return cast(
            int,
            msvcrt.open_osfhandle(
                handle,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            ),
        )
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _candidate_identity(info: os.stat_result) -> CandidateFileIdentity:
    return CandidateFileIdentity(
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
    )


def _path_identity(path: Path) -> CandidateFileIdentity:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise _output_error("output path must be a regular file")
    return _candidate_identity(info)


def _require_regular_nonempty(identity: CandidateFileIdentity) -> None:
    if identity.size < 1:
        raise _output_error("validated output candidate must be non-empty")


def _sha256_descriptor(descriptor: int) -> str:
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    digest = hashlib.sha256()
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)
    return digest.hexdigest()


def _require_candidate_current(candidate: ValidatedCandidate) -> None:
    descriptor_identity = _candidate_identity(os.fstat(candidate._descriptor))
    if (
        descriptor_identity != candidate.identity
        or _path_identity(candidate.path) != candidate.identity
        or _sha256_descriptor(candidate._descriptor) != candidate.sha256
    ):
        raise _output_error("validated output candidate changed before publication")


def _validate_held_candidate_webp(
    path: Path,
    descriptor: int,
    identity: CandidateFileIdentity,
    summary: EncodeSummary,
    ownership: RgbaOwnershipTracker | None,
) -> WebPInfo:
    """Validate a private hard link proven to name the held file description."""
    validation_link = path.parent / f".rembggui-{uuid.uuid4().hex}.validation"
    primary: BaseException | None = None
    linked = False
    try:
        os.link(path, validation_link, follow_symlinks=False)
        linked = True
        if (
            _path_identity(validation_link) != identity
            or _candidate_identity(os.fstat(descriptor)) != identity
        ):
            raise _output_error("candidate changed while binding WebP validation")
        return validate_webp(
            validation_link,
            summary.frames,
            summary.duration_ms,
            rgba_ownership_tracker=ownership,
        )
    except BaseException as error:
        primary = error
        raise
    finally:
        if linked:
            try:
                validation_link.unlink()
            except OSError as error:
                detail = f"additional candidate-validation cleanup failure: {error}"
                if primary is not None:
                    primary.add_note(detail)
                else:
                    raise _map_output_os_error(error, detail) from error


def _require_published_candidate(
    candidate: ValidatedCandidate, destination: Path
) -> None:
    descriptor_identity = _candidate_identity(os.fstat(candidate._descriptor))
    if (
        descriptor_identity != candidate.identity
        or _sha256_descriptor(candidate._descriptor) != candidate.sha256
    ):
        raise _output_error("validated candidate bytes changed during publication")
    published_descriptor = _open_held_file(destination)
    try:
        published_identity = _candidate_identity(os.fstat(published_descriptor))
        if (
            published_identity != candidate.identity
            or _path_identity(destination) != candidate.identity
            or _sha256_descriptor(published_descriptor) != candidate.sha256
        ):
            raise _output_error("published output is not the validated candidate")
    finally:
        os.close(published_descriptor)


def _copy_descriptor(source: int, destination: int) -> None:
    source_position = os.lseek(source, 0, os.SEEK_CUR)
    try:
        os.lseek(source, 0, os.SEEK_SET)
        while chunk := os.read(source, 1024 * 1024):
            view = memoryview(chunk)
            while view:
                written = os.write(destination, view)
                if written < 1:
                    raise OSError(errno.EIO, "short rollback-backup write")
                view = view[written:]
    finally:
        os.lseek(source, source_position, os.SEEK_SET)


def _snapshot_existing_output(destination: Path) -> _RollbackBackup | None:
    try:
        source = _open_held_file(destination)
    except FileNotFoundError:
        return None
    backup: Path | None = None
    backup_descriptor: int | None = None
    try:
        identity = _candidate_identity(os.fstat(source))
        if _path_identity(destination) != identity:
            raise _output_error("existing output changed before publication")
        digest = _sha256_descriptor(source)
        backup_descriptor, raw_backup = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".rollback",
            dir=destination.parent,
        )
        backup = Path(raw_backup)
        _copy_descriptor(source, backup_descriptor)
        os.fsync(backup_descriptor)
        if (
            _candidate_identity(os.fstat(source)) != identity
            or _path_identity(destination) != identity
            or _sha256_descriptor(source) != digest
        ):
            raise _output_error("existing output changed while preparing rollback")
        return _RollbackBackup(backup, identity, digest)
    except BaseException:
        if backup is not None:
            try:
                backup.unlink()
            except OSError:
                pass
        raise
    finally:
        if backup_descriptor is not None:
            os.close(backup_descriptor)
        os.close(source)


def _require_existing_output_current(
    destination: Path, backup: _RollbackBackup
) -> None:
    current = _open_held_file(destination)
    try:
        if (
            _candidate_identity(os.fstat(current)) != backup.source_identity
            or _path_identity(destination) != backup.source_identity
            or _sha256_descriptor(current) != backup.sha256
        ):
            raise _output_error("existing output changed before atomic publication")
    finally:
        os.close(current)


def _rollback_publication(
    destination: Path,
    backup: _RollbackBackup | None,
    primary: BaseException,
) -> None:
    try:
        if backup is None:
            destination.unlink()
            return
        os.replace(backup.path, destination)
        restored = _open_held_file(destination)
        try:
            if _sha256_descriptor(restored) != backup.sha256:
                raise _output_error("rollback restored different output bytes")
        finally:
            os.close(restored)
    except FileNotFoundError:
        if backup is not None:
            primary.add_note("additional output rollback failure: output disappeared")
    except BaseException as error:
        primary.add_note(f"additional output rollback failure: {error}")


def _remove_failed_no_clobber_output(destination: Path, primary: BaseException) -> None:
    try:
        destination.unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        primary.add_note(f"additional failed-publication cleanup failure: {error}")


def _cleanup_publication_backup(
    backup: Path,
    cleanup_notes: list[str] | None,
    primary: BaseException | None,
) -> None:
    try:
        backup.unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        detail = f"additional output rollback-backup cleanup failure: {error}"
        if primary is not None:
            primary.add_note(detail)
        elif cleanup_notes is not None:
            cleanup_notes.append(detail)


def _cleanup_candidate(
    candidate: Path, primary: BaseException | None, notes: list[str]
) -> None:
    try:
        candidate.unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        detail = f"additional output-candidate cleanup failure: {error}"
        if primary is not None:
            primary.add_note(detail)
        else:
            notes.append(detail)


def _close_validated_candidate(
    candidate: ValidatedCandidate,
    primary: BaseException | None,
    notes: list[str],
) -> None:
    try:
        candidate.close()
    except OSError as error:
        detail = f"additional validated-candidate handle cleanup failure: {error}"
        if primary is not None:
            primary.add_note(detail)
        else:
            notes.append(detail)


def _finish_failed_context(context: JobContext) -> None:
    if context.terminal_state is JobTerminalState.RUNNING:
        context.fail()
    elif context.terminal_state is JobTerminalState.CANCEL_PENDING:
        try:
            context.checkpoint("job-cleanup")
        except AppError as error:
            if error.code is not ErrorCode.JOB_CANCELLED:
                raise


def _decimal_text(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    return format(value, "f").rstrip("0").rstrip(".")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _map_output_os_error(error: OSError, detail: str) -> AppError:
    if error.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}:
        suffix = "disk quota or free space exhausted"
        action = "free-disk-space"
    elif error.errno in {errno.EACCES, errno.EPERM}:
        suffix = "output location is not writable"
        action = "choose-writable-output"
    elif error.errno == getattr(errno, "EROFS", -1):
        suffix = "output filesystem is read-only"
        action = "choose-writable-output"
    elif error.errno == errno.EEXIST:
        suffix = "output target already exists"
        action = "choose-collision-policy"
    else:
        suffix = f"{type(error).__name__}: {error}"
        action = "retry-output"
    return AppError(
        ErrorCode.INVALID_OUTPUT,
        "output",
        "error.output.failed",
        f"{detail}: {suffix}",
        action,
    )


def _output_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.INVALID_OUTPUT,
        "output",
        "error.output.failed",
        detail,
        "retry-output",
    )
