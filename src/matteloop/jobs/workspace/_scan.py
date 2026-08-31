from __future__ import annotations

from typing import TYPE_CHECKING

# ruff: noqa: F403,F405
from ._common import *  # noqa: F403,F401

if TYPE_CHECKING:
    from ._errors import _cuts_changed, _set_error, _snapshot_error, _unsafe_error
    from ._filesystem import _BoundDirectory
    from ._fs_helpers import (
        _fdopen_owned,
        _hash_file,
        _sha256_fd,
        _stat_identity,
        _write_all,
    )
    from ._manifest import CutFrame, CutManifest
    from ._manifest_validation import _parse_png_header, _validate_dimensions

__all__ = (
    "_copy_frame_descriptor_bound",
    "_inspect_bound_frame",
    "_inspect_frame",
    "_scan_bound_cut_set",
    "_scan_cut_set",
    "_try_reflink",
)


def _scan_cut_set(
    path: Path,
    manifest: CutManifest,
    manifest_identity: tuple[int, int, int, int, int],
    *,
    compare_recorded: bool,
) -> tuple[tuple[CutFrame, ...], tuple[tuple[int, int, int, int, int], ...]]:
    try:
        with _BoundDirectory.open(path) as bound:
            return _scan_bound_cut_set(
                bound,
                manifest,
                manifest_identity,
                compare_recorded=compare_recorded,
            )
    except AppError:
        raise
    except OSError as error:
        raise _set_error(f"cannot inspect cut directory: {error}") from error


def _scan_bound_cut_set(
    bound: _BoundDirectory,
    manifest: CutManifest,
    manifest_identity: tuple[int, int, int, int, int],
    *,
    compare_recorded: bool,
) -> tuple[tuple[CutFrame, ...], tuple[tuple[int, int, int, int, int], ...]]:
    expected_names = {MANIFEST_FILENAME, *(frame.filename for frame in manifest.frames)}
    try:
        actual_names: set[str] = set()
        for name, info in bound.iter_entries():
            if len(actual_names) > MAX_FRAME_COUNT + 1:
                raise _set_error("cut directory entry count exceeds the bound")
            actual_names.add(name)
            if stat.S_ISLNK(info.st_mode):
                raise _unsafe_error(f"cut entry {name!r} is a symbolic link")
            if not stat.S_ISREG(info.st_mode):
                raise _set_error(f"cut entry {name!r} is not a regular file")
        if actual_names != expected_names:
            missing = sorted(expected_names - actual_names)
            unexpected = sorted(actual_names - expected_names)
            detail = "cut frame names/count are not sequential"
            if missing:
                detail += f"; missing {missing[0]!r}"
            if unexpected:
                detail += f"; unexpected {unexpected[0]!r}"
            raise _set_error(detail)
        current_manifest = bound.lstat(MANIFEST_FILENAME)
        if _stat_identity(current_manifest) != manifest_identity:
            raise _set_error("manifest changed during cut validation")
        bound.assert_still_named()
    except AppError:
        raise
    except OSError as error:
        raise _set_error(f"cannot inspect cut directory: {error}") from error
    frames: list[CutFrame] = []
    identities: list[tuple[int, int, int, int, int]] = []
    for expected in manifest.frames:
        frame, identity = _inspect_bound_frame(
            bound,
            expected,
            compare_recorded=compare_recorded,
            load_pixels=True,
        )
        if (frame.width, frame.height) != (manifest.width, manifest.height):
            raise _set_error(f"frame {frame.filename} dimensions do not match manifest")
        frames.append(frame)
        identities.append(identity)
    try:
        after_manifest = bound.lstat(MANIFEST_FILENAME)
        if _stat_identity(after_manifest) != manifest_identity:
            raise _set_error("manifest changed during frame validation")
        bound.assert_still_named()
    except AppError:
        raise
    except OSError as error:
        raise _set_error(f"manifest became unavailable: {error}") from error
    return tuple(frames), tuple(identities)


def _inspect_frame(
    directory: Path,
    expected: CutFrame,
    *,
    compare_recorded: bool,
    load_pixels: bool,
) -> tuple[CutFrame, tuple[int, int, int, int, int]]:
    try:
        with _BoundDirectory.open(directory) as bound:
            return _inspect_bound_frame(
                bound,
                expected,
                compare_recorded=compare_recorded,
                load_pixels=load_pixels,
            )
    except AppError:
        raise
    except (OSError, UnidentifiedImageError, SyntaxError, ValueError) as error:
        raise _set_error(
            f"frame {expected.filename} is not a readable PNG: {error}"
        ) from error


