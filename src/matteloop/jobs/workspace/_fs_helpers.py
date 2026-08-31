from __future__ import annotations

from typing import TYPE_CHECKING

# ruff: noqa: F403,F405
from ._common import *  # noqa: F403,F401

if TYPE_CHECKING:
    from ._errors import _unsafe_error
    from ._filesystem import _BoundDirectory
    from ._manifest_validation import _validate_component

__all__ = (
    "_OUTPUT_LOCK_ANCHOR_SCHEMA",
    "_directory_identity",
    "_entry_exists_no_follow",
    "_fdopen_owned",
    "_fsync_directory",
    "_fsync_fd",
    "_hash_file",
    "_output_lock_anchor_payload",
    "_output_target_component_key",
    "_parse_output_lock_anchor",
    "_read_small_descriptor",
    "_same_lexical_path",
    "_sha256_fd",
    "_stat_identity",
    "_unlink_bound_regular",
    "_unlink_regular",
    "_write_all",
)


def _hash_file(source: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := source.read(COPY_CHUNK_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


def _fdopen_owned(descriptor: int, mode: str) -> BinaryIO:
    """Transfer one descriptor to a file object or close it exactly once."""
    try:
        return cast(BinaryIO, os.fdopen(descriptor, mode))
    except BaseException as primary_error:
        try:
            os.close(descriptor)
        except BaseException as close_error:
            primary_error.add_note(
                f"additional descriptor cleanup failure: {close_error}"
            )
        raise


def _sha256_fd(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, COPY_CHUNK_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError("short write while copying cut frame")
        offset += written


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _directory_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


_OUTPUT_LOCK_ANCHOR_SCHEMA = "matteloop-output-lock-anchor-v1"


def _output_lock_anchor_payload(
    directory_identity: tuple[int, int],
    lock_identity: tuple[int, int],
) -> bytes:
    values = (*directory_identity, *lock_identity)
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("output lock anchor identities must be non-negative integers")
    return (
        f"{_OUTPUT_LOCK_ANCHOR_SCHEMA}\n"
        f"{directory_identity[0]}:{directory_identity[1]}\n"
        f"{lock_identity[0]}:{lock_identity[1]}\n"
    ).encode("ascii")


def _parse_output_lock_anchor(
    payload: bytes,
) -> tuple[tuple[int, int], tuple[int, int]]:
    if not isinstance(payload, bytes) or len(payload) > 256:
        raise _unsafe_error("output transaction anchor has invalid size")
    try:
        schema, directory, lock, terminator = payload.decode("ascii").split("\n")
        directory_parts = directory.split(":")
        lock_parts = lock.split(":")
        if (
            schema != _OUTPUT_LOCK_ANCHOR_SCHEMA
            or terminator
            or len(directory_parts) != 2
            or len(lock_parts) != 2
            or any(
                not part or not part.isdecimal()
                for part in (*directory_parts, *lock_parts)
            )
        ):
            raise ValueError
        values = tuple(int(part) for part in (*directory_parts, *lock_parts))
    except (UnicodeDecodeError, ValueError) as error:
        raise _unsafe_error("output transaction anchor is malformed") from error
    return (values[0], values[1]), (values[2], values[3])


def _read_small_descriptor(descriptor: int) -> bytes:
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        payload = os.read(descriptor, 257)
        if len(payload) > 256:
            raise _unsafe_error("output transaction anchor exceeds its size bound")
        return payload
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)


def _output_target_component_key(name: str, *, platform: str) -> str:
    """Hash only a validated entry component in its filesystem name domain."""
    _validate_component(name)
    if platform not in {"windows", "darwin", "posix"}:
        raise ValueError("output target platform is invalid")
    canonical = unicodedata.normalize("NFC", name)
    if platform in {"windows", "darwin"}:
        canonical = canonical.casefold()
    encoded = f"matteloop-output-target-v1\0{platform}\0{canonical}".encode()
    return hashlib.sha256(encoded).hexdigest()


def _entry_exists_no_follow(path: Path) -> bool:
    try:
        with _BoundDirectory.open(path.parent) as bound:
            info = bound.lstat(path.name)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        raise _unsafe_error(f"workspace entry {path.name!r} is redirected")
    if not stat.S_ISDIR(info.st_mode):
        raise _unsafe_error(f"workspace entry {path.name!r} is not a directory")
    return True


def _unlink_regular(path: Path) -> None:
    with _BoundDirectory.open(path.parent) as bound:
        _unlink_bound_regular(bound, path.name)


def _unlink_bound_regular(bound: _BoundDirectory, name: str) -> None:
    _validate_component(name)
    info = bound.lstat(name)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OSError("refusing to unlink a non-regular workspace entry")
    bound.unlink(name)


def _fsync_fd(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EBADF}:
            raise


def _fsync_directory(path: Path) -> None:
    with _BoundDirectory.open(path) as bound:
        bound.fsync()


def _same_lexical_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )
