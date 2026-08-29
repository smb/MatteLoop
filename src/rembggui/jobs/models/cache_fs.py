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
from typing import Protocol

_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400


class _WindowsDirectoryApi(Protocol):
    def open_directory(self, path: Path) -> int: ...

    def file_attributes(self, handle: int) -> int: ...

    def close_handle(self, handle: int) -> None: ...


class UnsafeCacheError(Exception):
    pass


class BoundModelDirectory:
    """A model directory whose namespace cannot be redirected while bound."""

    __slots__ = ("_fd", "_windows_api", "_windows_handles", "path")

    def __init__(
        self,
        path: Path,
        *,
        descriptor: int | None,
        windows_handles: tuple[int, ...] = (),
        windows_api: _WindowsDirectoryApi | None = None,
    ) -> None:
        self.path = path
        self._fd = descriptor
        self._windows_handles = windows_handles
        self._windows_api = windows_api

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
            api = self._windows_api
            if api is None:
                raise RuntimeError("Windows directory handles have no owner API")
            handles = self._windows_handles
            self._windows_handles = ()
            _close_windows_handles(api, handles)

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
    root: Path,
    version: str,
    model_id: str,
    *,
    create: bool,
    api: _WindowsDirectoryApi | None = None,
) -> BoundModelDirectory | None:
    active_api = api if api is not None else _CtypesWindowsDirectoryApi()
    handles: list[int] = []
    current = root
    bound: BoundModelDirectory | None = None
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
            handle = active_api.open_directory(current)
            handles.append(handle)
            attributes = active_api.file_attributes(handle)
            if not attributes & _FILE_ATTRIBUTE_DIRECTORY:
                raise UnsafeCacheError("opened model cache handle is not a directory")
            if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise UnsafeCacheError("opened model cache handle is a reparse point")
        bound = BoundModelDirectory(
            current,
            descriptor=None,
            windows_handles=tuple(handles),
            windows_api=active_api,
        )
        return bound
    finally:
        if bound is None:
            _close_windows_handles(active_api, tuple(handles))


def _close_windows_handles(api: _WindowsDirectoryApi, handles: tuple[int, ...]) -> None:
    first_error: BaseException | None = None
    for handle in reversed(handles):
        try:
            api.close_handle(handle)
        except BaseException as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


class _CtypesWindowsDirectoryApi:
    __slots__ = ("_close_handle", "_create_file", "_get_information", "_invalid")

    def __init__(self) -> None:
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
        get_information = kernel32.GetFileInformationByHandleEx
        get_information.argtypes = (
            wintypes.HANDLE,
            wintypes.INT,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        get_information.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        self._create_file = create_file
        self._get_information = get_information
        self._close_handle = close_handle
        self._invalid = wintypes.HANDLE(-1).value

    def open_directory(self, path: Path) -> int:
        import ctypes

        handle = self._create_file(
            str(path),
            0x80000000,
            0x00000001 | 0x00000002,
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        if handle == self._invalid:
            last_error = getattr(ctypes, "get_last_error")()
            raise OSError(last_error, "could not bind cache directory")
        return int(handle)

    def file_attributes(self, handle: int) -> int:
        import ctypes
        from ctypes import wintypes

        class FileAttributeTagInfo(ctypes.Structure):
            _fields_ = (
                ("FileAttributes", wintypes.DWORD),
                ("ReparseTag", wintypes.DWORD),
            )

        info = FileAttributeTagInfo()
        if not self._get_information(
            handle,
            9,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            last_error = getattr(ctypes, "get_last_error")()
            raise OSError(last_error, "could not validate cache directory handle")
        return int(info.FileAttributes)

    def close_handle(self, handle: int) -> None:
        import ctypes

        if not self._close_handle(handle):
            last_error = getattr(ctypes, "get_last_error")()
            raise OSError(last_error, "could not close cache directory handle")


def _validate_component(value: str) -> None:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise UnsafeCacheError("model cache namespace component is invalid")


def _validate_filename(value: str) -> None:
    _validate_component(value)
