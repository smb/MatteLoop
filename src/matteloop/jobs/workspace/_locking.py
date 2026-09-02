from __future__ import annotations

from typing import TYPE_CHECKING

# ruff: noqa: F403,F405
from ._common import *  # noqa: F403,F401

if TYPE_CHECKING:
    from ._errors import _unsafe_error
    from ._fs_helpers import (
        _directory_identity,
        _parse_output_lock_anchor,
        _read_small_descriptor,
    )
    from ._manifest_validation import _validate_component
    from ._publication import PublicationDirectory
    from ._recovery import RecoveryDirectory
    from ._runtime_helpers import _release_local_advisory_lock

__all__ = (
    "AdvisoryFileLock",
    "LockedSlotFile",
    "_OpenedDescriptorOwner",
    "_SystemAdvisoryFileLock",
)

# Far beyond any payload byte, so the mandatory Windows lock never covers
# content another handle has to read.
_WINDOWS_LOCK_OFFSET = 1 << 62


class _SystemAdvisoryFileLock:
    """Non-blocking process lock adapter over one held regular-file fd."""

    __slots__ = ("_platform", "_posix", "_windows")

    def __init__(
        self,
        *,
        platform: str | None = None,
        posix_module: Any | None = None,
        windows_module: Any | None = None,
    ) -> None:
        selected = (
            ("windows" if os.name == "nt" else "posix")
            if platform is None
            else platform
        )
        if selected not in {"posix", "windows"}:
            raise ValueError("advisory-lock platform must be posix or windows")
        if selected == "posix" and windows_module is not None:
            raise ValueError("POSIX advisory locks do not accept a Windows module")
        if selected == "windows" and posix_module is not None:
            raise ValueError("Windows advisory locks do not accept a POSIX module")
        self._platform = selected
        self._posix = (
            importlib.import_module("fcntl")
            if selected == "posix" and posix_module is None
            else posix_module
        )
        self._windows = (
            importlib.import_module("msvcrt")
            if selected == "windows" and windows_module is None
            else windows_module
        )

    def acquire_nonblocking(self, descriptor: int) -> bool:
        if type(descriptor) is not int or descriptor < 0:
            raise ValueError("advisory-lock descriptor must be a non-negative int")
        if self._platform == "posix":
            posix = self._posix
            if posix is None:
                raise RuntimeError("POSIX advisory-lock adapter is unavailable")
            try:
                posix.flock(descriptor, posix.LOCK_EX | posix.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    return False
                raise
            return True
        windows = self._windows
        if windows is None:
            raise RuntimeError("Windows advisory-lock adapter is unavailable")
        # Windows byte-range locks are mandatory, not advisory: locking byte
        # 0 would make every later reader of the file itself fail with
        # ERROR_LOCK_VIOLATION. Lock a byte no payload can reach instead, and
        # restore the position because callers write from where they left off.
        position = os.lseek(descriptor, 0, os.SEEK_CUR)
        os.lseek(descriptor, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        try:
            windows.locking(descriptor, windows.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(
                error, "winerror", None
            ) in {33, 36}:
                return False
            raise
        finally:
            os.lseek(descriptor, position, os.SEEK_SET)
        return True


class _OpenedDescriptorOwner:
    """Own an fd immediately and consume its integer on every close attempt."""

    __slots__ = ("_close_guard", "_descriptor")

    def __init__(self, descriptor: int) -> None:
        if type(descriptor) is not int or descriptor < 0:
            raise ValueError("owned descriptor must be a non-negative int")
        self._descriptor: int | None = descriptor
        self._close_guard = Lock()

    @property
    def descriptor(self) -> int:
        descriptor = self._descriptor
        if descriptor is None:
            raise _unsafe_error("owned descriptor was already consumed")
        return descriptor

    def transfer(self) -> int:
        with self._close_guard:
            descriptor = self.descriptor
            self._descriptor = None
            return descriptor

    def close(
        self,
        primary: BaseException | None = None,
        *,
        detail: str,
    ) -> None:
        with self._close_guard:
            descriptor = self._descriptor
            if descriptor is None:
                return
            # A failed close leaves POSIX fd state unspecified.  Consume the
            # integer before calling close so it can never be retried after
            # the kernel may have made that integer reusable.
            self._descriptor = None
            try:
                os.close(descriptor)
            except BaseException as error:
                if primary is not None:
                    primary.add_note(f"additional {detail} cleanup failure: {error}")
                    return
                structured = _unsafe_error(f"cannot close {detail}: {error}")
                raise structured from error


class AdvisoryFileLock:
    """Owned advisory lock, optionally bound to an output-parent anchor."""

    __slots__ = (
        "_adapter",
        "_anchor_descriptor",
        "_anchor_identity",
        "_anchor_name",
        "_close_guard",
        "_descriptor",
        "_directory",
        "_directory_identity",
        "_local_key",
        "_local_lock",
        "_locked",
        "_publication",
        "_lock_identity",
        "name",
    )

    def __init__(
        self,
        name: str,
        descriptor: int,
        adapter: _SystemAdvisoryFileLock,
        local_key: str,
        local_lock: Lock,
        *,
        locked: bool = True,
        directory: RecoveryDirectory | None = None,
        lock_identity: tuple[int, int] | None = None,
        publication: PublicationDirectory | None = None,
        anchor_name: str | None = None,
        anchor_descriptor: int | None = None,
        anchor_identity: tuple[int, int] | None = None,
        directory_identity: tuple[int, int] | None = None,
    ) -> None:
        self.name = name
        self._descriptor: int | None = descriptor
        self._adapter = adapter
        self._close_guard = Lock()
        self._locked = locked
        self._local_key = local_key
        self._local_lock: Lock | None = local_lock
        self._directory = directory
        self._lock_identity = lock_identity
        self._publication = publication
        self._anchor_name = anchor_name
        self._anchor_descriptor = anchor_descriptor
        self._anchor_identity = anchor_identity
        self._directory_identity = directory_identity

    @property
    def anchored(self) -> bool:
        return self._publication is not None

    def assert_owned(self) -> None:
        """Fail closed if the parent, private directory, anchor, or lock changed."""
        descriptor = self._descriptor
        directory = self._directory
        lock_identity = self._lock_identity
        if descriptor is None or directory is None or lock_identity is None:
            raise _unsafe_error("output transaction lock is no longer owned")
        directory.assert_handle_owned()
        current_lock = directory.lstat(self.name)
        if (
            stat.S_ISLNK(current_lock.st_mode)
            or not stat.S_ISREG(current_lock.st_mode)
            or _directory_identity(current_lock) != lock_identity
            or _directory_identity(os.fstat(descriptor)) != lock_identity
        ):
            raise _unsafe_error("output transaction lock ownership changed")

        publication = self._publication
        if publication is None:
            return
        anchor_name = self._anchor_name
        anchor_descriptor = self._anchor_descriptor
        anchor_identity = self._anchor_identity
        directory_identity = self._directory_identity
        if (
            anchor_name is None
            or anchor_descriptor is None
            or anchor_identity is None
            or directory_identity is None
        ):
            raise _unsafe_error("output transaction anchor is incomplete")
        publication.assert_handle_bound()
        if directory.identity != directory_identity:
            raise _unsafe_error("output private directory ownership changed")
        before = publication.lstat(anchor_name)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or _directory_identity(before) != anchor_identity
            or _directory_identity(os.fstat(anchor_descriptor)) != anchor_identity
        ):
            raise _unsafe_error("output transaction anchor ownership changed")
        payload = _read_small_descriptor(anchor_descriptor)
        after = publication.lstat(anchor_name)
        if _directory_identity(after) != anchor_identity:
            raise _unsafe_error("output transaction anchor changed while checked")
        if _parse_output_lock_anchor(payload) != (directory_identity, lock_identity):
            raise _unsafe_error("output transaction anchor content changed")
        after_lock = directory.lstat(self.name)
        if _directory_identity(after_lock) != lock_identity:
            raise _unsafe_error("output transaction lock changed while checked")

    def close(self, primary: BaseException | None = None) -> None:
        with self._close_guard:
            descriptor = self._descriptor
            if descriptor is None and self._anchor_descriptor is None:
                return
            failures: list[BaseException] = []
            if self._locked and descriptor is None:
                failures.append(
                    _unsafe_error("output transaction lost its locked descriptor")
                )
                self._locked = False
            for attribute in ("_descriptor", "_anchor_descriptor"):
                owned_descriptor = getattr(self, attribute)
                if owned_descriptor is None:
                    continue
                # POSIX does not define whether an fd remains open after every
                # close error.  Consume the integer before the attempt so no
                # retry can close an unrelated descriptor that reused it.
                setattr(self, attribute, None)
                try:
                    os.close(owned_descriptor)
                except BaseException as error:
                    failures.append(error)
                finally:
                    if attribute == "_descriptor":
                        self._locked = False
            local_lock = self._local_lock
            if local_lock is not None:
                _release_local_advisory_lock(self._local_key, local_lock)
                self._local_lock = None
            if not failures:
                return
            detail = "; ".join(str(error) for error in failures)
            if primary is not None:
                primary.add_note(
                    f"additional output-transaction lock cleanup failure: {detail}"
                )
                return
            failure = _unsafe_error(f"cannot close output-transaction lock: {detail}")
            raise failure from failures[0]


class LockedSlotFile:
    """One exact fixed-slot inode locked until its owning artifact closes."""

    __slots__ = (
        "_adapter",
        "_close_guard",
        "_descriptor",
        "_directory",
        "_identity",
        "_local_key",
        "_local_lock",
        "_locked",
        "name",
    )

    def __init__(
        self,
        directory: RecoveryDirectory,
        name: str,
        descriptor: int,
        identity: tuple[int, int],
        adapter: _SystemAdvisoryFileLock,
        local_key: str,
        local_lock: Lock,
        *,
        locked: bool = True,
    ) -> None:
        self._directory = directory
        self.name = name
        self._descriptor: int | None = descriptor
        self._identity = identity
        self._adapter = adapter
        self._close_guard = Lock()
        self._local_key = local_key
        self._local_lock: Lock | None = local_lock
        self._locked = locked

    @property
    def descriptor(self) -> int:
        descriptor = self._descriptor
        if descriptor is None:
            raise _unsafe_error("output private slot is closed")
        return descriptor

    @property
    def identity(self) -> tuple[int, int]:
        return self._identity

    def assert_owned(self) -> None:
        if not self._locked or self._local_lock is None:
            raise _unsafe_error("output private slot lock is no longer owned")
        descriptor = self.descriptor
        self._directory.assert_handle_owned()
        opened = os.fstat(descriptor)
        current = self._directory.lstat(self.name)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or _directory_identity(opened) != self._identity
            or _directory_identity(current) != self._identity
        ):
            raise _unsafe_error("output private slot ownership changed")

    def reset_for_write(self, source_descriptor: int) -> None:
        """Truncate only this locked, singly-linked inode, never its source."""
        self.assert_owned()
        descriptor = self.descriptor
        opened = os.fstat(descriptor)
        source = os.fstat(source_descriptor)
        if _directory_identity(source) == self._identity:
            raise _unsafe_error("output private slot aliases its write source")
        if opened.st_nlink != 1:
            raise _unsafe_error("hard-linked output private slot cannot be recycled")
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        self.assert_owned()

    def rename_to(self, name: str) -> None:
        _validate_component(name)
        current = self._directory.lstat(name)
        if _directory_identity(current) != self._identity:
            raise _unsafe_error("renamed output private slot identity changed")
        self.name = name
        self.assert_owned()

    def close(self, primary: BaseException | None = None) -> None:
        with self._close_guard:
            descriptor = self._descriptor
            if descriptor is None:
                return
            self._descriptor = None
            failure: BaseException | None = None
            try:
                os.close(descriptor)
            except BaseException as error:
                failure = error
            finally:
                self._locked = False
            local_lock = self._local_lock
            if local_lock is not None:
                _release_local_advisory_lock(self._local_key, local_lock)
                self._local_lock = None
            if failure is None:
                return
            if primary is not None:
                primary.add_note(f"additional output-slot cleanup failure: {failure}")
                return
            structured = _unsafe_error(f"cannot close output private slot: {failure}")
            raise structured from failure
