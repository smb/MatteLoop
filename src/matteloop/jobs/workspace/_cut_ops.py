from __future__ import annotations

from typing import TYPE_CHECKING

# ruff: noqa: F403,F405
from ._common import *  # noqa: F403,F401

if TYPE_CHECKING:
    from ._errors import (
        _manifest_error,
        _promotion_error,
        _require_workspace,
        _set_error,
        _stage_error,
        _unsafe_error,
    )
    from ._filesystem import _BoundDirectory
    from ._fs_helpers import _unlink_bound_regular
    from ._manifest import CutFrame, CutManifest, CutUnionMetadata
    from ._manifest_io import (
        _atomic_directory_exchange,
        _read_bound_manifest,
        _read_manifest,
        _recover_promotion,
        _write_journal,
        _write_manifest_atomic,
    )
    from ._manifest_validation import (
        _bounded_int,
        _frame_filename,
        _validate_dimensions,
        _validate_frame_index,
    )
    from ._models import CutWorkspace, WorkspaceLifecycle
    from ._runtime_helpers import _not_cancelled, _promotion_lock, _raise_if_cancelled
    from ._scan import _inspect_frame, _scan_bound_cut_set, _scan_cut_set
    from ._snapshot_ops import _snapshot_validated_workspace
    from ._tree_helpers import (
        _cleanup_snapshot,
        _cleanup_staged_cut,
        _remove_bound_tree,
    )

__all__ = (
    "_validate_bound_cut_set",
    "compare_and_set_union_metadata",
    "detect_external_edits",
    "discard_staged_set",
    "promote_cut_set",
    "promote_for_render",
    "stage_cut",
    "validate_cut_set",
)


def stage_cut(workspace: CutWorkspace, index: int, image: Image.Image) -> CutFrame:
    """Persist one sequential RGBA PNG into a private sibling stage."""
    _require_workspace(workspace)
    if workspace.lifecycle is not WorkspaceLifecycle.STAGING:
        raise _stage_error("stage_cut requires a staging workspace")
    _validate_frame_index(index)
    if not isinstance(image, Image.Image) or image.mode != "RGBA":
        raise _stage_error("staged cut must be a real Pillow RGBA image")
    _validate_dimensions(*image.size)
    filename = _frame_filename(index)
    try:
        with _BoundDirectory.open(workspace.path) as bound:
            existing_names: set[str] = set()
            for name, info in bound.iter_entries():
                if name == MANIFEST_FILENAME or name.startswith(".manifest-"):
                    continue
                match = _FRAME_RE.fullmatch(name)
                if match is None or not stat.S_ISREG(info.st_mode):
                    raise _stage_error(f"unexpected staged entry {name!r}")
                existing_names.add(name)
                if len(existing_names) > MAX_FRAME_COUNT:
                    raise _stage_error("staged frame count exceeds the bound")
            expected_names = {_frame_filename(value) for value in range(index)}
            if existing_names != expected_names:
                detail = (
                    f"staged frame {index} is not sequential; existing canonical "
                    "names contain a gap"
                )
                raise _stage_error(detail)
            output = bound.open_new(filename)
            try:
                image.save(output, format="PNG")
                output.flush()
                os.fsync(output.fileno())
            finally:
                output.close()
            bound.fsync()
            bound.assert_still_named()
    except AppError:
        raise
    except (OSError, ValueError) as error:
        raise _stage_error(f"cannot persist frame {filename}: {error}") from error
    frame, _identity = _inspect_frame(
        workspace.path,
        CutFrame(index, filename, image.width, image.height, 1, 0, "0" * 64),
        compare_recorded=False,
        load_pixels=True,
    )
    return frame


@_filesystem_boundary("stage", "cannot discard staged cut workspace")
def discard_staged_set(workspace: CutWorkspace) -> bool:
    """Explicitly remove one unpublished staged set and nothing durable."""
    _require_workspace(workspace)
    if workspace.lifecycle is not WorkspaceLifecycle.STAGING:
        raise _stage_error("discard requires a staged cut workspace")
    try:
        with _BoundDirectory.open(workspace.cuts_root) as parent:
            _remove_bound_tree(parent, workspace.path.name)
            parent.fsync()
    except FileNotFoundError:
        return False
    return True


