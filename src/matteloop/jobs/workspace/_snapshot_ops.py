from __future__ import annotations

from typing import TYPE_CHECKING

# ruff: noqa: F403,F405
from ._common import *  # noqa: F403,F401

if TYPE_CHECKING:
    from ._cut_ops import detect_external_edits, validate_cut_set
    from ._errors import (
        _cuts_changed,
        _delete_error,
        _require_workspace,
        _set_error,
        _snapshot_error,
        _unsafe_error,
    )
    from ._filesystem import _BoundDirectory
    from ._fs_helpers import _fsync_directory
    from ._manifest import CutManifest
    from ._manifest_io import (
        _read_manifest,
        _recover_all_promotions,
        _recover_promotion,
        _write_manifest_atomic,
    )
    from ._manifest_validation import (
        _bounded_int,
        _validate_job_id,
        _validate_path_value,
    )
    from ._models import (
        _READABLE_WORKSPACE_NAME_RE,
        CutWorkspace,
        ScratchCleanupResult,
        WorkspaceLifecycle,
        WorkspaceListing,
        WorkspaceSummary,
    )
    from ._platform import _workspace_layout
    from ._runtime_helpers import _not_cancelled, _promotion_lock, _raise_if_cancelled
    from ._scan import _copy_frame_descriptor_bound, _scan_cut_set
    from ._tree_helpers import _bounded_tree_size, _cleanup_snapshot, _remove_tree

__all__ = (
    "_snapshot_validated_workspace",
    "cleanup_abandoned_scratch",
    "cleanup_scratch",
    "delete_workspace",
    "list_workspaces",
    "snapshot_for_rebuild",
)


def _snapshot_validated_workspace(
    workspace: CutWorkspace,
    baseline: CutManifest,
    scratch_directory: Path,
    *,
    cancelled: CancellationCheck,
    prefer_reflink: bool,
) -> CutWorkspace:
    if not isinstance(scratch_directory, Path):
        raise _unsafe_error("scratch directory must be a Path")
    _validate_path_value(scratch_directory)
    if scratch_directory.parent != workspace.scratch_root:
        raise _unsafe_error("snapshot must use scratch/<job-id> under its workspace")
    _validate_job_id(scratch_directory.name)
    snapshot_path = scratch_directory / "cuts-snapshot"
    started = False
    try:
        _raise_if_cancelled(cancelled)
        source_manifest, manifest_identity = _read_manifest(workspace.path)
        if source_manifest != baseline:
            raise _cuts_changed("staged manifest changed before private snapshot")
        _frames, baseline_identities = _scan_cut_set(
            workspace.path,
            baseline,
            manifest_identity,
            compare_recorded=True,
        )
        with _BoundDirectory.open(workspace.scratch_root) as scratch_bound:
            scratch_bound.mkdir(scratch_directory.name, exist_ok=False)
            started = True
            with scratch_bound.open_child(scratch_directory.name) as job_bound:
                job_bound.mkdir(snapshot_path.name, exist_ok=False)
                with job_bound.open_child(snapshot_path.name):
                    pass
        for frame in baseline.frames:
            _raise_if_cancelled(cancelled)
            _copy_frame_descriptor_bound(
                workspace.path,
                snapshot_path,
                frame,
                prefer_reflink=prefer_reflink,
            )
        _write_manifest_atomic(snapshot_path, baseline)
        _raise_if_cancelled(cancelled)
        after, after_identity = _read_manifest(workspace.path)
        after_frames, after_identities = _scan_cut_set(
            workspace.path,
            after,
            after_identity,
            compare_recorded=True,
        )
        if (
            after != baseline
            or after_frames != baseline.frames
            or after_identities != baseline_identities
        ):
            raise _cuts_changed("cuts changed during private render snapshot")
        snapshot = CutWorkspace(
            workspace.output_directory,
            workspace.workspace_root,
            workspace.cuts_root,
            workspace.scratch_root,
            workspace.cache_key,
            snapshot_path,
            WorkspaceLifecycle.SNAPSHOT,
            workspace.fallback,
            workspace.directory_name,
        )
        validate_cut_set(snapshot)
        return snapshot
    except AppError as error:
        if started:
            _cleanup_snapshot(scratch_directory, error)
        raise
    except OSError as error:
        failure = _snapshot_error(f"cannot create private render snapshot: {error}")
        if started:
            _cleanup_snapshot(scratch_directory, failure)
        raise failure from error


