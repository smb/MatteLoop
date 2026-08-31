from __future__ import annotations

from typing import TYPE_CHECKING

# ruff: noqa: F403,F405
from ._common import *  # noqa: F403,F401

if TYPE_CHECKING:
    from ._errors import _manifest_error, _promotion_error, _unsafe_error
    from ._filesystem import _BoundDirectory
    from ._fs_helpers import (
        _entry_exists_no_follow,
        _fdopen_owned,
        _fsync_directory,
        _same_lexical_path,
        _stat_identity,
        _unlink_regular,
    )
    from ._manifest import CutManifest
    from ._manifest_validation import (
        _canonical_json,
        _reject_json_constant,
        _strict_object,
        _string,
        _validate_component,
        _validate_sha256,
    )
    from ._runtime_helpers import _promotion_lock
    from ._scan import _scan_cut_set
    from ._tree_helpers import _remove_tree

__all__ = (
    "_atomic_directory_exchange",
    "_manifest_hash_if_valid",
    "_read_bound_manifest",
    "_read_manifest",
    "_recover_all_promotions",
    "_recover_promotion",
    "_write_bound_journal",
    "_write_journal",
    "_write_manifest_atomic",
)


def _read_manifest(path: Path) -> tuple[CutManifest, tuple[int, int, int, int, int]]:
    try:
        with _BoundDirectory.open(path) as bound:
            return _read_bound_manifest(bound)
    except AppError:
        raise
    except OSError as error:
        raise _manifest_error(f"cannot read manifest: {error}") from error