@_filesystem_boundary("promotion", "cannot prepare cuts for render")
def promote_for_render(
    workspace: CutWorkspace,
    manifest: CutManifest,
    scratch_directory: Path,
    *,
    cancelled: CancellationCheck | None = None,
    prefer_reflink: bool = True,
) -> tuple[CutWorkspace, CutWorkspace, CutManifest]:
    """Snapshot a validated stage, then durably publish it for later jobs."""
    _require_workspace(workspace)
    if workspace.lifecycle is not WorkspaceLifecycle.STAGING:
        raise _promotion_error("render promotion requires a staged workspace")
    if type(manifest) is not CutManifest or manifest.cache_key != workspace.cache_key:
        raise _manifest_error("render manifest does not match its staged workspace")
    check_cancelled = cancelled if cancelled is not None else _not_cancelled
    if not callable(check_cancelled):
        raise TypeError("cancelled must be callable")
    private: CutWorkspace | None = None
    try:
        _raise_if_cancelled(check_cancelled)
        _write_manifest_atomic(workspace.path, manifest)
        candidate = validate_cut_set(workspace)
        private = _snapshot_validated_workspace(
            workspace,
            candidate,
            scratch_directory,
            cancelled=check_cancelled,
            prefer_reflink=prefer_reflink,
        )
        _raise_if_cancelled(check_cancelled)
        durable = promote_cut_set(workspace)
        promoted_manifest = validate_cut_set(durable)
        return durable, private, promoted_manifest
    except AppError as error:
        if private is not None:
            _cleanup_snapshot(scratch_directory, error)
        if workspace.path.exists():
            _cleanup_staged_cut(workspace.path, error)
        raise


