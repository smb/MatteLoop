"""Handle-bound access to one canonical model-cache directory.

POSIX operations are relative to an open directory descriptor. Windows keeps
every cache namespace directory open without delete sharing, which prevents a
parent rename/reparse-point swap while full-path file APIs are in use.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from types import TracebackType


class UnsafeCacheError(Exception):
    pass


class BoundModelDirectory:
    """A model directory whose namespace cannot be redirected while bound."""

    __slots__ = ("_fd", "_windows_handles", "path")

    def __init__(
        self,
        path: Path,
        *,
        descriptor: int | None,
        windows_handles: tuple[int, ...] = (),
    ) -> None:
        self.path = path
        self._fd = descriptor
        self._windows_handles = windows_handles

    @classmethod
    def bind(
        cls, root: Path, version: str, model_id: str, *, create: bool
    ) -> BoundModelDirectory | None:
        absolute_root = root.absolute()
        model_path = absolute_root / version / model_id
        if os.name == "nt":
            return _bind_windows(absolute_root, version, model_id, create=create)
        return _bind_posix(model_path, create=create)

    def __enter__(self) -> BoundModelDirectory:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        descriptor = self._fd
        self._fd = None
        if descriptor is not None:
            os.close(descriptor)
        if self._windows_handles:
            import ctypes
            from ctypes import wintypes

            kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            for handle in reversed(self._windows_handles):
                kernel32.CloseHandle(handle)
            self._windows_handles = ()

    def target(self, filename: str) -> Path:
        _validate_filename(filename)
        return self.path / filename

    def lstat(self, filename: str) -> os.stat_result:
        _validate_filename(filename)
        if self._fd is not None:
            return os.stat(filename, dir_fd=self._fd, follow_symlinks=False)
        return (self.path / filename).lstat()

    def open_read(self, filename: str) -> int:
        _validate_filename(filename)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if self._fd is not None:
            return os.open(filename, flags, dir_fd=self._fd)
        return os.open(self.path / filename, flags)

    def open_new(self, filename: str):  # type: ignore[no-untyped-def]
        _validate_filename(filename)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if self._fd is not None:
            descriptor = os.open(filename, flags, 0o600, dir_fd=self._fd)
        else:
            descriptor = os.open(self.path / filename, flags, 0o600)
        return os.fdopen(descriptor, "wb")

    def unlink_regular(self, filename: str) -> bool:
        _validate_filename(filename)
        try:
            info = self.lstat(filename)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise UnsafeCacheError(
                f"model cache target {filename!r} is not a regular file"
            )
        if self._fd is not None:
            os.unlink(filename, dir_fd=self._fd)
        else:
            (self.path / filename).unlink()
        return True

    def unlink_file_entry(self, filename: str) -> bool:
        """Unlink a file or symlink entry itself without following its target."""
        _validate_filename(filename)
        try:
            info = self.lstat(filename)
        except FileNotFoundError:
            return False
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            raise UnsafeCacheError(f"model cache target {filename!r} is a directory")
        if self._fd is not None:
            os.unlink(filename, dir_fd=self._fd)
        else:
            (self.path / filename).unlink()
        return True

    def replace(self, source: str, destination: str) -> None:
        _validate_filename(source)
        _validate_filename(destination)
        if self._fd is not None:
            os.replace(
                source,
                destination,
                src_dir_fd=self._fd,
                dst_dir_fd=self._fd,
            )
        else:
            os.replace(self.path / source, self.path / destination)

    def fsync(self) -> None:
        if self._fd is None:
            return
        try:
            os.fsync(self._fd)
        except OSError as error:
            if error.errno not in {
                getattr(os, "EINVAL", 22),
                getattr(os, "ENOTSUP", 45),
                getattr(os, "EBADF", 9),
            }:
                raise

    def assert_still_named(self) -> None:
        if self._fd is None:
            info = self.path.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or self.path.is_symlink()
                or self.path.is_junction()
                or not stat.S_ISDIR(info.st_mode)
            ):
                raise UnsafeCacheError("bound model cache directory was redirected")
            return
        opened = os.fstat(self._fd)
        try:
            named = self.path.lstat()
        except OSError as error:
            raise UnsafeCacheError("bound model cache directory was renamed") from error
        if (
            stat.S_ISLNK(named.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or named.st_dev != opened.st_dev
            or named.st_ino != opened.st_ino
        ):
            raise UnsafeCacheError("bound model cache directory was redirected")


def _bind_posix(path: Path, *, create: bool) -> BoundModelDirectory | None:
    parts = path.parts
    if not path.is_absolute() or not parts:
        raise UnsafeCacheError("model cache path must be absolute")
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for name in parts[1:]:
            _validate_component(name)
            try:
                child = os.open(name, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    os.close(descriptor)
                    return None
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
                child = os.open(name, flags, dir_fd=descriptor)
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise UnsafeCacheError(
                        "model cache namespace contains an unsafe component"
                    ) from error
                raise
            opened = os.fstat(child)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(child)
                raise UnsafeCacheError("model cache component is not a directory")
            os.close(descriptor)
            descriptor = child
        return BoundModelDirectory(path, descriptor=descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _bind_windows(
    root: Path, version: str, model_id: str, *, create: bool
) -> BoundModelDirectory | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    invalid = wintypes.HANDLE(-1).value
    handles: list[int] = []
    current = root
    try:
        for component in (None, version, model_id):
            if component is not None:
                _validate_component(component)
                current = current / component
            try:
                info = current.lstat()
            except FileNotFoundError:
                if not create:
                    return None
                current.mkdir(mode=0o700)
                info = current.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or current.is_symlink()
                or current.is_junction()
                or not stat.S_ISDIR(info.st_mode)
            ):
                raise UnsafeCacheError("model cache component is not a real directory")
            handle = create_file(
                str(current),
                0x80000000,
                0x00000001 | 0x00000002,
                None,
                3,
                0x02000000 | 0x00200000,
                None,
            )
            if handle == invalid:
                last_error = getattr(ctypes, "get_last_error")()
                raise OSError(last_error, "could not bind cache directory")
            handles.append(int(handle))
        return BoundModelDirectory(
            current, descriptor=None, windows_handles=tuple(handles)
        )
    except BaseException:
        for handle in reversed(handles):
            kernel32.CloseHandle(handle)
        raise


def _validate_component(value: str) -> None:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise UnsafeCacheError("model cache namespace component is invalid")


def _validate_filename(value: str) -> None:
    _validate_component(value)
