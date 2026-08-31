from __future__ import annotations

from typing import TYPE_CHECKING

# ruff: noqa: F403,F405
from ._common import *  # noqa: F403,F401

if TYPE_CHECKING:
    from ._errors import _unsafe_error
    from ._filesystem import _BoundDirectory
    from ._fs_helpers import _output_target_component_key
    from ._locking import AdvisoryFileLock
    from ._manifest_validation import _validate_component, _validate_path_value
    from ._recovery import RecoveryDirectory

__all__ = (
    "PublicationDirectory",
    "_rename_bound_publication",
    "_rename_no_replace_bound",
)


class PublicationDirectory:
    """One handle-bound output parent for an entire publication transaction."""

    __slots__ = ("_directory", "_stack", "path")

    def __init__(self, directory: _BoundDirectory, stack: ExitStack) -> None:
        self._directory = directory
        self._stack = stack
        self.path = directory.path

    @classmethod
    def open(cls, path: Path) -> PublicationDirectory:
        stack = ExitStack()
        try:
            absolute = Path(os.path.abspath(path))
            directory = stack.enter_context(
                _BoundDirectory._open_windows_publication(absolute)
                if os.name == "nt"
                else _BoundDirectory.open(absolute)
            )
            return cls(directory, stack)
        except BaseException as error:
            try:
                stack.__exit__(type(error), error, error.__traceback__)
            except BaseException as cleanup_error:
                error.add_note(
                    f"additional publication-parent cleanup failure: {cleanup_error}"
                )
            raise

    def close(self, primary: BaseException | None = None) -> None:
        if primary is None:
            self._stack.close()
            return
        self._stack.__exit__(type(primary), primary, primary.__traceback__)

    def assert_still_bound(self) -> None:
        self._directory.assert_still_named()

    def assert_handle_bound(self) -> None:
        self._directory.assert_handle_safe()

    def name_for(self, path: Path) -> str:
        _validate_path_value(path)
        parent = Path(os.path.abspath(path.parent))
        if parent != self.path:
            raise _unsafe_error("output entry is outside the bound publication parent")
        _validate_component(path.name)
        return path.name

    def target_key(self, path: Path) -> str:
        name = self.name_for(path)
        platform = (
            "windows"
            if os.name == "nt"
            else "darwin"
            if sys.platform == "darwin"
            else "posix"
        )
        return _output_target_component_key(name, platform=platform)

    def path_for(self, name: str) -> Path:
        _validate_component(name)
        return self.path / name

    def lstat(self, name: str) -> os.stat_result:
        return self._directory.publication_lstat(name)

    def open_read(self, name: str) -> int:
        return self._directory.open_publication_read(name)

    def open_read_write(self, name: str) -> int:
        return self._directory.open_publication_read_write(name)

    def open_new(self, name: str) -> int:
        return self._directory.open_new_publication_fd(name)

    def replace(self, source: str, destination: str) -> None:
        self._directory.replace_publication(source, destination)

    def replace_from(
        self,
        source_directory: RecoveryDirectory,
        source: str,
        destination: str,
    ) -> None:
        _rename_bound_publication(
            source_directory._directory,
            source,
            self._directory,
            destination,
            replace=True,
        )

    def rename_no_replace_from(
        self,
        source_directory: RecoveryDirectory,
        source: str,
        destination: str,
    ) -> None:
        _rename_bound_publication(
            source_directory._directory,
            source,
            self._directory,
            destination,
            replace=False,
        )

    def fsync(self) -> None:
        self._directory.fsync()

    def open_private_directory(self, name: str, purpose: str) -> RecoveryDirectory:
        return RecoveryDirectory.open_from(self, name, purpose)

    def acquire_output_lock(
        self,
        directory: RecoveryDirectory,
        target_key: str,
    ) -> AdvisoryFileLock:
        """Bind a target lock and its private directory to this parent handle."""
        if not isinstance(directory, RecoveryDirectory):
            raise TypeError("output lock requires a private publication directory")
        if (
            not isinstance(target_key, str)
            or len(target_key) != 64
            or any(character not in "0123456789abcdef" for character in target_key)
        ):
            raise ValueError("output target key must be lowercase SHA-256")
        return directory._acquire_advisory_lock(
            f"{target_key}.transaction.lock",
            publication=self,
            anchor_name=f".{target_key}.transaction-anchor",
        )

def _rename_bound_publication(
    source_directory: _BoundDirectory,
    source: str,
    destination_directory: _BoundDirectory,
    destination: str,
    *,
    replace: bool,
) -> None:
    _validate_component(source)
    _validate_component(destination)
    if (
        source_directory.descriptor is not None
        and destination_directory.descriptor is not None
    ):
        if replace:
            os.replace(
                source,
                destination,
                src_dir_fd=source_directory.descriptor,
                dst_dir_fd=destination_directory.descriptor,
            )
            return
        _rename_no_replace_bound(
            source_directory.descriptor,
            source,
            destination_directory.descriptor,
            destination,
        )
        return
    if source_directory._windows_handles and destination_directory._windows_handles:
        if source_directory._windows_api is not destination_directory._windows_api:
            raise _unsafe_error("publication directories have different handle owners")
        source_directory._windows_api.rename_publication_at(
            source_directory._windows_handles[-1],
            source,
            destination_directory._windows_handles[-1],
            destination,
            replace=replace,
        )
        return
    raise _unsafe_error("handle-relative output rename is unavailable")


def _rename_no_replace_bound(
    source_descriptor: int,
    source: str,
    destination_descriptor: int,
    destination: str,
) -> None:
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        renamex = getattr(libc, "renameatx_np", None)
        if renamex is None:
            raise OSError(errno.ENOTSUP, "renameatx_np is unavailable")
        renamex.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renamex.restype = ctypes.c_int
        result = renamex(
            source_descriptor,
            os.fsencode(source),
            destination_descriptor,
            os.fsencode(destination),
            0x00000004,
        )
    elif sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOTSUP, "renameat2 is unavailable")
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            source_descriptor,
            os.fsencode(source),
            destination_descriptor,
            os.fsencode(destination),
            0x00000001,
        )
    else:
        raise OSError(
            errno.ENOTSUP,
            "atomic handle-relative no-replace rename is unsupported",
        )
    if result == 0:
        return
    code = ctypes.get_errno()
    if code == errno.EEXIST:
        raise FileExistsError(code, os.strerror(code), destination)
    raise OSError(code, os.strerror(code), destination)