@_filesystem_boundary("promotion", "cannot promote cut workspace")
def promote_cut_set(
    workspace: CutWorkspace, manifest: CutManifest | None = None
) -> CutWorkspace:
    """Validate and atomically publish a sibling stage without losing old cuts."""
    _require_workspace(workspace)
    if workspace.lifecycle is not WorkspaceLifecycle.STAGING:
        raise _promotion_error("promotion requires a staging workspace")
    try:
        if manifest is not None:
            if (
                type(manifest) is not CutManifest
                or manifest.cache_key != workspace.cache_key
            ):
                raise _manifest_error(
                    "promotion manifest does not match staging cache key"
                )
            _write_manifest_atomic(workspace.path, manifest)
        candidate = validate_cut_set(workspace)
    except AppError as error:
        _cleanup_staged_cut(workspace.path, error)
        raise
    except (OSError, UnsafeCacheError, BoundDirectoryCloseError) as error:
        failure = _structured_filesystem_failure(
            "promotion", "cannot validate staged cut workspace", error
        )
        _cleanup_staged_cut(workspace.path, failure)
        raise failure from error
    target = workspace.cuts_root / workspace.directory_name
    marker = workspace.cuts_root / f".replace-{workspace.cache_key}.json"
    token = uuid.uuid4().hex
    backup = workspace.cuts_root / f".backup-{workspace.cache_key}-{token}"
    lock = _promotion_lock(str(target))
    with lock:
        _recover_promotion(workspace.cuts_root, workspace.cache_key)
        cuts_bound: _BoundDirectory | None = None
        try:
            with _BoundDirectory.open(workspace.cuts_root) as opened_cuts_bound:
                cuts_bound = opened_cuts_bound
                previous_hash: str | None = None
                try:
                    target_info = cuts_bound.lstat(target.name)
                except FileNotFoundError:
                    target_exists = False
                else:
                    if stat.S_ISLNK(target_info.st_mode) or not stat.S_ISDIR(
                        target_info.st_mode
                    ):
                        raise _unsafe_error(
                            f"workspace entry {target.name!r} is redirected"
                        )
                    target_exists = True
                if target_exists:
                    with cuts_bound.open_child(target.name) as previous_bound:
                        previous, _identity = _read_bound_manifest(previous_bound)
                    previous_hash = hashlib.sha256(previous.to_json_bytes()).hexdigest()
                journal: dict[str, object] = {
                    "backup_name": backup.name,
                    "cache_key": workspace.cache_key,
                    "candidate_manifest_sha256": hashlib.sha256(
                        candidate.to_json_bytes()
                    ).hexdigest(),
                    "phase": "prepared",
                    "previous_manifest_sha256": previous_hash,
                    "stage_name": workspace.path.name,
                    "target_name": target.name,
                    "used_exchange": False,
                    "version": 1,
                }
                try:
                    _write_journal(marker, journal, bound=cuts_bound)
                except AppError as error:
                    _cleanup_staged_cut(workspace.path, error, parent=cuts_bound)
                    raise
                except (
                    OSError,
                    UnsafeCacheError,
                    BoundDirectoryCloseError,
                ) as error:
                    failure = _structured_filesystem_failure(
                        "promotion", "cannot create cut promotion journal", error
                    )
                    _cleanup_staged_cut(
                        workspace.path,
                        failure,
                        parent=cuts_bound,
                    )
                    raise failure from error
                old_location: Path | None = None
                try:
                    if target_exists:
                        exchanged = (
                            False
                            if cuts_bound.descriptor is None
                            else _atomic_directory_exchange(workspace.path, target)
                        )
                        if exchanged:
                            old_location = workspace.path
                            journal["phase"] = "new-active"
                            journal["used_exchange"] = True
                            _write_journal(marker, journal, bound=cuts_bound)
                        else:
                            cuts_bound.replace_directory(target.name, backup.name)
                            old_location = backup
                            journal["phase"] = "old-moved"
                            _write_journal(marker, journal, bound=cuts_bound)
                            cuts_bound.replace_directory(
                                workspace.path.name, target.name
                            )
                            journal["phase"] = "new-active"
                            _write_journal(marker, journal, bound=cuts_bound)
                    else:
                        cuts_bound.replace_directory(workspace.path.name, target.name)
                        journal["phase"] = "new-active"
                        _write_journal(marker, journal, bound=cuts_bound)
                    promoted = CutWorkspace(
                        workspace.output_directory,
                        workspace.workspace_root,
                        workspace.cuts_root,
                        workspace.scratch_root,
                        workspace.cache_key,
                        target,
                        WorkspaceLifecycle.PROMOTED,
                        workspace.fallback,
                        target.name,
                    )
                    with cuts_bound.open_child(target.name) as promoted_bound:
                        validated = _validate_bound_cut_set(
                            promoted_bound, workspace.cache_key
                        )
                    if validated.to_json_bytes() != candidate.to_json_bytes():
                        raise _promotion_error(
                            "promoted manifest changed during replacement"
                        )
                    cuts_bound.fsync()
                    if old_location is not None:
                        try:
                            _remove_bound_tree(cuts_bound, old_location.name)
                        except FileNotFoundError:
                            pass
                    _unlink_bound_regular(cuts_bound, marker.name)
                    cuts_bound.fsync()
                    return promoted
                except AppError:
                    raise
                except OSError as error:
                    raise _promotion_error(
                        f"atomic cut promotion failed: {error}"
                    ) from error
        finally:
            if cuts_bound is None or not cuts_bound.owns_resources():
                try:
                    _recover_promotion(workspace.cuts_root, workspace.cache_key)
                except AppError:
                    pass
                except (OSError, UnsafeCacheError, BoundDirectoryCloseError):
                    pass


@_filesystem_boundary("set", "cannot validate cut workspace")
def validate_cut_set(workspace: CutWorkspace) -> CutManifest:
    """Validate manifest, namespace, frame bytes, metadata, and exact hashes."""
    _require_workspace(workspace)
    with _promotion_lock(str(workspace.cuts_root / workspace.cache_key)):
        try:
            manifest, manifest_identity = _read_manifest(workspace.path)
            if manifest.cache_key != workspace.cache_key:
                raise _set_error("manifest cache key does not match workspace")
            _scan_cut_set(
                workspace.path,
                manifest,
                manifest_identity,
                compare_recorded=True,
            )
            return manifest
        except AppError:
            raise
        except OSError as error:
            raise _set_error(f"cannot validate cut workspace: {error}") from error


