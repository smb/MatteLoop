"""Handle-bound access to one canonical model-cache directory.

POSIX operations are relative to an open directory descriptor. Windows opens
only the volume/share anchor by full path, then keeps every descendant handle
bound without write/delete sharing and performs cache I/O handle-relatively.
Even an in-place reparse mutation cannot redirect an operation to another tree.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
from collections.abc import Iterator
from pathlib import Path, PurePath, PureWindowsPath
from types import TracebackType
from typing import Protocol

_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_WINDOWS_DIRECTORY_ACCESS = 0x80000000
_WINDOWS_WRITABLE_DIRECTORY_ACCESS = 0xC0000000
_WINDOWS_DIRECTORY_SHARE = 0x00000001
_WINDOWS_PUBLICATION_SHARE = 0x00000001 | 0x00000002 | 0x00000004
# A publication parent is deliberately distinct from the restrictive model-cache
# namespace.  It must create/remove children, serve as a relative rename target,
# and remain compatible with the open publication file handles.
_WINDOWS_PUBLICATION_DIRECTORY_ACCESS = (
    0x00000001  # FILE_LIST_DIRECTORY
    | 0x00000002  # FILE_ADD_FILE
    | 0x00000004  # FILE_ADD_SUBDIRECTORY
    | 0x00000020  # FILE_TRAVERSE
    | 0x00000040  # FILE_DELETE_CHILD
    | 0x00000080  # FILE_READ_ATTRIBUTES
    | 0x00100000  # SYNCHRONIZE
)
_WINDOWS_DIRECTORY_FLAGS = 0x02000000 | 0x00200000


_LOGGER = logging.getLogger(__name__)

class _WindowsDirectoryApi(Protocol):
    def open_anchor(
        self,
        path: Path,
        *,
        desired_access: int,
        share_mode: int,
        flags: int,
    ) -> int: ...

    def open_child_directory(
        self,
        parent_handle: int,
        name: str,
        *,
        create: bool,
        desired_access: int,
        share_mode: int,
        flags: int,
    ) -> int: ...

    def open_publication_anchor(
        self,
        path: Path,
        *,
        desired_access: int,
        share_mode: int,
        flags: int,
    ) -> int: ...

    def open_publication_child_directory(
        self,
        parent_handle: int,
        name: str,
        *,
        create: bool,
        desired_access: int,
        share_mode: int,
        flags: int,
    ) -> int: ...

    def file_attributes(self, handle: int) -> int: ...

    def close_handle(self, handle: int) -> None: ...

    def lstat_at(self, directory_handle: int, filename: str) -> os.stat_result: ...

    def publication_lstat_at(
        self, directory_handle: int, filename: str
    ) -> os.stat_result: ...

    def open_read_at(self, directory_handle: int, filename: str) -> int: ...

    def open_publication_read_at(self, directory_handle: int, filename: str) -> int: ...

    def open_publication_read_write_at(
        self, directory_handle: int, filename: str
    ) -> int: ...

    def open_new_at(self, directory_handle: int, filename: str) -> int: ...

    def open_new_read_write_at(self, directory_handle: int, filename: str) -> int: ...

    def open_new_publication_read_write_at(
        self, directory_handle: int, filename: str
    ) -> int: ...

    def mkdir_at(self, directory_handle: int, name: str, *, exist_ok: bool) -> None: ...

    def iter_entries_at(
        self, directory_handle: int, *, max_entries: int
    ) -> Iterator[tuple[str, os.stat_result]]: ...

    def unlink_at(
        self, directory_handle: int, filename: str, *, require_regular: bool
    ) -> None: ...

    def replace_at(
        self, directory_handle: int, source: str, destination: str
    ) -> None: ...

    def replace_publication_at(
        self, directory_handle: int, source: str, destination: str
    ) -> None: ...

    def rename_publication_at(
        self,
        source_directory_handle: int,
        source: str,
        destination_directory_handle: int,
        destination: str,
        *,
        replace: bool,
    ) -> None: ...

    def replace_directory_at(
        self, directory_handle: int, source: str, destination: str
    ) -> None: ...

    def rmdir_at(self, directory_handle: int, name: str) -> None: ...

    def flush_directory(self, directory_handle: int) -> None: ...

    def flush_directory_strict(self, directory_handle: int) -> None: ...

    def assert_directory_handle(self, directory_handle: int) -> None: ...


class UnsafeCacheError(Exception):
    pass


class BoundDirectoryCloseError(Exception):
    """A bound-directory cleanup failure with its interrupted error retained."""

    __slots__ = ("close_error", "primary_error")

    def __init__(
        self, close_error: OSError, primary_error: BaseException | None
    ) -> None:
        self.close_error = close_error
        self.primary_error = primary_error
        super().__init__(f"could not close bound model cache: {close_error}")


class FileDescriptorCloseError(OSError):
    """An fd-wrapper failure whose attempted descriptor cleanup also failed."""

    __slots__ = ("close_error", "primary_error")

    def __init__(
        self, close_error: BaseException, primary_error: BaseException
    ) -> None:
        self.close_error = close_error
        self.primary_error = primary_error
        super().__init__(
            "could not close model-cache descriptor after file wrapper failure"
        )


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
        if os.name == "nt":
            return _bind_windows(root, version, model_id, create=create)
        absolute_root = root.absolute()
        model_path = absolute_root / version / model_id
        return _bind_posix(model_path, create=create)

    def __enter__(self) -> BoundModelDirectory:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.close()
        except OSError as error:
            failure = BoundDirectoryCloseError(error, exc_value)
            if exc_value is not None:
                raise failure from exc_value
            raise failure from error

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
        if self._windows_handles:
            api = self._require_windows_api()
            return api.lstat_at(self._windows_handles[-1], filename)
        return (self.path / filename).lstat()

    def open_read(self, filename: str) -> int:
        _validate_filename(filename)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if self._fd is not None:
            return os.open(filename, flags, dir_fd=self._fd)
        if self._windows_handles:
            api = self._require_windows_api()
            return api.open_read_at(self._windows_handles[-1], filename)
        return os.open(self.path / filename, flags)

    def open_new(self, filename: str):  # type: ignore[no-untyped-def]
        _validate_filename(filename)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if self._fd is not None:
            descriptor = os.open(filename, flags, 0o600, dir_fd=self._fd)
        elif self._windows_handles:
            api = self._require_windows_api()
            descriptor = api.open_new_at(self._windows_handles[-1], filename)
        else:
            descriptor = os.open(self.path / filename, flags, 0o600)
        try:
            return os.fdopen(descriptor, "wb")
        except BaseException as primary_error:
            try:
                os.close(descriptor)
            except BaseException as close_error:
                raise FileDescriptorCloseError(
                    close_error, primary_error
                ) from primary_error
            raise

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
        elif self._windows_handles:
            api = self._require_windows_api()
            api.unlink_at(self._windows_handles[-1], filename, require_regular=True)
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
        elif self._windows_handles:
            api = self._require_windows_api()
            api.unlink_at(self._windows_handles[-1], filename, require_regular=False)
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
        elif self._windows_handles:
            api = self._require_windows_api()
            api.replace_at(self._windows_handles[-1], source, destination)
        else:
            os.replace(self.path / source, self.path / destination)

    def fsync(self) -> None:
        if self._windows_handles:
            api = self._require_windows_api()
            api.flush_directory(self._windows_handles[-1])
            return
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
        if self._windows_handles:
            api = self._require_windows_api()
            api.assert_directory_handle(self._windows_handles[-1])
            return
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

    def _require_windows_api(self) -> _WindowsDirectoryApi:
        api = self._windows_api
        if api is None:
            raise RuntimeError("Windows directory handles have no owner API")
        return api


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
    chain = _windows_path_chain(root, version, model_id)
    current = chain[-1]
    bound: BoundModelDirectory | None = None
    try:
        anchor = chain[0]
        try:
            anchor_handle = active_api.open_anchor(
                anchor,
                desired_access=_WINDOWS_DIRECTORY_ACCESS,
                share_mode=_WINDOWS_DIRECTORY_SHARE,
                flags=_WINDOWS_DIRECTORY_FLAGS,
            )
        except FileNotFoundError:
            raise UnsafeCacheError(
                "model cache volume or share anchor is unavailable"
            ) from None
        handles.append(anchor_handle)
        _validate_windows_directory_handle(active_api, anchor_handle)
        for component_path in chain[1:]:
            try:
                handle = active_api.open_child_directory(
                    handles[-1],
                    component_path.name,
                    create=create,
                    desired_access=_WINDOWS_DIRECTORY_ACCESS,
                    share_mode=_WINDOWS_DIRECTORY_SHARE,
                    flags=_WINDOWS_DIRECTORY_FLAGS,
                )
            except FileNotFoundError as error:
                _LOGGER.warning(
                    "model cache directory %s could not be opened: %s",
                    component_path,
                    error,
                )
                return None
            handles.append(handle)
            _validate_windows_directory_handle(active_api, handle)
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


def _validate_windows_directory_handle(api: _WindowsDirectoryApi, handle: int) -> None:
    attributes = api.file_attributes(handle)
    if not attributes & _FILE_ATTRIBUTE_DIRECTORY:
        raise UnsafeCacheError("opened model cache handle is not a directory")
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise UnsafeCacheError("opened model cache handle is a reparse point")


def _windows_path_chain[PathT: PurePath](
    root: PathT, version: str, model_id: str
) -> tuple[PathT, ...]:
    """Return the lexical anchor-to-model chain without resolving any segment."""
    if not root.is_absolute() or not root.anchor or not root.parts:
        raise UnsafeCacheError("model cache path must be absolute")
    if isinstance(root, PureWindowsPath):
        _validate_windows_anchor(root)
        windows_path = True
    else:
        windows_path = False
    if root.parts[0] != root.anchor:
        raise UnsafeCacheError("model cache path has an invalid anchor")

    current = type(root)(root.anchor)
    chain = [current]
    for component in (*root.parts[1:], version, model_id):
        if windows_path:
            _validate_windows_component(component)
        else:
            _validate_component(component)
        current = current / component
        chain.append(current)
    return tuple(chain)


def _validate_windows_anchor(root: PureWindowsPath) -> None:
    drive = root.drive
    anchor = root.anchor
    if root.root != "\\" or not drive:
        raise UnsafeCacheError("model cache path must be absolute")
    if drive.startswith("\\\\"):
        lowered = drive.casefold()
        if lowered.startswith(("\\\\?\\", "\\\\.\\")):
            raise UnsafeCacheError("model cache path has an invalid Windows anchor")
        unc_parts = [part for part in drive[2:].split("\\") if part]
        if len(unc_parts) != 2:
            raise UnsafeCacheError("model cache path has an invalid UNC share anchor")
        for component in unc_parts:
            _validate_windows_component(component)
        return
    if (
        len(drive) != 2
        or drive[1] != ":"
        or not drive[0].isascii()
        or not drive[0].isalpha()
    ):
        raise UnsafeCacheError("model cache path has an invalid drive anchor")
    if anchor != f"{drive}\\":
        raise UnsafeCacheError("model cache path has an invalid Windows anchor")


def _validate_windows_component(value: str) -> None:
    _validate_component(value)
    if (
        value.endswith((" ", "."))
        or any(character in '<>:"|?*' for character in value)
        or any(ord(character) < 32 for character in value)
    ):
        raise UnsafeCacheError("model cache namespace component is invalid")


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
    __slots__ = (
        "_close_handle",
        "_create_file",
        "_flush_file_buffers",
        "_get_file_information",
        "_get_information",
        "_invalid",
        "_nt_create_file",
        "_nt_set_information_file",
        "_rtl_nt_status_to_dos_error",
        "_set_file_information",
    )

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
        get_file_information = kernel32.GetFileInformationByHandle
        get_file_information.argtypes = (wintypes.HANDLE, wintypes.LPVOID)
        get_file_information.restype = wintypes.BOOL
        set_file_information = kernel32.SetFileInformationByHandle
        set_file_information.argtypes = (
            wintypes.HANDLE,
            wintypes.INT,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        set_file_information.restype = wintypes.BOOL
        flush_file_buffers = kernel32.FlushFileBuffers
        flush_file_buffers.argtypes = (wintypes.HANDLE,)
        flush_file_buffers.restype = wintypes.BOOL
        ntdll = getattr(ctypes, "WinDLL")("ntdll", use_last_error=True)
        nt_create_file = ntdll.NtCreateFile
        nt_create_file.argtypes = (
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        nt_create_file.restype = wintypes.LONG
        nt_set_information_file = ntdll.NtSetInformationFile
        nt_set_information_file.argtypes = (
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.ULONG,
            wintypes.INT,
        )
        nt_set_information_file.restype = wintypes.LONG
        rtl_nt_status_to_dos_error = ntdll.RtlNtStatusToDosError
        rtl_nt_status_to_dos_error.argtypes = (wintypes.LONG,)
        rtl_nt_status_to_dos_error.restype = wintypes.ULONG
        self._create_file = create_file
        self._get_information = get_information
        self._get_file_information = get_file_information
        self._set_file_information = set_file_information
        self._flush_file_buffers = flush_file_buffers
        self._close_handle = close_handle
        self._nt_create_file = nt_create_file
        self._nt_set_information_file = nt_set_information_file
        self._rtl_nt_status_to_dos_error = rtl_nt_status_to_dos_error
        self._invalid = wintypes.HANDLE(-1).value

    def open_anchor(
        self,
        path: Path,
        *,
        desired_access: int,
        share_mode: int,
        flags: int,
    ) -> int:
        import ctypes

        if (
            desired_access
            not in {_WINDOWS_DIRECTORY_ACCESS, _WINDOWS_WRITABLE_DIRECTORY_ACCESS}
            or share_mode != _WINDOWS_DIRECTORY_SHARE
            or flags != _WINDOWS_DIRECTORY_FLAGS
        ):
            raise ValueError("Windows cache directory open is not hardened")
        handle = self._create_file(
            str(path),
            desired_access,
            share_mode,
            None,
            3,
            flags,
            None,
        )
        if handle == self._invalid:
            last_error = getattr(ctypes, "get_last_error")()
            if last_error in {2, 3}:
                raise FileNotFoundError(
                    last_error, "cache directory does not exist", str(path)
                )
            if last_error == 5:
                raise PermissionError(
                    last_error, "could not bind cache directory", str(path)
                )
            raise OSError(last_error, "could not bind cache directory")
        return int(handle)

    def open_child_directory(
        self,
        parent_handle: int,
        name: str,
        *,
        create: bool,
        desired_access: int,
        share_mode: int,
        flags: int,
    ) -> int:
        _validate_windows_component(name)
        self._require_hardened_open(desired_access, share_mode, flags)
        return self._open_relative(
            parent_handle,
            name,
            desired_access=desired_access,
            share_mode=share_mode,
            disposition=3 if create else 1,
            options=0x00000001 | 0x00000020 | 0x00200000,
        )

    def open_publication_anchor(
        self,
        path: Path,
        *,
        desired_access: int,
        share_mode: int,
        flags: int,
    ) -> int:
        import ctypes

        self._require_publication_directory_open(desired_access, share_mode, flags)
        handle = self._create_file(
            str(path),
            desired_access,
            share_mode,
            None,
            3,
            flags,
            None,
        )
        if handle == self._invalid:
            last_error = getattr(ctypes, "get_last_error")()
            if last_error in {2, 3}:
                raise FileNotFoundError(
                    last_error, "output directory does not exist", str(path)
                )
            if last_error == 5:
                raise PermissionError(
                    last_error, "could not bind output directory", str(path)
                )
            raise OSError(last_error, "could not bind output directory")
        return int(handle)

    def open_publication_child_directory(
        self,
        parent_handle: int,
        name: str,
        *,
        create: bool,
        desired_access: int,
        share_mode: int,
        flags: int,
    ) -> int:
        _validate_windows_component(name)
        self._require_publication_directory_open(desired_access, share_mode, flags)
        return self._open_relative(
            parent_handle,
            name,
            desired_access=desired_access,
            share_mode=share_mode,
            disposition=3 if create else 1,
            options=0x00000001 | 0x00000020 | 0x00200000,
        )

    def lstat_at(self, directory_handle: int, filename: str) -> os.stat_result:
        return self._lstat_at(
            directory_handle,
            filename,
            share_mode=_WINDOWS_DIRECTORY_SHARE,
        )

    def publication_lstat_at(
        self, directory_handle: int, filename: str
    ) -> os.stat_result:
        return self._lstat_at(
            directory_handle,
            filename,
            share_mode=_WINDOWS_PUBLICATION_SHARE,
        )

    def _lstat_at(
        self,
        directory_handle: int,
        filename: str,
        *,
        share_mode: int,
    ) -> os.stat_result:
        _validate_windows_component(filename)
        handle = self._open_relative(
            directory_handle,
            filename,
            desired_access=0x00000080 | 0x00100000,
            share_mode=share_mode,
            disposition=1,
            options=0x00000020 | 0x00200000,
        )
        try:
            return self._stat_handle(handle)
        finally:
            self.close_handle(handle)

    def open_read_at(self, directory_handle: int, filename: str) -> int:
        return self._open_read_at(
            directory_handle,
            filename,
            share_mode=_WINDOWS_DIRECTORY_SHARE,
        )

    def open_publication_read_at(self, directory_handle: int, filename: str) -> int:
        return self._open_read_at(
            directory_handle,
            filename,
            share_mode=_WINDOWS_PUBLICATION_SHARE,
        )

    def open_publication_read_write_at(
        self, directory_handle: int, filename: str
    ) -> int:
        _validate_windows_component(filename)
        handle = self._open_relative(
            directory_handle,
            filename,
            desired_access=0x80000000 | 0x40000000,
            share_mode=_WINDOWS_PUBLICATION_SHARE,
            disposition=1,
            options=0x00000020 | 0x00000040 | 0x00200000,
        )
        try:
            self._require_regular_file_handle(handle)
            return self._handle_to_fd(handle, os.O_RDWR)
        except BaseException:
            self.close_handle(handle)
            raise

    def _open_read_at(
        self,
        directory_handle: int,
        filename: str,
        *,
        share_mode: int,
    ) -> int:
        _validate_windows_component(filename)
        handle = self._open_relative(
            directory_handle,
            filename,
            desired_access=0x80000000,
            share_mode=share_mode,
            disposition=1,
            options=0x00000020 | 0x00000040 | 0x00200000,
        )
        try:
            self._require_regular_file_handle(handle)
            return self._handle_to_fd(handle, os.O_RDONLY)
        except BaseException:
            self.close_handle(handle)
            raise

    def open_new_at(self, directory_handle: int, filename: str) -> int:
        return self._open_new_at(
            directory_handle,
            filename,
            os.O_WRONLY,
            share_mode=_WINDOWS_DIRECTORY_SHARE,
        )

    def open_new_read_write_at(self, directory_handle: int, filename: str) -> int:
        return self._open_new_at(
            directory_handle,
            filename,
            os.O_RDWR,
            share_mode=_WINDOWS_DIRECTORY_SHARE,
        )

    def open_new_publication_read_write_at(
        self, directory_handle: int, filename: str
    ) -> int:
        return self._open_new_at(
            directory_handle,
            filename,
            os.O_RDWR,
            share_mode=_WINDOWS_PUBLICATION_SHARE,
        )

    def mkdir_at(self, directory_handle: int, name: str, *, exist_ok: bool) -> None:
        _validate_windows_component(name)
        handle = self._open_relative(
            directory_handle,
            name,
            desired_access=_WINDOWS_WRITABLE_DIRECTORY_ACCESS,
            share_mode=_WINDOWS_DIRECTORY_SHARE,
            disposition=3 if exist_ok else 2,
            options=0x00000001 | 0x00000020 | 0x00200000,
        )
        try:
            _validate_windows_directory_handle(self, handle)
        finally:
            self.close_handle(handle)
        try:
            self.flush_directory_strict(directory_handle)
        except OSError as error:
            if not exist_ok:
                try:
                    self.rmdir_at(directory_handle, name)
                except OSError as cleanup_error:
                    error.add_note(
                        f"additional directory-create cleanup failure: {cleanup_error}"
                    )
            raise

    def iter_entries_at(
        self, directory_handle: int, *, max_entries: int
    ) -> Iterator[tuple[str, os.stat_result]]:
        import ctypes

        if not 1 <= max_entries <= 1_000_000:
            raise ValueError("directory enumeration bound is invalid")

        class FileIdBothDirectoryInfo(ctypes.Structure):
            _fields_ = (
                ("NextEntryOffset", ctypes.c_uint32),
                ("FileIndex", ctypes.c_uint32),
                ("CreationTime", ctypes.c_int64),
                ("LastAccessTime", ctypes.c_int64),
                ("LastWriteTime", ctypes.c_int64),
                ("ChangeTime", ctypes.c_int64),
                ("EndOfFile", ctypes.c_int64),
                ("AllocationSize", ctypes.c_int64),
                ("FileAttributes", ctypes.c_uint32),
                ("FileNameLength", ctypes.c_uint32),
                ("EaSize", ctypes.c_uint32),
                ("ShortNameLength", ctypes.c_ubyte),
                ("ShortName", ctypes.c_uint16 * 12),
                ("FileId", ctypes.c_int64),
                ("FileName", ctypes.c_uint16 * 1),
            )

        buffer_size = 64 * 1024
        buffer = ctypes.create_string_buffer(buffer_size)
        restart = True
        yielded = 0
        while True:
            info_class = 11 if restart else 10
            restart = False
            if not self._get_information(
                directory_handle, info_class, buffer, buffer_size
            ):
                code = int(getattr(ctypes, "get_last_error")())
                if code == 18:
                    return
                raise OSError(code, "could not enumerate bound cache directory")
            offset = 0
            while True:
                header_end = offset + FileIdBothDirectoryInfo.FileName.offset
                if header_end > buffer_size:
                    raise OSError(
                        errno.EOVERFLOW, "directory entry header is truncated"
                    )
                address = ctypes.addressof(buffer) + offset
                info = ctypes.cast(
                    address, ctypes.POINTER(FileIdBothDirectoryInfo)
                ).contents
                name_bytes = int(info.FileNameLength)
                if (
                    name_bytes <= 0
                    or name_bytes > 510
                    or name_bytes % 2
                    or header_end + name_bytes > buffer_size
                ):
                    raise OSError(
                        errno.EOVERFLOW, "directory entry name exceeds its bound"
                    )
                encoded_name = ctypes.string_at(
                    address + FileIdBothDirectoryInfo.FileName.offset,
                    name_bytes,
                )
                try:
                    name = encoded_name.decode("utf-16-le", "strict")
                except UnicodeDecodeError as error:
                    raise OSError(
                        errno.EINVAL, "directory entry name is not valid UTF-16"
                    ) from error
                if name not in {".", ".."}:
                    _validate_windows_component(name)
                    yielded += 1
                    if yielded > max_entries:
                        raise OSError(
                            errno.EOVERFLOW, "directory entry count exceeds its bound"
                        )
                    yield name, self.lstat_at(directory_handle, name)
                next_offset = int(info.NextEntryOffset)
                if next_offset == 0:
                    break
                if next_offset < FileIdBothDirectoryInfo.FileName.offset:
                    raise OSError(
                        errno.EOVERFLOW, "directory entry offset is malformed"
                    )
                offset += next_offset
                if offset >= buffer_size:
                    raise OSError(
                        errno.EOVERFLOW, "directory entry offset exceeds its buffer"
                    )

    def _open_new_at(
        self,
        directory_handle: int,
        filename: str,
        descriptor_flags: int,
        *,
        share_mode: int,
    ) -> int:
        _validate_windows_component(filename)
        handle = self._open_relative(
            directory_handle,
            filename,
            desired_access=0x40000000 | 0x80000000,
            share_mode=share_mode,
            disposition=2,
            options=0x00000020 | 0x00000040 | 0x00200000,
        )
        try:
            self._require_regular_file_handle(handle)
            return self._handle_to_fd(handle, descriptor_flags)
        except BaseException:
            self.close_handle(handle)
            raise

    def unlink_at(
        self, directory_handle: int, filename: str, *, require_regular: bool
    ) -> None:
        import ctypes

        _validate_windows_component(filename)
        handle = self._open_relative(
            directory_handle,
            filename,
            desired_access=0x00010000 | 0x00000080 | 0x00100000,
            share_mode=0x00000001 | 0x00000002 | 0x00000004,
            disposition=1,
            options=0x00000020 | 0x00200000,
        )

        class FileDispositionInfo(ctypes.Structure):
            _fields_ = (("DeleteFile", ctypes.c_ubyte),)

        try:
            attributes = self.file_attributes(handle)
            if require_regular and (
                attributes & (_FILE_ATTRIBUTE_DIRECTORY | _FILE_ATTRIBUTE_REPARSE_POINT)
            ):
                raise UnsafeCacheError("model cache target is not a regular file")
            if (
                not require_regular
                and attributes & _FILE_ATTRIBUTE_DIRECTORY
                and not attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise UnsafeCacheError("model cache target is a directory")
            info = FileDispositionInfo(True)
            if not self._set_file_information(
                handle,
                4,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                self._raise_last_error("could not unlink bound cache entry")
        finally:
            self.close_handle(handle)

    def replace_at(self, directory_handle: int, source: str, destination: str) -> None:
        self._replace_at(
            directory_handle,
            source,
            destination,
            require_directory=False,
            share_mode=_WINDOWS_DIRECTORY_SHARE,
            destination_directory_handle=None,
            replace=True,
        )

    def replace_publication_at(
        self, directory_handle: int, source: str, destination: str
    ) -> None:
        self._replace_at(
            directory_handle,
            source,
            destination,
            require_directory=False,
            share_mode=_WINDOWS_PUBLICATION_SHARE,
            destination_directory_handle=directory_handle,
            replace=True,
        )

    def rename_publication_at(
        self,
        source_directory_handle: int,
        source: str,
        destination_directory_handle: int,
        destination: str,
        *,
        replace: bool,
    ) -> None:
        self._replace_at(
            source_directory_handle,
            source,
            destination,
            require_directory=False,
            share_mode=_WINDOWS_PUBLICATION_SHARE,
            destination_directory_handle=destination_directory_handle,
            replace=replace,
        )

    def replace_directory_at(
        self, directory_handle: int, source: str, destination: str
    ) -> None:
        self._replace_at(
            directory_handle,
            source,
            destination,
            require_directory=True,
            share_mode=_WINDOWS_DIRECTORY_SHARE,
            destination_directory_handle=None,
            replace=True,
        )

    def _replace_at(
        self,
        directory_handle: int,
        source: str,
        destination: str,
        *,
        require_directory: bool,
        share_mode: int,
        destination_directory_handle: int | None,
        replace: bool,
    ) -> None:
        import ctypes

        _validate_windows_component(source)
        _validate_windows_component(destination)
        source_handle = self._open_relative(
            directory_handle,
            source,
            desired_access=0x00010000 | 0x00000080 | 0x00100000,
            share_mode=share_mode,
            disposition=1,
            options=(
                0x00000020
                | (0x00000001 if require_directory else 0x00000040)
                | 0x00200000
            ),
        )

        class IoStatusBlock(ctypes.Structure):
            _fields_ = (
                ("Status", ctypes.c_void_p),
                ("Information", ctypes.c_size_t),
            )

        try:
            if require_directory:
                attributes = self.file_attributes(source_handle)
                if not attributes & _FILE_ATTRIBUTE_DIRECTORY or attributes & (
                    _FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise UnsafeCacheError(
                        "cache rename source is not a safe directory"
                    )
            else:
                self._require_regular_file_handle(source_handle)
            encoded = destination.encode("utf-16-le")
            if destination_directory_handle is None:

                class FileRenameInformation(ctypes.Structure):
                    _fields_ = (
                        ("ReplaceIfExists", ctypes.c_ubyte),
                        ("RootDirectory", ctypes.c_void_p),
                        ("FileNameLength", ctypes.c_uint32),
                        ("FileName", ctypes.c_uint16 * 1),
                    )

                filename_offset = FileRenameInformation.FileName.offset
                buffer = ctypes.create_string_buffer(
                    ctypes.sizeof(FileRenameInformation) + len(encoded)
                )
                legacy_info = ctypes.cast(
                    buffer, ctypes.POINTER(FileRenameInformation)
                ).contents
                legacy_info.ReplaceIfExists = replace
                legacy_info.RootDirectory = None
                legacy_info.FileNameLength = len(encoded)
                information_class = 10
            else:

                class FileRenameInformationEx(ctypes.Structure):
                    _fields_ = (
                        ("Flags", ctypes.c_uint32),
                        ("RootDirectory", ctypes.c_void_p),
                        ("FileNameLength", ctypes.c_uint32),
                        ("FileName", ctypes.c_uint16 * 1),
                    )

                filename_offset = FileRenameInformationEx.FileName.offset
                buffer = ctypes.create_string_buffer(
                    ctypes.sizeof(FileRenameInformationEx) + len(encoded)
                )
                publication_info = ctypes.cast(
                    buffer, ctypes.POINTER(FileRenameInformationEx)
                ).contents
                # FILE_RENAME_POSIX_SEMANTICS allows a rename while compatible
                # publication handles remain open; replace is policy-specific.
                publication_info.Flags = 0x00000002 | (0x00000001 if replace else 0)
                publication_info.RootDirectory = destination_directory_handle
                publication_info.FileNameLength = len(encoded)
                information_class = 65
            ctypes.memmove(
                ctypes.addressof(buffer) + filename_offset,
                encoded,
                len(encoded),
            )
            io_status = IoStatusBlock()
            status = int(
                self._nt_set_information_file(
                    source_handle,
                    ctypes.byref(io_status),
                    buffer,
                    len(buffer),
                    information_class,
                )
            )
            if status < 0:
                self._raise_nt_status(status, destination)
        finally:
            self.close_handle(source_handle)

    def rmdir_at(self, directory_handle: int, name: str) -> None:
        import ctypes

        _validate_windows_component(name)
        handle = self._open_relative(
            directory_handle,
            name,
            desired_access=0x00010000 | 0x00000080 | 0x00100000,
            share_mode=0x00000001 | 0x00000002 | 0x00000004,
            disposition=1,
            options=0x00000001 | 0x00000020 | 0x00200000,
        )

        class FileDispositionInfo(ctypes.Structure):
            _fields_ = (("DeleteFile", ctypes.c_ubyte),)

        try:
            attributes = self.file_attributes(handle)
            if not attributes & _FILE_ATTRIBUTE_DIRECTORY or attributes & (
                _FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise UnsafeCacheError("cache removal target is not a directory")
            info = FileDispositionInfo(True)
            if not self._set_file_information(
                handle, 4, ctypes.byref(info), ctypes.sizeof(info)
            ):
                self._raise_last_error("could not remove bound cache directory")
        finally:
            self.close_handle(handle)

    def flush_directory(self, directory_handle: int) -> None:
        import ctypes

        # The artifact file itself was already flushed through its CRT fd.
        # Windows may reject FlushFileBuffers on our read-only directory handle;
        # directory durability is best-effort rather than weakening the binding
        # by reopening this handle with GENERIC_WRITE.
        if self._flush_file_buffers(directory_handle):
            return
        last_error = getattr(ctypes, "get_last_error")()
        if last_error in {1, 5, 6, 50}:
            return
        raise OSError(last_error, "could not flush bound cache directory")

    def flush_directory_strict(self, directory_handle: int) -> None:
        """Require an acknowledged directory flush for crash-durable callers."""
        import ctypes

        if self._flush_file_buffers(directory_handle):
            return
        last_error = getattr(ctypes, "get_last_error")()
        raise OSError(
            last_error,
            "Windows filesystem cannot confirm bound-directory durability",
        )

    def assert_directory_handle(self, directory_handle: int) -> None:
        _validate_windows_directory_handle(self, directory_handle)

    def _open_relative(
        self,
        parent_handle: int,
        name: str,
        *,
        desired_access: int,
        share_mode: int,
        disposition: int,
        options: int,
    ) -> int:
        import ctypes

        class UnicodeString(ctypes.Structure):
            _fields_ = (
                ("Length", ctypes.c_uint16),
                ("MaximumLength", ctypes.c_uint16),
                ("Buffer", ctypes.c_void_p),
            )

        class ObjectAttributes(ctypes.Structure):
            _fields_ = (
                ("Length", ctypes.c_uint32),
                ("RootDirectory", ctypes.c_void_p),
                ("ObjectName", ctypes.POINTER(UnicodeString)),
                ("Attributes", ctypes.c_uint32),
                ("SecurityDescriptor", ctypes.c_void_p),
                ("SecurityQualityOfService", ctypes.c_void_p),
            )

        class IoStatusBlock(ctypes.Structure):
            _fields_ = (
                ("Status", ctypes.c_void_p),
                ("Information", ctypes.c_size_t),
            )

        encoded_name = name.encode("utf-16-le")
        name_buffer = ctypes.create_string_buffer(encoded_name + b"\0\0")
        name_bytes = len(encoded_name)
        unicode_name = UnicodeString(
            name_bytes,
            name_bytes + 2,
            ctypes.addressof(name_buffer),
        )
        attributes = ObjectAttributes(
            ctypes.sizeof(ObjectAttributes),
            parent_handle,
            ctypes.pointer(unicode_name),
            0x00000040,
            None,
            None,
        )
        io_status = IoStatusBlock()
        handle = ctypes.c_void_p()
        status = int(
            self._nt_create_file(
                ctypes.byref(handle),
                desired_access,
                ctypes.byref(attributes),
                ctypes.byref(io_status),
                None,
                0,
                share_mode,
                disposition,
                options,
                None,
                0,
            )
        )
        if status < 0:
            self._raise_nt_status(status, name)
        if handle.value is None:
            raise OSError("NtCreateFile returned an empty cache handle")
        return int(handle.value)

    def _stat_handle(self, handle: int) -> os.stat_result:
        import ctypes

        class FileTime(ctypes.Structure):
            _fields_ = (("Low", ctypes.c_uint32), ("High", ctypes.c_uint32))

        class ByHandleFileInformation(ctypes.Structure):
            _fields_ = (
                ("FileAttributes", ctypes.c_uint32),
                ("CreationTime", FileTime),
                ("LastAccessTime", FileTime),
                ("LastWriteTime", FileTime),
                ("VolumeSerialNumber", ctypes.c_uint32),
                ("FileSizeHigh", ctypes.c_uint32),
                ("FileSizeLow", ctypes.c_uint32),
                ("NumberOfLinks", ctypes.c_uint32),
                ("FileIndexHigh", ctypes.c_uint32),
                ("FileIndexLow", ctypes.c_uint32),
            )

        info = ByHandleFileInformation()
        if not self._get_file_information(handle, ctypes.byref(info)):
            self._raise_last_error("could not stat bound cache entry")
        attributes = int(info.FileAttributes)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            file_type = stat.S_IFLNK
        elif attributes & _FILE_ATTRIBUTE_DIRECTORY:
            file_type = stat.S_IFDIR
        else:
            file_type = stat.S_IFREG
        size = (int(info.FileSizeHigh) << 32) | int(info.FileSizeLow)
        inode = (int(info.FileIndexHigh) << 32) | int(info.FileIndexLow)
        return os.stat_result(
            (
                file_type | 0o600,
                inode,
                int(info.VolumeSerialNumber),
                int(info.NumberOfLinks),
                0,
                0,
                size,
                self._filetime_seconds(info.LastAccessTime),
                self._filetime_seconds(info.LastWriteTime),
                self._filetime_seconds(info.CreationTime),
            )
        )

    @staticmethod
    def _filetime_seconds(value: object) -> float:
        low = int(getattr(value, "Low"))
        high = int(getattr(value, "High"))
        return (((high << 32) | low) - 116_444_736_000_000_000) / 10_000_000

    def _require_regular_file_handle(self, handle: int) -> None:
        attributes = self.file_attributes(handle)
        if attributes & (_FILE_ATTRIBUTE_DIRECTORY | _FILE_ATTRIBUTE_REPARSE_POINT):
            raise UnsafeCacheError("model cache target is not a regular file")

    def _handle_to_fd(self, handle: int, flags: int) -> int:
        import msvcrt

        return int(
            msvcrt.open_osfhandle(  # type: ignore[attr-defined]
                handle, flags | getattr(os, "O_BINARY", 0)
            )
        )

    def _raise_nt_status(self, status: int, name: str) -> None:
        code = int(self._rtl_nt_status_to_dos_error(status))
        if code in {2, 3}:
            raise FileNotFoundError(code, "cache entry does not exist", name)
        if code in {80, 183}:
            raise FileExistsError(code, "cache entry already exists", name)
        if code == 5:
            raise PermissionError(code, "cache entry access denied", name)
        raise OSError(code, "relative cache operation failed", name)

    @staticmethod
    def _require_hardened_open(
        desired_access: int, share_mode: int, flags: int
    ) -> None:
        if (
            desired_access
            not in {_WINDOWS_DIRECTORY_ACCESS, _WINDOWS_WRITABLE_DIRECTORY_ACCESS}
            or share_mode != _WINDOWS_DIRECTORY_SHARE
            or flags != _WINDOWS_DIRECTORY_FLAGS
        ):
            raise ValueError("Windows cache directory open is not hardened")

    @staticmethod
    def _require_publication_directory_open(
        desired_access: int, share_mode: int, flags: int
    ) -> None:
        if (
            desired_access != _WINDOWS_PUBLICATION_DIRECTORY_ACCESS
            or share_mode != _WINDOWS_PUBLICATION_SHARE
            or flags != _WINDOWS_DIRECTORY_FLAGS
        ):
            raise ValueError("Windows publication directory open is not hardened")

    @staticmethod
    def _raise_last_error(message: str) -> None:
        import ctypes

        last_error = getattr(ctypes, "get_last_error")()
        if last_error == 5:
            raise PermissionError(last_error, message)
        raise OSError(last_error, message)

    def file_attributes(self, handle: int) -> int:
        import ctypes

        class FileAttributeTagInfo(ctypes.Structure):
            _fields_ = (
                ("FileAttributes", ctypes.c_uint32),
                ("ReparseTag", ctypes.c_uint32),
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