def _inspect_bound_frame(
    bound: _BoundDirectory,
    expected: CutFrame,
    *,
    compare_recorded: bool,
    load_pixels: bool,
) -> tuple[CutFrame, tuple[int, int, int, int, int]]:
    try:
        descriptor = bound.open_read(expected.filename)
        with _fdopen_owned(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise _unsafe_error(f"frame {expected.filename} is not regular")
            if not 1 <= before.st_size <= MAX_FRAME_FILE_BYTES:
                raise _set_error(f"frame {expected.filename} has an invalid byte size")
            header = source.read(33)
            width, height = _parse_png_header(header, expected.filename)
            _validate_dimensions(width, height)
            source.seek(0)
            with Image.open(source) as image:
                if (
                    image.format != "PNG"
                    or image.mode != "RGBA"
                    or image.size != (width, height)
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise _set_error(
                        f"frame {expected.filename} must be one exact RGBA PNG"
                    )
                if load_pixels:
                    image.load()
                else:
                    image.verify()
            source.seek(0)
            digest = _hash_file(source)
            after = os.fstat(source.fileno())
        named = bound.lstat(expected.filename)
        bound.assert_still_named()
    except AppError:
        raise
    except (OSError, UnidentifiedImageError, SyntaxError, ValueError) as error:
        raise _set_error(
            f"frame {expected.filename} is not a readable PNG: {error}"
        ) from error
    identity = _stat_identity(before)
    if identity != _stat_identity(after) or identity != _stat_identity(named):
        raise _set_error(f"frame {expected.filename} changed while it was read")
    current = CutFrame(
        expected.index,
        expected.filename,
        width,
        height,
        before.st_size,
        before.st_mtime_ns,
        digest,
    )
    if compare_recorded:
        if (current.width, current.height) != (expected.width, expected.height):
            raise _set_error(f"frame {expected.filename} dimensions changed")
        if current.size_bytes != expected.size_bytes:
            raise _set_error(f"frame {expected.filename} byte size changed")
        if current.mtime_ns != expected.mtime_ns:
            raise _set_error(f"frame {expected.filename} metadata changed")
        if current.sha256 != expected.sha256:
            raise _set_error(f"frame {expected.filename} content hash changed")
    return current, identity


def _copy_frame_descriptor_bound(
    source_directory: Path,
    destination_directory: Path,
    frame: CutFrame,
    *,
    prefer_reflink: bool,
) -> None:
    try:
        with (
            _BoundDirectory.open(source_directory) as source_bound,
            _BoundDirectory.open(destination_directory) as destination_bound,
        ):
            source_fd = source_bound.open_read(frame.filename)
            try:
                before = os.fstat(source_fd)
                if _stat_identity(source_bound.lstat(frame.filename)) != _stat_identity(
                    before
                ):
                    raise _cuts_changed(
                        f"frame {frame.filename} was redirected before copy"
                    )
                if (
                    before.st_size != frame.size_bytes
                    or before.st_mtime_ns != frame.mtime_ns
                ):
                    raise _cuts_changed(
                        f"frame {frame.filename} metadata changed before copy"
                    )
                before_hash = _sha256_fd(source_fd)
                if before_hash != frame.sha256:
                    raise _cuts_changed(f"frame {frame.filename} changed before copy")
                destination_fd: int | None = None
                if prefer_reflink:
                    destination_fd = _try_reflink(
                        source_fd, destination_bound, frame.filename
                    )
                if destination_fd is None:
                    destination_fd = destination_bound.open_new_fd(frame.filename)
                    try:
                        os.lseek(source_fd, 0, os.SEEK_SET)
                        while True:
                            chunk = os.read(source_fd, COPY_CHUNK_BYTES)
                            if not chunk:
                                break
                            _write_all(destination_fd, chunk)
                    except BaseException:
                        os.close(destination_fd)
                        destination_fd = None
                        raise
                assert destination_fd is not None
                try:
                    os.fsync(destination_fd)
                    os.utime(
                        destination_fd,
                        ns=(before.st_atime_ns, before.st_mtime_ns),
                    )
                    destination_hash = _sha256_fd(destination_fd)
                    destination_info = os.fstat(destination_fd)
                finally:
                    os.close(destination_fd)
                after_hash = _sha256_fd(source_fd)
                after = os.fstat(source_fd)
            finally:
                os.close(source_fd)
            named_after = source_bound.lstat(frame.filename)
            source_bound.assert_still_named()
            destination_bound.assert_still_named()
    except AppError:
        raise
    except OSError as error:
        raise _snapshot_error(f"cannot copy frame {frame.filename}: {error}") from error
    if (
        _stat_identity(before) != _stat_identity(after)
        or _stat_identity(before) != _stat_identity(named_after)
        or before_hash != after_hash
        or after_hash != frame.sha256
    ):
        raise _cuts_changed(f"frame {frame.filename} changed during copy")
    if (
        destination_hash != frame.sha256
        or destination_info.st_size != frame.size_bytes
        or destination_info.st_mtime_ns != frame.mtime_ns
    ):
        raise _snapshot_error(f"private copy of {frame.filename} failed verification")


def _try_reflink(
    source_fd: int, destination: _BoundDirectory, filename: str
) -> int | None:
    unsupported = {
        errno.EINVAL,
        errno.ENOTSUP,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
        errno.EXDEV,
        errno.ENOSYS,
    }
    if sys.platform.startswith("linux"):
        fcntl_module = importlib.import_module("fcntl")
        destination_fd = destination.open_new_fd(filename)
        try:
            try:
                fcntl_module.ioctl(destination_fd, 0x40049409, source_fd)
            except OSError as error:
                if error.errno not in unsupported:
                    raise
                os.close(destination_fd)
                destination_fd = -1
                destination.unlink(filename)
                return None
            return destination_fd
        except BaseException:
            if destination_fd >= 0:
                os.close(destination_fd)
            try:
                destination.unlink(filename)
            except OSError:
                pass
            raise
    if sys.platform == "darwin" and destination.descriptor is not None:
        libc = ctypes.CDLL(None, use_errno=True)
        clone = getattr(libc, "fclonefileat", None)
        if clone is None:
            return None
        clone.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        clone.restype = ctypes.c_int
        if clone(source_fd, destination.descriptor, os.fsencode(filename), 0) == 0:
            return destination.open_read_write(filename)
        code = ctypes.get_errno()
        if code in unsupported:
            return None
        raise OSError(code, os.strerror(code), filename)
    return None
