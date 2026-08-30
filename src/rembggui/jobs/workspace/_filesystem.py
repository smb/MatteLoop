from __future__ import annotations

from typing import TYPE_CHECKING

# ruff: noqa: F403,F405
from ._common import *  # noqa: F403,F401

if TYPE_CHECKING:
    from ._errors import _unsafe_error
    from ._fs_helpers import _directory_identity, _fdopen_owned, _fsync_fd
    from ._manifest_validation import (
        _bounded_int,
        _validate_component,
        _validate_path_value,
    )
    from ._runtime_helpers import (
        _forget_deferred_bound_directory_close,
        _retain_deferred_bound_directory_close,
    )

__all__ = (
    "_BoundDirectory",
)


class _BoundDirectory:
    __slots__ = (
        "_windows_api",
        "_windows_cleanup_handles",
        "_windows_handles",
        "descriptor",
        "path",
    )

    def __init__(
        self,
        path: Path,
        descriptor: int | None,
        *,
        windows_handles: tuple[int, ...] = (),
        windows_api: Any = None,
    ) -> None:
        self.path = path
        self.descriptor = descriptor
        self._windows_handles = windows_handles
        self._windows_cleanup_handles: tuple[int, ...] = ()
        self._windows_api = windows_api

    @classmethod
    def open(cls, path: Path) -> Self:
        _validate_path_value(path)
        if ".." in path.parts:
            raise _unsafe_error("workspace directory traversal is not allowed")
        absolute = Path(os.path.abspath(path))
        if not absolute.is_absolute() or not absolute.parts:
            raise _unsafe_error("workspace directory must be absolute")
        if os.name == "nt":
            return cls._open_windows(absolute)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(absolute.anchor, flags)
            for component in absolute.parts[1:]:
                _validate_component(component)
                child = os.open(component, flags, dir_fd=descriptor)
                try:
                    opened = os.fstat(child)
                    if not stat.S_ISDIR(opened.st_mode):
                        raise _unsafe_error(
                            f"workspace component {component!r} is not a directory"
                        )
                except BaseException:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            return cls(absolute, descriptor)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                failure = _unsafe_error("workspace namespace contains redirection")
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                raise failure from error
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise
        except BaseException:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise

    @classmethod
    def _open_windows(cls, path: Path) -> Self:
        from rembggui.jobs.models.cache_fs import (
            _FILE_ATTRIBUTE_DIRECTORY,
            _FILE_ATTRIBUTE_REPARSE_POINT,
            _WINDOWS_DIRECTORY_ACCESS,
            _WINDOWS_DIRECTORY_FLAGS,
            _WINDOWS_DIRECTORY_SHARE,
            _WINDOWS_WRITABLE_DIRECTORY_ACCESS,
            _CtypesWindowsDirectoryApi,
        )

        api = _CtypesWindowsDirectoryApi()
        handles: list[int] = []
        try:
            anchor = Path(path.anchor)
            handle = api.open_anchor(
                anchor,
                desired_access=_WINDOWS_DIRECTORY_ACCESS,
                share_mode=_WINDOWS_DIRECTORY_SHARE,
                flags=_WINDOWS_DIRECTORY_FLAGS,
            )
            handles.append(handle)
            components = path.parts[1:]
            for index, component in enumerate(components):
                _validate_component(component)
                handle = api.open_child_directory(
                    handles[-1],
                    component,
                    create=False,
                    desired_access=(
                        _WINDOWS_WRITABLE_DIRECTORY_ACCESS
                        if index == len(components) - 1
                        else _WINDOWS_DIRECTORY_ACCESS
                    ),
                    share_mode=_WINDOWS_DIRECTORY_SHARE,
                    flags=_WINDOWS_DIRECTORY_FLAGS,
                )
                handles.append(handle)
                attributes = api.file_attributes(handle)
                if not attributes & _FILE_ATTRIBUTE_DIRECTORY or attributes & (
                    _FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise _unsafe_error(
                        f"workspace component {component!r} is redirected"
                    )
            return cls(
                path,
                None,
                windows_handles=tuple(handles),
                windows_api=api,
            )
        except BaseException:
            for handle in reversed(handles):
                try:
                    api.close_handle(handle)
                except OSError:
                    pass
            raise

    @classmethod
    def _open_windows_publication(cls, path: Path) -> Self:
        """Bind only the final output parent with publication sharing/access."""
        from rembggui.jobs.models.cache_fs import (
            _FILE_ATTRIBUTE_DIRECTORY,
            _FILE_ATTRIBUTE_REPARSE_POINT,
            _WINDOWS_DIRECTORY_ACCESS,
            _WINDOWS_DIRECTORY_FLAGS,
            _WINDOWS_DIRECTORY_SHARE,
            _WINDOWS_PUBLICATION_DIRECTORY_ACCESS,
            _WINDOWS_PUBLICATION_SHARE,
            _CtypesWindowsDirectoryApi,
        )

        api = _CtypesWindowsDirectoryApi()
        handles: list[int] = []
        try:
            anchor = Path(path.anchor)
            components = path.parts[1:]
            if components:
                handle = api.open_anchor(
                    anchor,
                    desired_access=_WINDOWS_DIRECTORY_ACCESS,
                    share_mode=_WINDOWS_DIRECTORY_SHARE,
                    flags=_WINDOWS_DIRECTORY_FLAGS,
                )
            else:
                handle = api.open_publication_anchor(
                    anchor,
                    desired_access=_WINDOWS_PUBLICATION_DIRECTORY_ACCESS,
                    share_mode=_WINDOWS_PUBLICATION_SHARE,
                    flags=_WINDOWS_DIRECTORY_FLAGS,
                )
            handles.append(handle)
            for index, component in enumerate(components):
                _validate_component(component)
                final = index == len(components) - 1
                if final:
                    handle = api.open_publication_child_directory(
                        handles[-1],
                        component,
                        create=False,
                        desired_access=_WINDOWS_PUBLICATION_DIRECTORY_ACCESS,
                        share_mode=_WINDOWS_PUBLICATION_SHARE,
                        flags=_WINDOWS_DIRECTORY_FLAGS,
                    )
                else:
                    handle = api.open_child_directory(
                        handles[-1],
                        component,
                        create=False,
                        desired_access=_WINDOWS_DIRECTORY_ACCESS,
                        share_mode=_WINDOWS_DIRECTORY_SHARE,
                        flags=_WINDOWS_DIRECTORY_FLAGS,
                    )
                handles.append(handle)
                attributes = api.file_attributes(handle)
                if not attributes & _FILE_ATTRIBUTE_DIRECTORY or attributes & (
                    _FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise _unsafe_error(f"output component {component!r} is redirected")
            return cls(path, None, windows_handles=tuple(handles), windows_api=api)
        except BaseException as error:
            failed_handles: list[int] = []
            for handle in reversed(handles):
                try:
                    api.close_handle(handle)
                except OSError as close_error:
                    failed_handles.append(handle)
                    error.add_note(
                        f"additional publication-binding cleanup failure: {close_error}"
                    )
            if failed_handles:
                owner = cls(
                    path,
                    None,
                    windows_handles=tuple(reversed(failed_handles)),
                    windows_api=api,
                )
                _retain_deferred_bound_directory_close(owner, error)
            raise

    def __enter__(self) -> Self:
        return self

    def owns_resources(self) -> bool:
        return bool(
            self.descriptor is not None
            or self._windows_handles
            or self._windows_cleanup_handles
        )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.close()
        except BaseException as error:
            if self._windows_handles or self._windows_cleanup_handles:
                _retain_deferred_bound_directory_close(
                    self, exc_value if exc_value is not None else error
                )
            if exc_value is not None:
                exc_value.add_note(f"additional bound-directory close failure: {error}")
                return
            raise

    def close(self) -> None:
        failures: list[OSError] = []
        descriptor = self.descriptor
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as error:
                failures.append(error)
            else:
                self.descriptor = None
        cleanup_handles = self._windows_cleanup_handles
        failed_cleanup_handles: set[int] = set()
        for handle in reversed(cleanup_handles):
            try:
                self._windows_api.close_handle(handle)
            except OSError as error:
                failures.append(error)
                failed_cleanup_handles.add(handle)
        self._windows_cleanup_handles = tuple(
            handle for handle in cleanup_handles if handle in failed_cleanup_handles
        )
        handles = self._windows_handles
        failed_handles: set[int] = set()
        for handle in reversed(handles):
            try:
                self._windows_api.close_handle(handle)
            except OSError as error:
                failures.append(error)
                failed_handles.add(handle)
        self._windows_handles = tuple(
            handle for handle in handles if handle in failed_handles
        )
        if failures:
            detail = "; ".join(str(error) for error in failures)
            raise _unsafe_error(f"cannot close bound workspace resources: {detail}")
        _forget_deferred_bound_directory_close(self)

    def assert_still_named(self) -> None:
        self.assert_handle_safe()
        if self._windows_handles:
            return
        if self.descriptor is not None:
            opened = os.fstat(self.descriptor)
            try:
                named = self.path.lstat()
            except OSError as error:
                raise _unsafe_error("bound workspace directory was renamed") from error
            if _directory_identity(opened) != _directory_identity(named):
                raise _unsafe_error("bound workspace directory was redirected")

    def assert_handle_safe(self) -> None:
        """Validate held handles without relying on their lexical namespace."""
        if self._windows_handles:
            for handle in self._windows_handles:
                self._windows_api.assert_directory_handle(handle)
            return
        if self.descriptor is not None and not stat.S_ISDIR(
            os.fstat(self.descriptor).st_mode
        ):
            raise _unsafe_error("bound workspace handle is not a directory")

    def lstat(self, name: str) -> os.stat_result:
        _validate_component(name)
        if self.descriptor is not None:
            return os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        if self._windows_handles:
            return cast(
                os.stat_result,
                self._windows_api.lstat_at(self._windows_handles[-1], name),
            )
        return (self.path / name).lstat()

    def publication_lstat(self, name: str) -> os.stat_result:
        _validate_component(name)
        if self.descriptor is not None:
            return os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        if self._windows_handles:
            return cast(
                os.stat_result,
                self._windows_api.publication_lstat_at(self._windows_handles[-1], name),
            )
        return (self.path / name).lstat()

    def mkdir(self, name: str, *, exist_ok: bool) -> None:
        _validate_component(name)
        try:
            if self.descriptor is not None:
                os.mkdir(name, mode=0o700, dir_fd=self.descriptor)
            elif self._windows_handles:
                self._windows_api.mkdir_at(
                    self._windows_handles[-1], name, exist_ok=exist_ok
                )
            else:
                os.mkdir(self.path / name, mode=0o700)
        except FileExistsError:
            if not exist_ok:
                raise
            info = self.lstat(name)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _unsafe_error(f"workspace entry {name!r} is redirected")

    def open_child(self, name: str) -> _BoundDirectory:
        _validate_component(name)
        if self.descriptor is not None:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(name, flags, dir_fd=self.descriptor)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISDIR(info.st_mode):
                    raise _unsafe_error(f"workspace entry {name!r} is not a directory")
                return _BoundDirectory(self.path / name, descriptor)
            except BaseException:
                os.close(descriptor)
                raise
        if self._windows_handles:
            from rembggui.jobs.models.cache_fs import (
                _FILE_ATTRIBUTE_DIRECTORY,
                _FILE_ATTRIBUTE_REPARSE_POINT,
                _WINDOWS_DIRECTORY_FLAGS,
                _WINDOWS_DIRECTORY_SHARE,
                _WINDOWS_WRITABLE_DIRECTORY_ACCESS,
            )

            handle = self._windows_api.open_child_directory(
                self._windows_handles[-1],
                name,
                create=False,
                desired_access=_WINDOWS_WRITABLE_DIRECTORY_ACCESS,
                share_mode=_WINDOWS_DIRECTORY_SHARE,
                flags=_WINDOWS_DIRECTORY_FLAGS,
            )
            try:
                attributes = self._windows_api.file_attributes(handle)
                if not attributes & _FILE_ATTRIBUTE_DIRECTORY or attributes & (
                    _FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise _unsafe_error(f"workspace entry {name!r} is redirected")
                return _BoundDirectory(
                    self.path / name,
                    None,
                    windows_handles=(handle,),
                    windows_api=self._windows_api,
                )
            except BaseException as error:
                try:
                    self._windows_api.close_handle(handle)
                except BaseException as cleanup_error:
                    self._windows_cleanup_handles += (handle,)
                    error.add_note(
                        f"additional child-handle cleanup failure: {cleanup_error}"
                    )
                raise
        return _BoundDirectory.open(self.path / name)

    def open_publication_child(self, name: str) -> _BoundDirectory:
        _validate_component(name)
        if self.descriptor is not None:
            return self.open_child(name)
        if self._windows_handles:
            from rembggui.jobs.models.cache_fs import (
                _FILE_ATTRIBUTE_DIRECTORY,
                _FILE_ATTRIBUTE_REPARSE_POINT,
                _WINDOWS_DIRECTORY_FLAGS,
                _WINDOWS_PUBLICATION_DIRECTORY_ACCESS,
                _WINDOWS_PUBLICATION_SHARE,
            )

            handle = self._windows_api.open_publication_child_directory(
                self._windows_handles[-1],
                name,
                create=False,
                desired_access=_WINDOWS_PUBLICATION_DIRECTORY_ACCESS,
                share_mode=_WINDOWS_PUBLICATION_SHARE,
                flags=_WINDOWS_DIRECTORY_FLAGS,
            )
            try:
                attributes = self._windows_api.file_attributes(handle)
                if not attributes & _FILE_ATTRIBUTE_DIRECTORY or attributes & (
                    _FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise _unsafe_error(f"output entry {name!r} is redirected")
                return _BoundDirectory(
                    self.path / name,
                    None,
                    windows_handles=(handle,),
                    windows_api=self._windows_api,
                )
            except BaseException as error:
                try:
                    self._windows_api.close_handle(handle)
                except BaseException as cleanup_error:
                    self._windows_cleanup_handles += (handle,)
                    error.add_note(
                        f"additional child-handle cleanup failure: {cleanup_error}"
                    )
                raise
        return _BoundDirectory.open(self.path / name)

    def open_read(self, name: str) -> int:
        _validate_component(name)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            if self.descriptor is not None:
                return os.open(name, flags, dir_fd=self.descriptor)
            if self._windows_handles:
                return cast(
                    int,
                    self._windows_api.open_read_at(self._windows_handles[-1], name),
                )
            return os.open(self.path / name, flags)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise _unsafe_error(
                    f"workspace entry {name!r} is redirected"
                ) from error
            raise

    def open_publication_read(self, name: str) -> int:
        _validate_component(name)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            if self.descriptor is not None:
                return os.open(name, flags, dir_fd=self.descriptor)
            if self._windows_handles:
                return cast(
                    int,
                    self._windows_api.open_publication_read_at(
                        self._windows_handles[-1], name
                    ),
                )
            return os.open(self.path / name, flags)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise _unsafe_error(
                    f"workspace entry {name!r} is redirected"
                ) from error
            raise

    def open_publication_read_write(self, name: str) -> int:
        _validate_component(name)
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            if self.descriptor is not None:
                return os.open(name, flags, dir_fd=self.descriptor)
            if self._windows_handles:
                return cast(
                    int,
                    self._windows_api.open_publication_read_write_at(
                        self._windows_handles[-1], name
                    ),
                )
            return os.open(self.path / name, flags)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise _unsafe_error(f"output entry {name!r} is redirected") from error
            raise

    def open_read_write(self, name: str) -> int:
        _validate_component(name)
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if self.descriptor is not None:
            return os.open(name, flags, dir_fd=self.descriptor)
        if self._windows_handles:
            return cast(
                int,
                self._windows_api.open_read_at(self._windows_handles[-1], name),
            )
        return os.open(self.path / name, flags)

    def open_new_fd(self, name: str) -> int:
        _validate_component(name)
        # Every staged/snapshot output is hashed again through this same bound
        # descriptor before it is trusted, so the descriptor must be readable.
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if self.descriptor is not None:
            return os.open(name, flags, 0o600, dir_fd=self.descriptor)
        if self._windows_handles:
            return cast(
                int,
                self._windows_api.open_new_read_write_at(
                    self._windows_handles[-1], name
                ),
            )
        return os.open(self.path / name, flags, 0o600)

    def open_new_publication_fd(self, name: str) -> int:
        _validate_component(name)
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if self.descriptor is not None:
            return os.open(name, flags, 0o600, dir_fd=self.descriptor)
        if self._windows_handles:
            return cast(
                int,
                self._windows_api.open_new_publication_read_write_at(
                    self._windows_handles[-1], name
                ),
            )
        return os.open(self.path / name, flags, 0o600)

    def open_new(self, name: str) -> BinaryIO:
        descriptor = self.open_new_fd(name)
        return _fdopen_owned(descriptor, "wb")

    def replace(self, source: str, destination: str) -> None:
        _validate_component(source)
        _validate_component(destination)
        if self.descriptor is not None:
            os.replace(
                source,
                destination,
                src_dir_fd=self.descriptor,
                dst_dir_fd=self.descriptor,
            )
        elif self._windows_handles:
            self._windows_api.replace_at(self._windows_handles[-1], source, destination)
        else:
            os.replace(self.path / source, self.path / destination)

    def replace_publication(self, source: str, destination: str) -> None:
        _validate_component(source)
        _validate_component(destination)
        if self.descriptor is not None:
            os.replace(
                source,
                destination,
                src_dir_fd=self.descriptor,
                dst_dir_fd=self.descriptor,
            )
        elif self._windows_handles:
            self._windows_api.replace_publication_at(
                self._windows_handles[-1], source, destination
            )
        else:
            os.replace(self.path / source, self.path / destination)

    def replace_directory(self, source: str, destination: str) -> None:
        _validate_component(source)
        _validate_component(destination)
        if self.descriptor is not None:
            os.replace(
                source,
                destination,
                src_dir_fd=self.descriptor,
                dst_dir_fd=self.descriptor,
            )
        elif self._windows_handles:
            self._windows_api.replace_directory_at(
                self._windows_handles[-1], source, destination
            )
        else:
            os.replace(self.path / source, self.path / destination)

    def unlink(self, name: str) -> None:
        _validate_component(name)
        if self.descriptor is not None:
            os.unlink(name, dir_fd=self.descriptor)
        elif self._windows_handles:
            self._windows_api.unlink_at(
                self._windows_handles[-1], name, require_regular=False
            )
        else:
            (self.path / name).unlink()

    def rmdir(self, name: str) -> None:
        _validate_component(name)
        if self.descriptor is not None:
            os.rmdir(name, dir_fd=self.descriptor)
        elif self._windows_handles:
            self._windows_api.rmdir_at(self._windows_handles[-1], name)
        else:
            os.rmdir(self.path / name)

    def iter_entries(
        self, *, max_entries: int | None = None
    ) -> Iterator[tuple[str, os.stat_result]]:
        if max_entries is None:
            max_entries = MAX_FRAME_COUNT + 16
        _bounded_int(
            max_entries,
            "directory enumeration count",
            minimum=1,
            maximum=MAX_FRAME_COUNT + 16,
        )
        if self._windows_handles:
            yield from self._windows_api.iter_entries_at(
                self._windows_handles[-1], max_entries=max_entries
            )
            return
        target: int | Path = (
            self.descriptor if self.descriptor is not None else self.path
        )
        with os.scandir(target) as entries:
            for count, entry in enumerate(entries, start=1):
                if count > max_entries:
                    raise _unsafe_error("directory entry count exceeds its bound")
                yield entry.name, entry.stat(follow_symlinks=False)

    def fsync(self) -> None:
        if self.descriptor is not None:
            _fsync_fd(self.descriptor)
        elif self._windows_handles:
            self._windows_api.flush_directory_strict(self._windows_handles[-1])