def _validate_bound_cut_set(bound: _BoundDirectory, cache_key: str) -> CutManifest:
    manifest, manifest_identity = _read_bound_manifest(bound)
    if manifest.cache_key != cache_key:
        raise _set_error("manifest cache key does not match workspace")
    _scan_bound_cut_set(
        bound,
        manifest,
        manifest_identity,
        compare_recorded=True,
    )
    return manifest


@_filesystem_boundary("set", "cannot detect external cut edits")
def detect_external_edits(
    workspace: CutWorkspace, *, now_ns: int | None = None
) -> CutManifest:
    """Rescan valid cuts, persist current hashes, and invalidate derived union data."""
    _require_workspace(workspace)
    if workspace.lifecycle is not WorkspaceLifecycle.PROMOTED:
        raise _set_error("external edit detection requires promoted durable cuts")
    with _promotion_lock(str(workspace.cuts_root / workspace.cache_key)):
        manifest, manifest_identity = _read_manifest(workspace.path)
        frames, _identities = _scan_cut_set(
            workspace.path,
            manifest,
            manifest_identity,
            compare_recorded=False,
        )
        timestamp = time.time_ns() if now_ns is None else now_ns
        _bounded_int(timestamp, "last-use timestamp", minimum=0, maximum=_MAX_INT64)
        changed = frames != manifest.frames
        updated = replace(
            manifest,
            frames=frames,
            edited=manifest.edited or changed,
            union_metadata=None if changed else manifest.union_metadata,
            last_used_at_ns=max(
                timestamp, manifest.created_at_ns, manifest.last_used_at_ns
            ),
        )
        if updated != manifest:
            _write_manifest_atomic(
                workspace.path, updated, expected_identity=manifest_identity
            )
        return validate_cut_set(workspace)


@_filesystem_boundary("set", "cannot compare-and-set cut union metadata")
def compare_and_set_union_metadata(
    workspace: CutWorkspace,
    expected_frame_hashes: Sequence[str],
    union_metadata: CutUnionMetadata,
    *,
    now_ns: int | None = None,
) -> bool:
    """Publish derived union data only while the expected cut bytes still win."""
    _require_workspace(workspace)
    if workspace.lifecycle is not WorkspaceLifecycle.PROMOTED:
        raise _set_error("union metadata updates require durable promoted cuts")
    if isinstance(expected_frame_hashes, (str, bytes)):
        raise TypeError("expected_frame_hashes must be a sequence")
    expected = tuple(expected_frame_hashes)
    if any(type(value) is not str for value in expected):
        raise TypeError("expected frame hashes must be strings")
    if type(union_metadata) is not CutUnionMetadata:
        raise TypeError("union_metadata must be a CutUnionMetadata")
    timestamp = time.time_ns() if now_ns is None else now_ns
    _bounded_int(timestamp, "last-use timestamp", minimum=0, maximum=_MAX_INT64)
    with _promotion_lock(str(workspace.cuts_root / workspace.cache_key)):
        manifest, manifest_identity = _read_manifest(workspace.path)
        frames, _identities = _scan_cut_set(
            workspace.path,
            manifest,
            manifest_identity,
            compare_recorded=False,
        )
        if tuple(frame.sha256 for frame in frames) != expected:
            return False
        updated = replace(
            manifest,
            frames=frames,
            edited=manifest.edited or frames != manifest.frames,
            union_metadata=union_metadata,
            last_used_at_ns=max(
                timestamp, manifest.created_at_ns, manifest.last_used_at_ns
            ),
        )
        try:
            _write_manifest_atomic(
                workspace.path, updated, expected_identity=manifest_identity
            )
        except AppError as error:
            if (
                error.code is ErrorCode.CUT_MANIFEST_INVALID
                and "changed before the atomic update" in error.technical_detail
            ):
                return False
            raise
        return True