@_filesystem_boundary("snapshot", "cannot snapshot cut workspace")
def snapshot_for_rebuild(
    workspace: CutWorkspace,
    scratch_directory: Path,
    *,
    cancelled: CancellationCheck | None = None,
    prefer_reflink: bool = True,
) -> CutWorkspace:
    """Create one private, immutable frame set with a stable rescan boundary."""
    _require_workspace(workspace)
    if workspace.lifecycle is WorkspaceLifecycle.STAGING:
        raise _snapshot_error("cannot snapshot an unpromoted cut set")
    if not isinstance(scratch_directory, Path):
        raise _unsafe_error("scratch directory must be a Path")
    _validate_path_value(scratch_directory)
    if scratch_directory.parent != workspace.scratch_root:
        raise _unsafe_error("snapshot must use scratch/<job-id> under its workspace")
    _validate_job_id(scratch_directory.name)
    check_cancelled = cancelled if cancelled is not None else _not_cancelled
    if not callable(check_cancelled):
        raise TypeError("cancelled must be callable")
    snapshot_path = scratch_directory / "cuts-snapshot"
    started = False
    try:
        _raise_if_cancelled(check_cancelled)
        # Validate before allocating scratch so pre-existing corruption keeps
        # its precise CUT_SET_INVALID diagnosis. A change after this baseline
        # is instead the retryable snapshot race.
        current = detect_external_edits(workspace)
        baseline, manifest_identity = _read_manifest(workspace.path)
        if baseline != current:
            raise _set_error("manifest changed before snapshot copying")
        _frames, baseline_identities = _scan_cut_set(
            workspace.path,
            baseline,
            manifest_identity,
            compare_recorded=True,
        )
        _raise_if_cancelled(check_cancelled)
        with _BoundDirectory.open(workspace.scratch_root) as scratch_bound:
            scratch_bound.mkdir(scratch_directory.name, exist_ok=False)
            started = True
            with scratch_bound.open_child(scratch_directory.name) as job_bound:
                job_bound.mkdir(snapshot_path.name, exist_ok=False)
                with job_bound.open_child(snapshot_path.name):
                    pass
        for frame in baseline.frames:
            _raise_if_cancelled(check_cancelled)
            _copy_frame_descriptor_bound(
                workspace.path,
                snapshot_path,
                frame,
                prefer_reflink=prefer_reflink,
            )
            _raise_if_cancelled(check_cancelled)
        _write_manifest_atomic(snapshot_path, baseline)
        _raise_if_cancelled(check_cancelled)
        after, after_manifest_identity = _read_manifest(workspace.path)
        after_frames, after_identities = _scan_cut_set(
            workspace.path,
            after,
            after_manifest_identity,
            compare_recorded=True,
        )
        if (
            after != baseline
            or after_frames != baseline.frames
            or after_identities != baseline_identities
        ):
            raise _cuts_changed("cut frames changed during the full snapshot operation")
        snapshot = CutWorkspace(
            workspace.output_directory,
            workspace.workspace_root,
            workspace.cuts_root,
            workspace.scratch_root,
            workspace.cache_key,
            snapshot_path,
            WorkspaceLifecycle.SNAPSHOT,
            workspace.fallback,
            workspace.directory_name,
        )
        validate_cut_set(snapshot)
        return snapshot
    except AppError as error:
        if started:
            _cleanup_snapshot(scratch_directory, error)
        if (
            error.code
            in {
                ErrorCode.CUT_SET_INVALID,
                ErrorCode.CUT_MANIFEST_INVALID,
                ErrorCode.CUT_WORKSPACE_UNSAFE,
            }
            and started
        ):
            raise _cuts_changed(error.technical_detail) from error
        raise
    except OSError as error:
        failure = _snapshot_error(f"cannot create rebuild snapshot: {error}")
        if started:
            _cleanup_snapshot(scratch_directory, failure)
        raise failure from error


