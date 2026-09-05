from __future__ import annotations

from typing import TYPE_CHECKING

from platformdirs import user_cache_dir

from matteloop.paths import (
    WORKSPACE_NAME,
    cache_subdirectory,
)

# ruff: noqa: F403,F405
from ._common import *  # noqa: F403,F401

if TYPE_CHECKING:
    from ._errors import _unsafe_error
    from ._filesystem import _BoundDirectory
    from ._manifest_validation import _validate_path_value

__all__ = (
    "WorkspaceFallback",
    "WorkspaceFallbackReason",
    "WorkspaceLayout",
    "_locality_fallback",
    "_assert_safe_directory",
    "_canonical_output_directory",
    "_create_fallback_workspace",
    "_darwin_descriptor_is_local",
    "_default_local_filesystem_probe",
    "_fallback_workspace_root",
    "_linux_mount_is_local",
    "_linux_mountinfo_is_local",
    "_windows_drive_type_is_local",
    "_workspace_layout",
    "user_cache_dir",
)


class WorkspaceFallbackReason(StrEnum):
    NETWORK_FILESYSTEM = "network-filesystem"
    LOCALITY_UNKNOWN = "locality-unknown"


@dataclass(frozen=True, slots=True)
class WorkspaceFallback:
    """Structured explanation for moving workspace intermediates locally."""

    reason: WorkspaceFallbackReason
    detail: str


@dataclass(frozen=True, slots=True)
class WorkspaceLayout:
    """Workspace paths plus locality fallback metadata for UI consumers."""

    output_directory: Path
    workspace_root: Path
    cuts_root: Path
    scratch_root: Path
    fallback: WorkspaceFallback | None = None

    @property
    def fallback_used(self) -> bool:
        return self.fallback is not None

    def __iter__(self) -> Iterator[Path]:
        """Keep the former four-value unpacking contract for existing callers."""
        yield self.output_directory
        yield self.workspace_root
        yield self.cuts_root
        yield self.scratch_root


def _workspace_layout(
    output_directory: Path, *, create: bool
) -> WorkspaceLayout:
    output = _canonical_output_directory(output_directory)
    requested_root = output / WORKSPACE_NAME
    fallback = (
        _locality_fallback(requested_root)
        if requested_root.exists()
        else _locality_fallback(output)
    )
    root = (
        _fallback_workspace_root(output) if fallback is not None else requested_root
    )
    cuts = root / "cuts"
    scratch = root / "scratch"
    if create:
        if fallback is None:
            try:
                with _BoundDirectory.open(output) as output_bound:
                    output_bound.mkdir(root.name, exist_ok=True)
                    with output_bound.open_child(root.name) as root_bound:
                        root_bound.mkdir(cuts.name, exist_ok=True)
                        root_bound.mkdir(scratch.name, exist_ok=True)
                        with root_bound.open_child(cuts.name):
                            pass
                        with root_bound.open_child(scratch.name):
                            pass
            except AppError:
                raise
            except OSError as error:
                raise _unsafe_error(
                    f"cannot create workspace directory: {error}"
                ) from error
        else:
            _create_fallback_workspace(root, cuts, scratch)
    elif root.exists():
        _assert_safe_directory(root)
        if cuts.exists():
            _assert_safe_directory(cuts)
        if scratch.exists():
            _assert_safe_directory(scratch)
    return WorkspaceLayout(output, root, cuts, scratch, fallback)


def _fallback_workspace_root(output: Path) -> Path:
    """Return the local cache workspace for a non-local output directory."""
    digest = hashlib.sha256(os.fsencode(str(output))).hexdigest()
    return cache_subdirectory("workspaces", digest)


def _create_fallback_workspace(root: Path, cuts: Path, scratch: Path) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        cuts.mkdir(exist_ok=True)
        scratch.mkdir(exist_ok=True)
        _assert_safe_directory(root)
        _assert_safe_directory(cuts)
        _assert_safe_directory(scratch)
    except AppError:
        raise
    except OSError as error:
        raise _unsafe_error(
            f"cannot create local fallback workspace directory: {error}"
        ) from error


def _canonical_output_directory(path: Path) -> Path:
    if not isinstance(path, Path):
        raise _unsafe_error("output directory must be a Path")
    _validate_path_value(path)
    if ".." in path.parts:
        raise _unsafe_error("output directory traversal is not allowed")
    absolute = Path(os.path.abspath(path))
    try:
        with _BoundDirectory.open(absolute):
            pass
    except OSError as error:
        raise _unsafe_error("output directory must be an existing directory") from error
    return absolute


def _assert_safe_directory(path: Path) -> None:
    try:
        with _BoundDirectory.open(path):
            pass
    except AppError:
        raise
    except OSError as error:
        raise _unsafe_error(f"workspace directory is unavailable: {error}") from error


