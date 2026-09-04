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
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from fractions import Fraction
from functools import partial
from pathlib import Path
from typing import Protocol, cast

import numpy as np
from PIL import Image

from matteloop.core.errors import AppError, ErrorCode, ValidationError
from matteloop.core.fingerprints import (
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
from matteloop.core.geometry import (
    FramingPlan,
    PixelBounds,
    alpha_bounds,
    apply_framing,
    apply_source_crop,
)
from matteloop.core.rgba import RgbaOwnershipTracker
from matteloop.core.specs import (
    CollisionPolicy,
    EdgeMode,
    RenderRequest,
    catalog_edge_mode,
)
from matteloop.core.timebase import sample_times, webp_delays
from matteloop.core.webp import (
    EncodeSummary,
    WebPInfo,
    encode_lossless_webp,
    validate_webp,
)
from matteloop.jobs.context import JobContext, JobTerminalState
from matteloop.jobs.encoding import (
    _map_output_os_error,
    _output_error,
    auto_fit_progress,
    auto_fit_webp,
)
from matteloop.jobs.models.cache_fs import BoundDirectoryCloseError, UnsafeCacheError
from matteloop.jobs.protocol import PROTOCOL_VERSION, SegmentOptions, SegmentRequest
from matteloop.jobs.source import DecodedFrame, SourceInfo, decode_frame, probe_source
from matteloop.jobs.transform_stage import framing_plan, stage_encoder_frames
from matteloop.jobs.transform_store import store_transform
from matteloop.jobs.workspace import (
    AdvisoryFileLock,
    CutFrame,
    CutManifest,
    CutUnionMetadata,
    CutWorkspace,
    LockedSlotFile,
    PublicationDirectory,
    RecoveryDirectory,
    cleanup_scratch,
    compare_and_set_union_metadata,
    detect_external_edits,
    discard_staged_set,
    promote_for_render,
    snapshot_for_rebuild,
    stage_cut,
    transfer_deferred_bound_directory_closes,
    validate_cut_set,
)
from matteloop.jobs.workspace_names import readable_workspace_name


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
        self,
        output_directory: Path,
        cache_key: str,
        job_id: str,
        directory_name: str | None = None,
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
    def candidate_path(
        self, destination: Path, job_id: str, work_dir: Path
    ) -> Path: ...

    def publish(
        self,
        candidate: ValidatedCandidate,
        destination: Path,
        policy: CollisionPolicy,
        *,
        cleanup_notes: list[str] | None = None,
    ) -> Path: ...


def _overall_budget(
    timestamp_count: int, kept_count: int, *, rebuilt: bool
) -> tuple[int, int]:
    post_process_and_encode = 2 * kept_count
    total = (
        post_process_and_encode
        if rebuilt
        else timestamp_count + post_process_and_encode
    )
    return (0 if rebuilt else timestamp_count, total)


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
                descriptor,
                summary,
                ownership,
            )
            if not _descriptor_path_matches(path, descriptor, before, digest):
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
        self,
        output_directory: Path,
        cache_key: str,
        job_id: str,
        directory_name: str | None = None,
    ) -> CutWorkspace:
        return CutWorkspace.create_staging(
            output_directory, cache_key, job_id, directory_name
        )

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