@_filesystem_boundary("unsafe", "cannot list cut workspaces")
def list_workspaces(
    output_directory: Path,
    *,
    warning_threshold_bytes: int = WORKSPACE_WARNING_BYTES,
) -> WorkspaceListing:
    """Return immutable durable-cache summaries; never remove a cut directory."""
    _bounded_int(
        warning_threshold_bytes,
        "warning threshold",
        minimum=0,
        maximum=_MAX_INT64,
    )
    layout = _workspace_layout(output_directory, create=False)
    output, root, cuts, scratch = layout
    if not cuts.exists():
        return WorkspaceListing((), 0, warning_threshold_bytes, False)
    _recover_all_promotions(cuts)
    summaries: list[WorkspaceSummary] = []
    with _BoundDirectory.open(cuts) as bound:
        seen = 0
        for scanned, (name, info) in enumerate(bound.iter_entries(), start=1):
            if scanned > MAX_WORKSPACE_ENTRIES * 3:
                raise _unsafe_error("workspace namespace exceeds the listing bound")
            if name.startswith("."):
                continue
            if stat.S_ISLNK(info.st_mode):
                raise _unsafe_error(f"workspace entry {name!r} is redirected")
            if not stat.S_ISDIR(info.st_mode):
                continue
            if _CACHE_KEY_RE.fullmatch(name) is not None:
                cache_key = name
                manifest = None
            elif _READABLE_WORKSPACE_NAME_RE.fullmatch(name) is not None:
                try:
                    manifest, _identity = _read_manifest(cuts / name)
                except AppError:
                    continue
                cache_key = manifest.cache_key
            else:
                continue
            seen += 1
            if seen > MAX_WORKSPACE_ENTRIES:
                raise _unsafe_error("workspace count exceeds the listing bound")
            workspace = CutWorkspace(
                output,
                root,
                cuts,
                scratch,
                cache_key,
                cuts / name,
                WorkspaceLifecycle.PROMOTED,
                layout.fallback,
                name,
            )
            try:
                manifest = validate_cut_set(workspace)
            except AppError as error:
                if error.code is not ErrorCode.CUT_SET_INVALID:
                    raise
                manifest = detect_external_edits(workspace)
            size_bytes = sum(frame.size_bytes for frame in manifest.frames)
            size_bytes += len(manifest.to_json_bytes())
            summaries.append(WorkspaceSummary(workspace, manifest, size_bytes))
        bound.assert_still_named()
    summaries.sort(key=lambda item: (-item.last_used_at_ns, item.workspace.cache_key))
    total = sum(item.size_bytes for item in summaries)
    return WorkspaceListing(
        tuple(summaries),
        total,
        warning_threshold_bytes,
        total > warning_threshold_bytes,
    )