def _locality_fallback(
    path: Path,
    *,
    probe: Callable[[_BoundDirectory], bool] | None = None,
) -> WorkspaceFallback | None:
    try:
        with _BoundDirectory.open(path) as bound:
            checker = _default_local_filesystem_probe if probe is None else probe
            try:
                is_local = checker(bound)
            except OSError as error:
                return WorkspaceFallback(
                    WorkspaceFallbackReason.LOCALITY_UNKNOWN,
                    f"the local-storage probe could not decide: {error}",
                )
            if not is_local:
                return WorkspaceFallback(
                    WorkspaceFallbackReason.NETWORK_FILESYSTEM,
                    "the output directory is on non-local storage",
                )
    except AppError:
        raise
    except OSError as error:
        raise _unsafe_error(f"workspace directory is unavailable: {error}") from error
    return None


def _default_local_filesystem_probe(bound: _BoundDirectory) -> bool:
    if os.name == "nt":
        text = str(bound.path)
        if text.startswith(("\\\\", "//")):
            return False
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        return _windows_drive_type_is_local(
            int(kernel32.GetDriveTypeW(str(Path(bound.path.anchor))))
        )
    descriptor = bound.descriptor
    if descriptor is None:
        return False
    flags = os.fstatvfs(descriptor).f_flag
    local_flag = getattr(os, "ST_LOCAL", None)
    if local_flag is not None:
        return bool(flags & local_flag)
    if sys.platform == "darwin":
        return _darwin_descriptor_is_local(descriptor)
    if sys.platform.startswith("linux"):
        info = os.fstat(descriptor)
        return _linux_mount_is_local(info.st_dev)
    raise OSError(errno.ENOTSUP, "host has no local-filesystem identity probe")


def _darwin_descriptor_is_local(descriptor: int) -> bool:
    class DarwinStatfs(ctypes.Structure):
        _fields_ = (
            ("f_bsize", ctypes.c_uint32),
            ("f_iosize", ctypes.c_int32),
            ("f_blocks", ctypes.c_uint64),
            ("f_bfree", ctypes.c_uint64),
            ("f_bavail", ctypes.c_uint64),
            ("f_files", ctypes.c_uint64),
            ("f_ffree", ctypes.c_uint64),
            ("f_fsid", ctypes.c_int32 * 2),
            ("f_owner", ctypes.c_uint32),
            ("f_type", ctypes.c_uint32),
            ("f_flags", ctypes.c_uint32),
            ("f_fssubtype", ctypes.c_uint32),
            ("f_fstypename", ctypes.c_char * 16),
            ("f_mntonname", ctypes.c_char * 1024),
            ("f_mntfromname", ctypes.c_char * 1024),
            ("f_reserved", ctypes.c_uint32 * 8),
        )

    filesystem = DarwinStatfs()
    libc = ctypes.CDLL(None, use_errno=True)
    fstatfs = libc.fstatfs
    fstatfs.argtypes = [ctypes.c_int, ctypes.POINTER(DarwinStatfs)]
    fstatfs.restype = ctypes.c_int
    if fstatfs(descriptor, ctypes.byref(filesystem)) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return bool(filesystem.f_flags & 0x00001000)


def _windows_drive_type_is_local(drive_type: int) -> bool:
    # DRIVE_REMOVABLE, FIXED, CDROM, and RAMDISK are local. UNKNOWN,
    # NO_ROOT_DIR, and REMOTE cannot satisfy the durable-workspace contract.
    return drive_type in {2, 3, 5, 6}


def _linux_mount_is_local(device: int) -> bool:
    major_minor = f"{os.major(device)}:{os.minor(device)}"
    with open("/proc/self/mountinfo", "rb") as source:
        encoded = source.read(MAX_MOUNTINFO_BYTES + 1)
    return _linux_mountinfo_is_local(encoded, major_minor)


def _linux_mountinfo_is_local(encoded: bytes, major_minor: str) -> bool:
    local_types = {
        "aufs",
        "btrfs",
        "erofs",
        "exfat",
        "ext2",
        "ext3",
        "ext4",
        "f2fs",
        "hfsplus",
        "iso9660",
        "jfs",
        "nilfs2",
        "ntfs",
        "ntfs3",
        "overlay",
        "ramfs",
        "reiserfs",
        "squashfs",
        "tmpfs",
        "udf",
        "ufs",
        "vfat",
        "xfs",
        "zfs",
    }
    if len(encoded) > MAX_MOUNTINFO_BYTES:
        raise OSError(errno.EOVERFLOW, "mount table exceeds its parsing bound")
    matches: list[str] = []
    for raw_line in encoded.splitlines():
        if len(raw_line) > MAX_PATH_CHARS * 4:
            raise OSError(errno.EOVERFLOW, "mount table line exceeds its bound")
        fields = raw_line.split(b" ")
        if len(fields) < 10 or fields[2].decode("ascii", "strict") != major_minor:
            continue
        try:
            separator = fields.index(b"-")
            filesystem = fields[separator + 1].decode("ascii", "strict").lower()
        except (ValueError, IndexError, UnicodeDecodeError) as error:
            raise OSError(errno.EINVAL, "mount table entry is malformed") from error
        matches.append(filesystem)
        if len(matches) > 64:
            raise OSError(errno.EOVERFLOW, "device has too many mount bindings")
    if not matches:
        raise OSError(errno.ENODEV, "workspace mount identity is unavailable")
    return all(filesystem in local_types for filesystem in matches)