def find_matching_cut_workspace(
    source: SourcePort,
    workspace: WorkspacePort,
    request: RenderRequest,
    *,
    model_weight_sha256: str,
    rembg_version: str,
    context: JobContext,
) -> CutWorkspace | None:
    """Find one validated workspace whose manifest matches every cut input."""
    source_sha = source.complete_sha256(request.source, context)
    context.checkpoint("source-hash")
    inputs = cut_cache_key_inputs(
        request,
        source_sha256=source_sha,
        model_weight_sha256=model_weight_sha256,
        rembg_version=rembg_version,
    )
    cache_key = cut_cache_key_from_inputs(inputs)
    try:
        candidate = workspace.open_promoted(request.output.directory, cache_key)
        manifest = workspace.validate(candidate)
    except AppError as error:
        if error.code in {
            ErrorCode.CUT_MANIFEST_INVALID,
            ErrorCode.CUT_SET_INVALID,
            ErrorCode.CUT_WORKSPACE_UNSAFE,
        }:
            return None
        raise
    return candidate if manifest.cache_key == cache_key else None


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
        stage = "Auto-fit" if max_bytes is not None else "Encode"
        overall = None if max_bytes is not None else context.overall_progress
        if max_bytes is None:
            frame_progress = cast(
                Callable[[int, int], None],
                partial(context.frame_progress, stage, overall=overall),
            )
            summary = encode_lossless_webp(
                frame_paths,
                delays_ms,
                destination,
                rgba_ownership_tracker=ownership,
                progress=frame_progress,
            )
        else:
            frame_progress, attempt_progress = auto_fit_progress(
                context, len(frame_paths)
            )
            summary = auto_fit_webp(
                frame_paths,
                delays_ms,
                destination,
                work_dir,
                max_bytes,
                context,
                ownership,
                frame_progress,
                attempt_progress,
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
    """Publish validated private candidates with one filesystem linearization."""

    def candidate_path(self, destination: Path, job_id: str, work_dir: Path) -> Path:
        if (
            not isinstance(destination, Path)
            or not isinstance(job_id, str)
            or not job_id
            or not isinstance(work_dir, Path)
        ):
            raise TypeError("destination, job_id, and work_dir are required")
        if (
            job_id in {".", ".."}
            or Path(job_id).name != job_id
            or work_dir.name != job_id
        ):
            raise ValueError("candidate work directory must be job-owned")
        return work_dir / "output.candidate.webp"

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
            or candidate.path == destination
            or not isinstance(policy, CollisionPolicy)
            or (cleanup_notes is not None and not isinstance(cleanup_notes, list))
        ):
            raise _output_error(
                "candidate, distinct destination, and collision policy must be valid"
            )
        previous: _HeldPreviousOutput | None = None
        recovery: _HeldRecoveryFile | None = None
        staged: _HeldPublicationFile | None = None
        transaction: _HeldOutputTransaction | None = None
        publication: PublicationDirectory | None = None
        primary: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        try:
            publication = PublicationDirectory.open(destination.parent)
            destination_name = publication.name_for(destination)
            publication.assert_still_bound()
            if not _descriptor_matches(
                candidate._descriptor, candidate.identity, candidate.sha256
            ):
                raise _output_error("validated output candidate changed")
            transaction = _acquire_output_transaction(publication, destination)
            transaction.assert_owned()
            if policy is CollisionPolicy.REPLACE:
                previous = _snapshot_existing_output(publication, destination_name)
                if previous is not None:
                    recovery = _prepare_durable_recovery(
                        publication,
                        destination_name,
                        previous,
                        transaction,
                    )
                    _require_existing_output_current(
                        publication,
                        destination_name,
                        previous,
                    )
                if not _descriptor_matches(
                    candidate._descriptor, candidate.identity, candidate.sha256
                ):
                    raise _output_error("validated output candidate changed")
                publication.assert_still_bound()
                staged = _prepare_publication_stage(
                    candidate,
                    publication,
                    destination,
                    transaction,
                )
                try:
                    transaction.assert_owned()
                    publication.replace_from(
                        staged.directory, staged.name, destination_name
                    )
                    staged.directory.fsync()
                    publication.fsync()
                    if not _published_file_matches(
                        publication,
                        destination_name,
                        staged.identity,
                        staged.sha256,
                    ):
                        raise _output_error(
                            "published output is not the validated candidate"
                        )
                    transaction.assert_owned()
                    publication.assert_still_bound()
                except BaseException as error:
                    _rollback_publication(
                        publication,
                        destination,
                        destination_name,
                        previous,
                        recovery,
                        transaction,
                        error,
                    )
                    raise
            else:
                staged = _prepare_publication_stage(
                    candidate,
                    publication,
                    destination,
                    transaction,
                )
                publication.assert_still_bound()
                try:
                    transaction.assert_owned()
                    _rename_no_replace(
                        staged.directory,
                        staged.name,
                        publication,
                        destination_name,
                    )
                    staged.directory.fsync()
                    publication.fsync()
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
                    if not _descriptor_bound_entry_matches(
                        publication,
                        destination_name,
                        staged.descriptor,
                        staged.identity,
                        staged.sha256,
                    ):
                        raise _output_error(
                            "no-clobber output changed after atomic reservation"
                        )
                except BaseException:
                    # A mismatched destination may now belong to a concurrent
                    # writer. Never unlink it by pathname.
                    raise
                publication.assert_still_bound()
                transaction.assert_owned()
        except AppError as error:
            if error.code is ErrorCode.CUT_WORKSPACE_UNSAFE:
                wrapped = _output_error(
                    f"unsafe output publication namespace: {error.technical_detail}"
                )
                for note in getattr(error, "__notes__", ()):
                    wrapped.add_note(note)
                transfer_deferred_bound_directory_closes(error, wrapped)
                primary = wrapped
                raise wrapped from error
            primary = error
            raise
        except (UnsafeCacheError, BoundDirectoryCloseError) as error:
            wrapped = _output_error(f"unsafe output publication operation: {error}")
            transfer_deferred_bound_directory_closes(error, wrapped)
            for note in getattr(error, "__notes__", ()):
                wrapped.add_note(note)
            primary = wrapped
            raise wrapped from error
        except OSError as error:
            wrapped = _map_output_os_error(error, "atomic output publication failed")
            transfer_deferred_bound_directory_closes(error, wrapped)
            for note in getattr(error, "__notes__", ()):
                wrapped.add_note(note)
            primary = wrapped
            raise wrapped from error
        except BaseException as error:
            primary = error
            raise
        finally:
            if staged is not None:
                _close_staged_file(staged, cleanup_notes, primary, cleanup_errors)
            if recovery is not None:
                _close_recovery_file(recovery, cleanup_notes, primary, cleanup_errors)
            if previous is not None:
                _close_previous_output(previous, cleanup_notes, primary)
            if transaction is not None:
                _close_output_transaction(
                    transaction, cleanup_notes, primary, cleanup_errors
                )
            if publication is not None:
                _close_publication_directory(
                    publication, cleanup_notes, primary, cleanup_errors
                )
            if primary is None and cleanup_errors:
                wrapped = _output_error(
                    "output publication completed but bound resources could not close"
                )
                for cleanup_error in cleanup_errors:
                    transfer_deferred_bound_directory_closes(cleanup_error, wrapped)
                    wrapped.add_note(f"publication cleanup failure: {cleanup_error}")
                raise wrapped from cleanup_errors[0]
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
            if not request.regenerate:
                reusable = find_matching_cut_workspace(
                    self._source,
                    self._workspace,
                    request,
                    model_weight_sha256=self._segmentation.model_weight_sha256,
                    rembg_version=self._segmentation.rembg_version,
                    context=context,
                )
                if reusable is not None:
                    return self.rebuild(
                        replace(request, rebuild=True), reusable, context
                    )
            source_info = self._source.probe(request.source, context)
            request.validate_for_source(
                source_info.width, source_info.height, source_info.duration
            )
            timestamps = sample_times(
                request.sampling.start,
                request.sampling.end,
                request.sampling.fps,
            )
            timestamp_count = len(timestamps)
            kept_count = len(request.transform.kept_range(timestamp_count))
            overall_start, overall_total = _overall_budget(
                timestamp_count, kept_count, rebuilt=False
            )
            delays = webp_delays(timestamp_count, request.sampling.fps)
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
                request.output.directory,
                cache_key,
                context.job_id,
                readable_workspace_name(request.source, cache_key),
            )
            frame_records: list[CutFrame] = []
            actual_pts: list[Fraction] = []
            union: PixelBounds | None = None
            for index, timestamp in enumerate(timestamps):
                context.set_frame_context(
                    index + 1,
                    timestamp_count,
                    overall=(index, overall_total),
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
                    gc.collect(0)
                context.checkpoint("cut-stage")
                context.progress(
                    "render-cut",
                    index + 1,
                    total=timestamp_count,
                    detail=f"Cut frame {index + 1} of {len(timestamps)}",
                    overall_completed=index + 1,
                    overall_total=overall_total,
                )
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
            context.progress("Cut promotion", 0, detail="Promoting cut frames")
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
                overall=(overall_start, overall_total),
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
            context.progress("Validation", 0, detail="Validating cut set")
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
            timestamp_count = len(timestamps)
            kept_count = len(request.transform.kept_range(timestamp_count))
            overall_start, overall_total = _overall_budget(
                timestamp_count, kept_count, rebuilt=True
            )
            delays = webp_delays(timestamp_count, request.sampling.fps)
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
            context.progress("Validation", 0, detail="Validating cut snapshot")
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
                overall=(overall_start, overall_total),
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
        overall: tuple[int, int],
        rebuilt: bool,
    ) -> RenderArtifact:
        plan = framing_plan((manifest.width, manifest.height), union, request.framing)
        tracker.include_size(plan.output_size)
        scratch = private.path.parent
        framed_paths, delays = stage_encoder_frames(
            partial(self._workspace.read_cut, private),
            manifest.frame_count,
            plan,
            request.transform,
            delays,
            scratch / "framed-inputs",
            tracker,
            context,
            overall=overall,
        )
        candidate = self._output_publisher.candidate_path(
            request.output.path, context.job_id, scratch
        )
        artifact_fingerprint = render_fingerprint(request, cut_key=manifest.cache_key)
        summary: EncodeSummary | None = None
        validated: ValidatedCandidate | None = None
        published = False
        publish_error: BaseException | None = None
        try:
            frame_count = len(framed_paths)
            overall = context.overall_progress or (0, frame_count)
            stage = "Auto-fit" if request.output.max_bytes is not None else "Encode"
            overall_for_encode = (
                None if request.output.max_bytes is not None else overall
            )
            context.frame_progress(
                stage,
                0,
                frame_count,
                overall=overall_for_encode,
                overall_indeterminate=request.output.max_bytes is not None,
            )
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
            context.frame_progress(
                stage,
                frame_count,
                frame_count,
                overall=overall_for_encode,
                overall_indeterminate=request.output.max_bytes is not None,
            )
            context.progress("Validation", 0, detail="Validating encoded output")
            context.checkpoint("encode")
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
        except BaseException as error:
            publish_error = error
            raise
        finally:
            if validated is not None:
                _close_validated_candidate(validated, publish_error, notes)
        if published:
            _cleanup_scratch_owners(
                self._workspace,
                scratch_owners,
                context.job_id,
                None,
                notes,
            )
            store_transform(durable, request.transform, notes)
        assert summary is not None
        return RenderArtifact(
            output_path,
            artifact_fingerprint,
            durable,
            manifest,
            len(framed_paths),
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
class _HeldPreviousOutput:
    descriptor: int = field(repr=False, compare=False)
    identity: CandidateFileIdentity
    sha256: str


@dataclass(frozen=True, slots=True)
class _HeldPublicationFile:
    directory: RecoveryDirectory = field(repr=False, compare=False)
    name: str
    slot: LockedSlotFile = field(repr=False, compare=False)
    identity: CandidateFileIdentity
    sha256: str
    owns_directory: bool = field(default=True, repr=False, compare=False)

    @property
    def descriptor(self) -> int:
        return self.slot.descriptor

    @property
    def path(self) -> Path:
        return self.directory.path_for(self.name)


@dataclass(frozen=True, slots=True)
class _HeldRecoveryFile:
    directory: RecoveryDirectory = field(repr=False, compare=False)
    name: str
    shadow_name: str
    slot: LockedSlotFile = field(repr=False, compare=False)
    shadow_slot: LockedSlotFile | None = field(repr=False, compare=False)
    identity: CandidateFileIdentity
    sha256: str

    @property
    def descriptor(self) -> int:
        return self.slot.descriptor

    @property
    def path(self) -> Path:
        return self.directory.path_for(self.name)

    @property
    def shadow_path(self) -> Path:
        return self.directory.path_for(self.shadow_name)


@dataclass(frozen=True, slots=True)
class _HeldOutputTransaction:
    directory: RecoveryDirectory = field(repr=False, compare=False)
    lock: AdvisoryFileLock = field(repr=False, compare=False)
    target_key: str
    destination_name: str

    def assert_owned(self) -> None:
        self.lock.assert_owned()


_MAX_ROLLBACK_RESTORE_ATTEMPTS = 4
_RECOVERY_DIRECTORY_NAME = ".matteloop-recovery"
_PUBLICATION_DIRECTORY_NAME = ".matteloop-publish"


def _acquire_output_transaction(
    publication: PublicationDirectory,
    destination: Path,
) -> _HeldOutputTransaction:
    directory = publication.open_private_directory(
        _PUBLICATION_DIRECTORY_NAME,
        "publication",
    )
    destination_name = publication.name_for(destination)
    target_key = publication.target_key(destination)
    try:
        lock = publication.acquire_output_lock(directory, target_key)
        return _HeldOutputTransaction(directory, lock, target_key, destination_name)
    except BlockingIOError as error:
        wrapped = _output_error("output transaction is already active")
        transfer_deferred_bound_directory_closes(error, wrapped)
        for note in getattr(error, "__notes__", ()):
            wrapped.add_note(note)
        try:
            directory.close(wrapped)
        except BaseException as close_error:
            wrapped.add_note(
                f"additional transaction-directory cleanup failure: {close_error}"
            )
        raise wrapped from error
    except BaseException as error:
        try:
            directory.close(error)
        except BaseException as close_error:
            error.add_note(
                f"additional transaction-directory cleanup failure: {close_error}"
            )
        raise


def _rename_no_replace(
    source_directory: RecoveryDirectory,
    source: str,
    destination_directory: PublicationDirectory,
    destination: str,
) -> None:
    """Atomically consume a bound private stage without replacing output."""
    destination_directory.rename_no_replace_from(
        source_directory,
        source,
        destination,
    )


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


def _descriptor_matches(
    descriptor: int,
    identity: CandidateFileIdentity,
    sha256: str,
) -> bool:
    """Bind a content check to one held file across the complete SHA read."""
    before = _candidate_identity(os.fstat(descriptor))
    digest = _sha256_descriptor(descriptor)
    after = _candidate_identity(os.fstat(descriptor))
    return before == identity and after == identity and digest == sha256


def _descriptor_path_matches(
    path: Path,
    descriptor: int,
    identity: CandidateFileIdentity,
    sha256: str,
) -> bool:
    """Verify a directory entry before and after hashing its held file."""
    descriptor_before = _candidate_identity(os.fstat(descriptor))
    path_before = _path_identity(path)
    digest = _sha256_descriptor(descriptor)
    path_after = _path_identity(path)
    descriptor_after = _candidate_identity(os.fstat(descriptor))
    return (
        descriptor_before == identity
        and descriptor_after == identity
        and path_before == identity
        and path_after == identity
        and digest == sha256
    )


def _bound_entry_identity(
    directory: PublicationDirectory | RecoveryDirectory,
    name: str,
) -> CandidateFileIdentity:
    info = directory.lstat(name)
    if not stat.S_ISREG(info.st_mode):
        raise _output_error("bound output entry must be a regular file")
    return _candidate_identity(info)


def _descriptor_bound_entry_matches(
    directory: PublicationDirectory | RecoveryDirectory,
    name: str,
    descriptor: int,
    identity: CandidateFileIdentity,
    sha256: str,
) -> bool:
    descriptor_before = _candidate_identity(os.fstat(descriptor))
    entry_before = _bound_entry_identity(directory, name)
    digest = _sha256_descriptor(descriptor)
    entry_after = _bound_entry_identity(directory, name)
    descriptor_after = _candidate_identity(os.fstat(descriptor))
    return (
        descriptor_before == identity
        and descriptor_after == identity
        and entry_before == identity
        and entry_after == identity
        and digest == sha256
    )


def _validate_held_candidate_webp(
    descriptor: int,
    summary: EncodeSummary,
    ownership: RgbaOwnershipTracker | None,
) -> WebPInfo:
    """Validate a duplicate of the exact held candidate file description."""
    duplicate = os.dup(descriptor)
    try:
        with os.fdopen(duplicate, "rb") as held:
            duplicate = -1
            return validate_webp(
                held,
                summary.frames,
                summary.duration_ms,
                rgba_ownership_tracker=ownership,
            )
    finally:
        if duplicate >= 0:
            os.close(duplicate)


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


def _prepare_publication_stage(
    candidate: ValidatedCandidate,
    publication: PublicationDirectory,
    destination: Path,
    transaction: _HeldOutputTransaction,
) -> _HeldPublicationFile:
    """Install one bounded private stage which commit consumes by rename."""
    publication.assert_still_bound()
    publication_directory = transaction.directory
    publication_directory.assert_still_bound()
    target_key = transaction.target_key
    if transaction.destination_name != publication.name_for(destination):
        raise _output_error("output transaction does not match its destination")
    publication_name = f"{target_key}.publish"
    pending_name = f".{target_key}.publish-pending"
    slot: LockedSlotFile | None = None
    identity: CandidateFileIdentity | None = None
    try:
        slot, identity = _stage_bound_recovery_copy(
            publication_directory,
            pending_name,
            candidate._descriptor,
            candidate.identity.size,
            candidate.sha256,
            transaction,
        )
        _fsync_directory(publication_directory)
        publication_directory.replace_owned(
            pending_name,
            publication_name,
            slot,
            transaction.lock,
        )
        if not _descriptor_bound_entry_matches(
            publication_directory,
            publication_name,
            slot.descriptor,
            identity,
            candidate.sha256,
        ):
            raise _output_error("private publication stage changed while installed")
        _fsync_directory(publication_directory)
        if not _descriptor_bound_entry_matches(
            publication_directory,
            publication_name,
            slot.descriptor,
            identity,
            candidate.sha256,
        ):
            raise _output_error(
                "private publication stage changed after directory sync"
            )
        staged = _HeldPublicationFile(
            publication_directory,
            publication_name,
            slot,
            identity,
            candidate.sha256,
            owns_directory=False,
        )
        slot = None
        return staged
    except BaseException as error:
        if slot is not None:
            slot.close(error)
        raise


def _snapshot_existing_output(
    publication: PublicationDirectory,
    destination_name: str,
) -> _HeldPreviousOutput | None:
    try:
        source = publication.open_read(destination_name)
    except FileNotFoundError:
        return None
    try:
        identity = _candidate_identity(os.fstat(source))
        if _bound_entry_identity(publication, destination_name) != identity:
            raise _output_error("existing output changed before publication")
        digest = _sha256_descriptor(source)
        if not _descriptor_bound_entry_matches(
            publication,
            destination_name,
            source,
            identity,
            digest,
        ):
            raise _output_error("existing output changed while taking stable snapshot")
        return _HeldPreviousOutput(source, identity, digest)
    except BaseException as error:
        try:
            os.close(source)
        except OSError as close_error:
            error.add_note(
                f"additional previous-output handle cleanup failure: {close_error}"
            )
        raise


def _fsync_directory(directory: RecoveryDirectory) -> None:
    directory.fsync()


def _prepare_durable_recovery(
    publication: PublicationDirectory,
    destination_name: str,
    previous: _HeldPreviousOutput,
    transaction: _HeldOutputTransaction,
) -> _HeldRecoveryFile:
    """Create a bounded durable old-output recovery before destructive commit."""
    transaction.assert_owned()
    target_key = transaction.target_key
    recovery_name = f"{target_key}.recovery"
    shadow_name = f"{target_key}.recovery-shadow"
    pending_name = f".{target_key}.recovery-pending"
    recovery_directory = publication.open_private_directory(
        _RECOVERY_DIRECTORY_NAME,
        "recovery",
    )
    existing_slot: LockedSlotFile | None = None
    slot: LockedSlotFile | None = None
    identity: CandidateFileIdentity | None = None
    try:
        try:
            existing_slot = recovery_directory.open_locked_slot(
                recovery_name,
                transaction.lock,
                create_if_missing=False,
            )
        except FileNotFoundError:
            pass
        else:
            if _descriptor_bound_entry_matches(
                recovery_directory,
                recovery_name,
                existing_slot.descriptor,
                previous.identity,
                previous.sha256,
            ):
                os.fsync(existing_slot.descriptor)
                _require_existing_output_current(
                    publication,
                    destination_name,
                    previous,
                )
                _fsync_directory(recovery_directory)
                provisional = _HeldRecoveryFile(
                    recovery_directory,
                    recovery_name,
                    shadow_name,
                    existing_slot,
                    None,
                    previous.identity,
                    previous.sha256,
                )
                shadow_slot = _prepare_recovery_shadow(provisional, transaction)
                recovery = _HeldRecoveryFile(
                    recovery_directory,
                    recovery_name,
                    shadow_name,
                    existing_slot,
                    shadow_slot,
                    previous.identity,
                    previous.sha256,
                )
                recovery_directory.assert_still_bound()
                existing_slot = None
                return recovery
            existing_slot.close()
            existing_slot = None

        try:
            linked = recovery_directory.link_parent_file(
                destination_name,
                pending_name,
                transaction.lock,
            )
        except OSError:
            linked = False
        if linked:
            slot = recovery_directory.open_locked_slot(
                pending_name,
                transaction.lock,
                create_if_missing=False,
            )
            identity = _candidate_identity(os.fstat(slot.descriptor))
            if not _descriptor_bound_entry_matches(
                recovery_directory,
                pending_name,
                slot.descriptor,
                previous.identity,
                previous.sha256,
            ):
                raise _output_error(
                    "hard-linked output recovery changed while prepared"
                )
        else:
            try:
                slot = recovery_directory.open_locked_slot(
                    pending_name,
                    transaction.lock,
                    create_if_missing=False,
                )
            except FileNotFoundError:
                slot = None
            if slot is not None and _descriptor_bound_entry_matches(
                recovery_directory,
                pending_name,
                slot.descriptor,
                previous.identity,
                previous.sha256,
            ):
                identity = previous.identity
            else:
                if slot is not None:
                    identity = _fill_locked_slot(
                        recovery_directory,
                        pending_name,
                        slot,
                        previous.descriptor,
                        previous.identity.size,
                        previous.sha256,
                        transaction,
                    )
                else:
                    slot, identity = _stage_bound_recovery_copy(
                        recovery_directory,
                        pending_name,
                        previous.descriptor,
                        previous.identity.size,
                        previous.sha256,
                        transaction,
                    )
        if slot is None or identity is None:
            raise RuntimeError("durable recovery slot was not prepared")
        os.fsync(slot.descriptor)
        recovery_directory.assert_still_bound()
        _require_existing_output_current(
            publication,
            destination_name,
            previous,
        )
        _fsync_directory(recovery_directory)
        recovery_directory.replace_owned(
            pending_name,
            recovery_name,
            slot,
            transaction.lock,
        )
        if not _descriptor_bound_entry_matches(
            recovery_directory,
            recovery_name,
            slot.descriptor,
            identity,
            previous.sha256,
        ):
            raise _output_error("durable output recovery changed while installed")
        _fsync_directory(recovery_directory)
        if not _descriptor_bound_entry_matches(
            recovery_directory,
            recovery_name,
            slot.descriptor,
            identity,
            previous.sha256,
        ):
            raise _output_error("durable output recovery changed after directory sync")
        provisional = _HeldRecoveryFile(
            recovery_directory,
            recovery_name,
            shadow_name,
            slot,
            None,
            identity,
            previous.sha256,
        )
        shadow_slot = _prepare_recovery_shadow(provisional, transaction)
        recovery = _HeldRecoveryFile(
            recovery_directory,
            recovery_name,
            shadow_name,
            slot,
            shadow_slot,
            identity,
            previous.sha256,
        )
        recovery_directory.assert_still_bound()
        slot = None
        return recovery
    except BaseException as error:
        for held_slot in (existing_slot, slot):
            if held_slot is None:
                continue
            held_slot.close(error)
        try:
            recovery_directory.close(error)
        except BaseException as close_error:
            error.add_note(
                f"additional recovery-directory cleanup failure: {close_error}"
            )
        raise


def _prepare_recovery_shadow(
    recovery: _HeldRecoveryFile,
    transaction: _HeldOutputTransaction,
) -> LockedSlotFile | None:
    """Keep a second bounded durable name for recovery-path interference."""
    transaction.assert_owned()
    recovery_directory = recovery.directory
    existing_shadow: LockedSlotFile | None = None
    try:
        try:
            shadow_info = recovery_directory.lstat(recovery.shadow_name)
        except FileNotFoundError:
            shadow_info = None
        if (
            shadow_info is not None
            and (shadow_info.st_dev, shadow_info.st_ino) == recovery.slot.identity
            and _descriptor_bound_entry_matches(
                recovery_directory,
                recovery.shadow_name,
                recovery.descriptor,
                recovery.identity,
                recovery.sha256,
            )
        ):
            os.fsync(recovery.descriptor)
            return None
        if shadow_info is not None:
            existing_shadow = recovery_directory.open_locked_slot(
                recovery.shadow_name,
                transaction.lock,
                create_if_missing=False,
            )
            if _descriptor_bound_entry_matches(
                recovery_directory,
                recovery.shadow_name,
                existing_shadow.descriptor,
                recovery.identity,
                recovery.sha256,
            ):
                os.fsync(existing_shadow.descriptor)
                held = existing_shadow
                existing_shadow = None
                return held
            existing_shadow.close()
            existing_shadow = None
    except BaseException as error:
        if existing_shadow is not None:
            existing_shadow.close(error)
        raise

    pending_name = f".{recovery.name.removesuffix('.recovery')}.recovery-shadow-pending"
    slot: LockedSlotFile | None = None
    source_alias = False
    identity: CandidateFileIdentity | None = None
    try:
        try:
            linked = recovery_directory.link_file(
                recovery.name,
                pending_name,
                transaction.lock,
            )
        except OSError:
            linked = False
        if linked:
            source_alias = True
            identity = recovery.identity
            if not _descriptor_bound_entry_matches(
                recovery_directory,
                pending_name,
                recovery.descriptor,
                recovery.identity,
                recovery.sha256,
            ):
                raise _output_error("recovery shadow changed while hard-linked")
        else:
            try:
                pending_info = recovery_directory.lstat(pending_name)
            except FileNotFoundError:
                pending_info = None
            if (
                pending_info is not None
                and (pending_info.st_dev, pending_info.st_ino) == recovery.slot.identity
            ):
                source_alias = True
                identity = recovery.identity
                if not _descriptor_bound_entry_matches(
                    recovery_directory,
                    pending_name,
                    recovery.descriptor,
                    recovery.identity,
                    recovery.sha256,
                ):
                    raise _output_error("recovery shadow alias changed")
            else:
                slot, identity = _stage_bound_recovery_copy(
                    recovery_directory,
                    pending_name,
                    recovery.descriptor,
                    recovery.identity.size,
                    recovery.sha256,
                    transaction,
                )
        source_slot = recovery.slot if source_alias else slot
        if source_slot is None or identity is None:
            raise RuntimeError("recovery shadow slot was not prepared")
        os.fsync(source_slot.descriptor)
        recovery_directory.assert_still_bound()
        _fsync_directory(recovery_directory)
        recovery_directory.replace_owned(
            pending_name,
            recovery.shadow_name,
            source_slot,
            transaction.lock,
            source_alias=source_alias,
        )
        if not _descriptor_bound_entry_matches(
            recovery_directory,
            recovery.shadow_name,
            source_slot.descriptor,
            identity,
            recovery.sha256,
        ):
            raise _output_error("durable recovery shadow changed while installed")
        _fsync_directory(recovery_directory)
        if not _descriptor_bound_entry_matches(
            recovery_directory,
            recovery.shadow_name,
            source_slot.descriptor,
            identity,
            recovery.sha256,
        ):
            raise _output_error("durable recovery shadow changed after directory sync")
        recovery_directory.assert_still_bound()
        if source_alias:
            return None
        if slot is None:
            raise RuntimeError("distinct recovery shadow lost its slot lock")
        held_slot = slot
        slot = None
        return held_slot
    except BaseException as error:
        if slot is not None:
            slot.close(error)
        raise


def _fill_locked_slot(
    directory: RecoveryDirectory,
    name: str,
    slot: LockedSlotFile,
    source: int,
    expected_size: int,
    expected_sha256: str,
    transaction: _HeldOutputTransaction,
) -> CandidateFileIdentity:
    slot.reset_for_write(source)
    _copy_descriptor(source, slot.descriptor)
    os.fsync(slot.descriptor)
    identity = _candidate_identity(os.fstat(slot.descriptor))
    if identity.size != expected_size or not _descriptor_bound_entry_matches(
        directory,
        name,
        slot.descriptor,
        identity,
        expected_sha256,
    ):
        raise _output_error("bound recovery copy changed while written")
    transaction.assert_owned()
    slot.assert_owned()
    return identity


def _stage_bound_recovery_copy(
    directory: RecoveryDirectory,
    name: str,
    source: int,
    expected_size: int,
    expected_sha256: str,
    transaction: _HeldOutputTransaction,
) -> tuple[LockedSlotFile, CandidateFileIdentity]:
    transaction.assert_owned()
    slot = directory.open_locked_slot(name, transaction.lock)
    try:
        identity = _fill_locked_slot(
            directory,
            name,
            slot,
            source,
            expected_size,
            expected_sha256,
            transaction,
        )
        return slot, identity
    except BaseException as error:
        slot.close(error)
        raise


def _held_bound_entry_matches(
    directory: RecoveryDirectory,
    name: str,
    identity: CandidateFileIdentity,
    sha256: str,
) -> bool:
    try:
        descriptor = directory.open_read(name)
    except FileNotFoundError:
        return False
    primary: BaseException | None = None
    try:
        return _descriptor_bound_entry_matches(
            directory,
            name,
            descriptor,
            identity,
            sha256,
        )
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            if primary is not None:
                primary.add_note(f"additional held-path cleanup failure: {error}")
            else:
                raise _map_output_os_error(
                    error,
                    "cannot close held-path verification handle",
                ) from error


def _require_existing_output_current(
    publication: PublicationDirectory,
    destination_name: str,
    previous: _HeldPreviousOutput,
) -> None:
    current = publication.open_read(destination_name)
    primary: BaseException | None = None
    try:
        if not _descriptor_bound_entry_matches(
            publication,
            destination_name,
            current,
            previous.identity,
            previous.sha256,
        ):
            raise _output_error("existing output changed before atomic publication")
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            os.close(current)
        except OSError as error:
            if primary is not None:
                primary.add_note(f"additional previous-output cleanup failure: {error}")
            else:
                raise _map_output_os_error(
                    error, "cannot close previous-output verification handle"
                ) from error


def _rollback_publication(
    publication: PublicationDirectory,
    destination: Path,
    destination_name: str,
    previous: _HeldPreviousOutput | None,
    recovery: _HeldRecoveryFile | None,
    transaction: _HeldOutputTransaction,
    primary: BaseException,
) -> None:
    transaction.assert_owned()
    if previous is None:
        primary.add_note(
            "commit verification failed with no previous output; the current "
            "destination was retained instead of being unlinked"
        )
        return
    if recovery is None:
        raise _output_error("previous output has no durable precommit recovery")

    failures: list[str] = []
    if transaction.destination_name != publication.name_for(destination):
        raise _output_error("rollback transaction does not match its destination")
    target_key = transaction.target_key
    for attempt in range(_MAX_ROLLBACK_RESTORE_ATTEMPTS):
        staged: _HeldPublicationFile | None = None
        restored = False
        try:
            restore_name = f".{target_key}.restore-{attempt}"
            restore_slot, identity = _stage_bound_recovery_copy(
                recovery.directory,
                restore_name,
                recovery.descriptor,
                recovery.identity.size,
                recovery.sha256,
                transaction,
            )
            staged = _HeldPublicationFile(
                recovery.directory,
                restore_name,
                restore_slot,
                identity,
                recovery.sha256,
                owns_directory=False,
            )
            _fsync_directory(recovery.directory)
            transaction.assert_owned()
            publication.replace_from(
                staged.directory,
                staged.name,
                destination_name,
            )
            staged.directory.fsync()
            publication.fsync()
            if _published_file_matches(
                publication,
                destination_name,
                staged.identity,
                recovery.sha256,
            ):
                restored = True
            else:
                failures.append("rollback destination identity changed after replace")
        except BaseException as error:
            failures.append(str(error))
        finally:
            if staged is not None:
                _close_staged_file(staged, failures, primary)
        if restored:
            for failure in failures:
                primary.add_note(f"rollback diagnostic: {failure}")
            return

    verified_recovery_path: Path | None = None
    try:
        if _descriptor_bound_entry_matches(
            recovery.directory,
            recovery.name,
            recovery.descriptor,
            recovery.identity,
            recovery.sha256,
        ):
            publication.assert_still_bound()
            verified_recovery_path = recovery.path
    except BaseException as error:
        failures.append(f"durable recovery verification failed: {error}")
    if verified_recovery_path is None:
        failures.append("durable recovery path changed during rollback")
        try:
            if _held_bound_entry_matches(
                recovery.directory,
                recovery.shadow_name,
                recovery.identity,
                recovery.sha256,
            ):
                publication.assert_still_bound()
                verified_recovery_path = recovery.shadow_path
        except BaseException as error:
            failures.append(f"durable recovery-shadow verification failed: {error}")
    if verified_recovery_path is not None:
        detail = (
            "atomic output rollback could not defeat repeated filesystem "
            f"interference; old bytes retained at {verified_recovery_path}"
        )
    else:
        detail = (
            "atomic output rollback could not defeat repeated filesystem "
            f"interference; recovery path could not be verified at {recovery.path}"
        )
    catastrophic = AppError(
        ErrorCode.INVALID_OUTPUT,
        "publish-rollback",
        "error.output.failed",
        detail,
        "recover-output",
    )
    catastrophic.add_note(f"original publication failure: {primary}")
    for failure in failures:
        catastrophic.add_note(f"rollback attempt: {failure}")
    raise catastrophic from primary


def _published_file_matches(
    publication: PublicationDirectory,
    destination_name: str,
    identity: CandidateFileIdentity,
    sha256: str,
) -> bool:
    published = publication.open_read(destination_name)
    primary: BaseException | None = None
    try:
        return _descriptor_bound_entry_matches(
            publication,
            destination_name,
            published,
            identity,
            sha256,
        )
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            os.close(published)
        except OSError as error:
            if primary is not None:
                primary.add_note(
                    f"additional published-output cleanup failure: {error}"
                )
            else:
                raise _map_output_os_error(
                    error, "cannot close published-output verification handle"
                ) from error


def _close_previous_output(
    previous: _HeldPreviousOutput,
    cleanup_notes: list[str] | None,
    primary: BaseException | None,
) -> None:
    try:
        os.close(previous.descriptor)
    except OSError as error:
        detail = f"additional previous-output handle cleanup failure: {error}"
        if primary is not None:
            primary.add_note(detail)
        elif cleanup_notes is not None:
            cleanup_notes.append(detail)


def _close_staged_file(
    staged: _HeldPublicationFile,
    cleanup_notes: list[str] | None,
    primary: BaseException | None,
    cleanup_errors: list[BaseException] | None = None,
) -> None:
    failures: list[str] = []
    try:
        if _held_bound_entry_matches(
            staged.directory,
            staged.name,
            staged.identity,
            staged.sha256,
        ):
            failures.append(f"verified private staged file retained at {staged.path}")
    except BaseException as error:
        failures.append(f"private staged-file inspection failed: {error}")
    try:
        staged.slot.close(primary)
    except BaseException as error:
        if cleanup_errors is not None:
            cleanup_errors.append(error)
        failures.append(f"staged-publication slot cleanup failed: {error}")
    if staged.owns_directory:
        try:
            staged.directory.close(primary)
        except BaseException as error:
            if cleanup_errors is not None:
                cleanup_errors.append(error)
            failures.append(
                f"additional publication-directory cleanup failure: {error}"
            )
    for detail in failures:
        if primary is not None:
            primary.add_note(detail)
        elif cleanup_notes is not None:
            cleanup_notes.append(detail)


def _close_recovery_file(
    recovery: _HeldRecoveryFile,
    cleanup_notes: list[str] | None,
    primary: BaseException | None,
    cleanup_errors: list[BaseException] | None = None,
) -> None:
    failures: list[str] = []
    for label, slot in (
        ("shadow", recovery.shadow_slot),
        ("primary", recovery.slot),
    ):
        if slot is None:
            continue
        try:
            slot.close(primary)
        except BaseException as error:
            if cleanup_errors is not None:
                cleanup_errors.append(error)
            failures.append(
                f"additional recovery-{label} slot cleanup failure: {error}"
            )
    try:
        recovery.directory.close(primary)
    except BaseException as error:
        if cleanup_errors is not None:
            cleanup_errors.append(error)
        failures.append(f"additional recovery-directory cleanup failure: {error}")
    for detail in failures:
        if primary is not None:
            primary.add_note(detail)
        elif cleanup_notes is not None:
            cleanup_notes.append(detail)


def _close_output_transaction(
    transaction: _HeldOutputTransaction,
    cleanup_notes: list[str] | None,
    primary: BaseException | None,
    cleanup_errors: list[BaseException] | None = None,
) -> None:
    failures: list[BaseException] = []
    try:
        transaction.directory.close(primary)
    except BaseException as error:
        failures.append(error)
    try:
        transaction.lock.close(primary)
    except BaseException as error:
        failures.append(error)
    for failure in failures:
        if cleanup_errors is not None:
            cleanup_errors.append(failure)
        detail = f"additional output-transaction cleanup failure: {failure}"
        if primary is not None:
            primary.add_note(detail)
        elif cleanup_notes is not None:
            cleanup_notes.append(detail)


def _close_publication_directory(
    publication: PublicationDirectory,
    cleanup_notes: list[str] | None,
    primary: BaseException | None,
    cleanup_errors: list[BaseException] | None = None,
) -> None:
    try:
        publication.close(primary)
    except BaseException as error:
        if cleanup_errors is not None:
            cleanup_errors.append(error)
        detail = f"additional publication-directory cleanup failure: {error}"
        if primary is not None:
            primary.add_note(detail)
        elif cleanup_notes is not None:
            cleanup_notes.append(detail)


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