@_filesystem_boundary("delete", "cannot delete cut workspace")
def delete_workspace(workspace: CutWorkspace, *, allow_pinned: bool = False) -> None:
    """Explicitly delete exactly one durable cut directory."""
    _require_workspace(workspace)
    if workspace.lifecycle is not WorkspaceLifecycle.PROMOTED:
        raise _delete_error("only a durable promoted workspace can be deleted")
    if type(allow_pinned) is not bool:
        raise TypeError("allow_pinned must be a bool")
    lock = _promotion_lock(str(workspace.path))
    with lock:
        _recover_promotion(workspace.cuts_root, workspace.cache_key)
        try:
            manifest, _identity = _read_manifest(workspace.path)
        except AppError as error:
            if not allow_pinned:
                raise AppError(
                    ErrorCode.CUT_WORKSPACE_PINNED,
                    "cut-workspace-delete",
                    "error.cuts.pin-unknown",
                    "corrupt manifest prevents a reliable pinned-state check",
                    "confirm-delete-pinned-workspace",
                ) from error
            manifest = None
        if manifest is not None and manifest.pinned and not allow_pinned:
            raise AppError(
                ErrorCode.CUT_WORKSPACE_PINNED,
                "cut-workspace-delete",
                "error.cuts.pinned",
                "pinned cut workspace requires an explicit delete override",
                "confirm-delete-pinned-workspace",
            )
        try:
            _remove_tree(workspace.path)
            _fsync_directory(workspace.cuts_root)
        except OSError as error:
            raise _delete_error(f"cannot delete cut workspace: {error}") from error


@_filesystem_boundary("delete", "cannot clean scratch workspace")
def cleanup_scratch(output_directory: Path, job_id: str) -> bool:
    """Immediately remove one exact scratch job after success or cancellation."""
    _validate_job_id(job_id)
    _output, _root, _cuts, scratch = _workspace_layout(output_directory, create=False)
    target = scratch / job_id
    try:
        with _BoundDirectory.open(scratch) as bound:
            try:
                info = bound.lstat(job_id)
            except FileNotFoundError:
                return False
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _unsafe_error(f"scratch entry {job_id!r} is redirected")
        _remove_tree(target)
        _fsync_directory(scratch)
        return True
    except FileNotFoundError:
        return False
    except AppError:
        raise
    except OSError as error:
        raise _delete_error(f"cannot clean scratch job {job_id!r}: {error}") from error


@_filesystem_boundary("snapshot", "cannot clean abandoned scratch workspaces")
def cleanup_abandoned_scratch(
    output_directory: Path,
    *,
    older_than_ns: int = ABANDONED_SCRATCH_AGE_NS,
    now_ns: int | None = None,
    max_entries: int = 256,
) -> ScratchCleanupResult:
    """Explicitly remove at most *max_entries* scratch jobs older than 24 hours."""
    _bounded_int(
        older_than_ns,
        "scratch abandonment age",
        minimum=ABANDONED_SCRATCH_AGE_NS,
        maximum=_MAX_INT64,
    )
    _bounded_int(max_entries, "scratch cleanup count", minimum=1, maximum=1024)
    timestamp = time.time_ns() if now_ns is None else now_ns
    _bounded_int(timestamp, "current timestamp", minimum=0, maximum=_MAX_INT64)
    _output, _root, _cuts, scratch = _workspace_layout(output_directory, create=False)
    if not scratch.exists():
        return ScratchCleanupResult(0, 0, False)
    candidates: list[tuple[int, Path, int]] = []
    with _BoundDirectory.open(scratch) as bound:
        scanned = 0
        for name, info in bound.iter_entries():
            scanned += 1
            if scanned > MAX_SCRATCH_ENTRIES:
                raise _unsafe_error("scratch namespace exceeds the cleanup bound")
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _unsafe_error(f"scratch entry {name!r} is not a safe directory")
            if timestamp - info.st_mtime_ns > older_than_ns:
                candidates.append(
                    (
                        info.st_mtime_ns,
                        scratch / name,
                        _bounded_tree_size(scratch / name),
                    )
                )
        bound.assert_still_named()
    candidates.sort(key=lambda item: (item[0], item[1].name))
    selected = candidates[:max_entries]
    removed_bytes = 0
    for _mtime, path, size_bytes in selected:
        try:
            _remove_tree(path)
        except OSError as error:
            raise _snapshot_error(
                f"cannot clean abandoned scratch {path.name!r}: {error}"
            ) from error
        removed_bytes += size_bytes
    if selected:
        _fsync_directory(scratch)
    return ScratchCleanupResult(
        len(selected), removed_bytes, len(candidates) > len(selected)
    )