def _read_bound_manifest(
    bound: _BoundDirectory,
) -> tuple[CutManifest, tuple[int, int, int, int, int]]:
    try:
        descriptor = bound.open_read(MANIFEST_FILENAME)
        with _fdopen_owned(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise _unsafe_error("manifest is not a regular file")
            if not 1 <= before.st_size <= MAX_MANIFEST_BYTES:
                raise _manifest_error("manifest exceeds the bounded byte limit")
            encoded = source.read(MAX_MANIFEST_BYTES + 1)
            after = os.fstat(source.fileno())
        named = bound.lstat(MANIFEST_FILENAME)
        bound.assert_still_named()
    except AppError:
        raise
    except OSError as error:
        raise _manifest_error(f"cannot read manifest: {error}") from error
    identity = _stat_identity(before)
    if (
        len(encoded) != before.st_size
        or identity != _stat_identity(after)
        or identity != _stat_identity(named)
    ):
        raise _manifest_error("manifest changed while it was read")
    return CutManifest.from_json_bytes(encoded), identity


def _write_manifest_atomic(
    path: Path,
    manifest: CutManifest,
    *,
    expected_identity: tuple[int, int, int, int, int] | None = None,
) -> None:
    if type(manifest) is not CutManifest:
        raise _manifest_error("manifest must be an exact CutManifest")
    encoded = manifest.to_json_bytes()
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise _manifest_error("manifest exceeds the bounded byte limit")
    temporary = f".manifest-{uuid.uuid4().hex}.tmp"
    try:
        with _BoundDirectory.open(path) as bound:
            try:
                output = bound.open_new(temporary)
                try:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
                finally:
                    output.close()
                if expected_identity is not None:
                    current = bound.lstat(MANIFEST_FILENAME)
                    if _stat_identity(current) != expected_identity:
                        raise _manifest_error(
                            "manifest changed before the atomic update committed"
                        )
                bound.replace(temporary, MANIFEST_FILENAME)
                bound.fsync()
                bound.assert_still_named()
            finally:
                try:
                    bound.unlink(temporary)
                except FileNotFoundError:
                    pass
    except AppError:
        raise
    except OSError as error:
        raise _manifest_error(f"cannot atomically write manifest: {error}") from error


def _write_journal(
    path: Path,
    payload: Mapping[str, object],
    *,
    bound: _BoundDirectory | None = None,
) -> None:
    encoded = _canonical_json(dict(payload)) + b"\n"
    if len(encoded) > 16 * 1024:
        raise _promotion_error("promotion journal exceeds its byte bound")
    if bound is not None:
        if not _same_lexical_path(bound.path, path.parent):
            raise _unsafe_error("promotion journal bound to the wrong directory")
        _write_bound_journal(bound, path.name, encoded)
        return
    with _BoundDirectory.open(path.parent) as opened_bound:
        _write_bound_journal(opened_bound, path.name, encoded)


def _write_bound_journal(bound: _BoundDirectory, name: str, encoded: bytes) -> None:
    _validate_component(name)
    temporary = f".{name}-{uuid.uuid4().hex}.tmp"
    try:
        output = bound.open_new(temporary)
        try:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        finally:
            output.close()
        bound.replace(temporary, name)
        bound.fsync()
    finally:
        try:
            bound.unlink(temporary)
        except FileNotFoundError:
            pass


def _recover_all_promotions(cuts_root: Path) -> None:
    with _BoundDirectory.open(cuts_root) as bound:
        keys: list[str] = []
        for name, info in bound.iter_entries():
            match = _MARKER_RE.fullmatch(name)
            if match is None:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise _unsafe_error("promotion recovery marker is redirected")
            if len(keys) >= MAX_WORKSPACE_ENTRIES:
                raise _unsafe_error("promotion recovery marker count is unbounded")
            keys.append(match.group(1))
    for key in keys:
        with _promotion_lock(str(cuts_root / key)):
            _recover_promotion(cuts_root, key)


def _recover_promotion(cuts_root: Path, cache_key: str) -> None:
    marker = cuts_root / f".replace-{cache_key}.json"
    try:
        try:
            with _BoundDirectory.open(cuts_root) as bound:
                marker_info = bound.lstat(marker.name)
                if stat.S_ISLNK(marker_info.st_mode) or not stat.S_ISREG(
                    marker_info.st_mode
                ):
                    raise _unsafe_error("promotion recovery marker is redirected")
                descriptor = bound.open_read(marker.name)
                with _fdopen_owned(descriptor, "rb") as source:
                    opened_before = os.fstat(source.fileno())
                    if not 1 <= opened_before.st_size <= 16 * 1024:
                        raise _promotion_error(
                            "promotion journal exceeds its byte bound"
                        )
                    data = source.read(16 * 1024 + 1)
                    opened_after = os.fstat(source.fileno())
                named_after = bound.lstat(marker.name)
                bound.assert_still_named()
        except FileNotFoundError:
            return
        marker_identity = _stat_identity(marker_info)
        if (
            len(data) != marker_info.st_size
            or marker_identity != _stat_identity(opened_before)
            or marker_identity != _stat_identity(opened_after)
            or marker_identity != _stat_identity(named_after)
        ):
            raise _promotion_error("promotion journal changed while it was read")
        payload = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(payload, dict):
            raise _promotion_error("promotion journal is not an object")
        journal_fields = {
            "backup_name",
            "cache_key",
            "candidate_manifest_sha256",
            "phase",
            "previous_manifest_sha256",
            "stage_name",
            "used_exchange",
            "version",
        }
        if set(payload) not in (journal_fields, journal_fields | {"target_name"}):
            raise _promotion_error("promotion journal contains unexpected fields")
        if payload["version"] != 1 or payload["cache_key"] != cache_key:
            raise _promotion_error("promotion journal identity is invalid")
        stage_name = _string(payload["stage_name"], "stage name")
        backup_name = _string(payload["backup_name"], "backup name")
        if _STAGE_RE.fullmatch(stage_name) is None or not stage_name.startswith(
            f".stage-{cache_key}-"
        ):
            raise _promotion_error("promotion journal stage is unsafe")
        if _BACKUP_RE.fullmatch(backup_name) is None or not backup_name.startswith(
            f".backup-{cache_key}-"
        ):
            raise _promotion_error("promotion journal backup is unsafe")
        target_name = _string(payload.get("target_name", cache_key), "target name")
        _validate_component(target_name)
        if target_name.startswith("."):
            raise _promotion_error("promotion journal target is hidden")
        candidate_hash = _string(
            payload["candidate_manifest_sha256"], "candidate manifest hash"
        )
        _validate_sha256(candidate_hash, "candidate manifest hash")
        previous_hash = payload["previous_manifest_sha256"]
        if previous_hash is not None:
            previous_hash = _string(previous_hash, "previous manifest hash")
            _validate_sha256(previous_hash, "previous manifest hash")
        target = cuts_root / target_name
        stage = cuts_root / stage_name
        backup = cuts_root / backup_name
        target_hash = _manifest_hash_if_valid(target, cache_key)
        stage_hash = _manifest_hash_if_valid(stage, cache_key)
        backup_hash = _manifest_hash_if_valid(backup, cache_key)
        if target_hash == candidate_hash:
            if stage.exists():
                _remove_tree(stage)
            if backup.exists():
                _remove_tree(backup)
            _unlink_regular(marker)
            _fsync_directory(cuts_root)
            return
        old_location: Path | None = None
        if previous_hash is not None:
            if stage_hash == previous_hash:
                old_location = stage
            elif backup_hash == previous_hash:
                old_location = backup
            elif target_hash == previous_hash:
                old_location = target
        if old_location is not None and old_location != target:
            if target.exists():
                _remove_tree(target)
            with _BoundDirectory.open(cuts_root) as bound:
                bound.replace_directory(old_location.name, target.name)
        if stage.exists() and stage != old_location:
            _remove_tree(stage)
        if backup.exists() and backup != old_location:
            _remove_tree(backup)
        _unlink_regular(marker)
        _fsync_directory(cuts_root)
    except AppError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise _promotion_error(
            f"cannot recover interrupted promotion: {error}"
        ) from error


def _manifest_hash_if_valid(path: Path, cache_key: str) -> str | None:
    if not _entry_exists_no_follow(path):
        return None
    try:
        manifest, manifest_identity = _read_manifest(path)
        if manifest.cache_key != cache_key:
            return None
        _scan_cut_set(
            path,
            manifest,
            manifest_identity,
            compare_recorded=True,
        )
    except AppError:
        return None
    return hashlib.sha256(manifest.to_json_bytes()).hexdigest()


def _atomic_directory_exchange(left: Path, right: Path) -> bool:
    """Exchange two named sibling directories atomically when supported."""
    if left.parent != right.parent:
        raise OSError(errno.EXDEV, "directory exchange requires siblings")
    with _BoundDirectory.open(left.parent) as parent:
        descriptor = parent.descriptor
        if descriptor is None:
            return False
        if sys.platform == "darwin":
            libc = ctypes.CDLL(None, use_errno=True)
            renameatx = getattr(libc, "renameatx_np", None)
            if renameatx is None:
                return False
            renameatx.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameatx.restype = ctypes.c_int
            if (
                renameatx(
                    descriptor,
                    os.fsencode(left.name),
                    descriptor,
                    os.fsencode(right.name),
                    0x00000002,
                )
                == 0
            ):
                return True
            code = ctypes.get_errno()
            if code in {errno.ENOTSUP, errno.EINVAL, errno.ENOSYS}:
                return False
            raise OSError(code, os.strerror(code))
        if sys.platform.startswith("linux"):
            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = getattr(libc, "renameat2", None)
            if renameat2 is None:
                return False
            renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            if (
                renameat2(
                    descriptor,
                    os.fsencode(left.name),
                    descriptor,
                    os.fsencode(right.name),
                    2,
                )
                == 0
            ):
                return True
            code = ctypes.get_errno()
            if code in {errno.ENOTSUP, errno.EINVAL, errno.ENOSYS}:
                return False
            raise OSError(code, os.strerror(code))
    return False
