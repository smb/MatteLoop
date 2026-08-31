from __future__ import annotations

from typing import TYPE_CHECKING

# ruff: noqa: F403,F405
from ._common import *  # noqa: F403,F401

if TYPE_CHECKING:
    from ._errors import _unsafe_error
    from ._filesystem import _BoundDirectory
    from ._fs_helpers import (
        _directory_identity,
        _output_lock_anchor_payload,
        _parse_output_lock_anchor,
        _read_small_descriptor,
        _write_all,
    )
    from ._locking import (
        AdvisoryFileLock,
        LockedSlotFile,
        _OpenedDescriptorOwner,
        _SystemAdvisoryFileLock,
    )
    from ._manifest_validation import _validate_component
    from ._publication import PublicationDirectory, _rename_bound_publication
    from ._runtime_helpers import (
        _acquire_local_advisory_lock,
        _release_local_advisory_lock,
    )

__all__ = (
    "RecoveryDirectory",
)


class RecoveryDirectory:
    """A private child resolved from one already-bound publication parent.

    All names are handle-relative. ``path_for`` is diagnostic-only.
    """

    __slots__ = (
        "_directory",
        "_identity",
        "_owned_parent",
        "_parent",
        "_stack",
        "name",
        "path",
    )

    def __init__(
        self,
        path: Path,
        name: str,
        parent: PublicationDirectory,
        directory: _BoundDirectory,
        identity: tuple[int, int],
        stack: ExitStack,
        owned_parent: PublicationDirectory | None,
    ) -> None:
        self.path = path
        self.name = name
        self._parent = parent
        self._directory = directory
        self._identity = identity
        self._stack = stack
        self._owned_parent = owned_parent

    @classmethod
    def open(cls, parent_path: Path, name: str) -> RecoveryDirectory:
        parent = PublicationDirectory.open(parent_path)
        try:
            result = cls.open_from(parent, name, "recovery")
        except BaseException as error:
            try:
                parent.close(error)
            except BaseException as cleanup_error:
                error.add_note(
                    f"additional recovery-parent cleanup failure: {cleanup_error}"
                )
            raise
        result._owned_parent = parent
        return result

    @classmethod
    def open_from(
        cls,
        parent: PublicationDirectory,
        name: str,
        purpose: str,
    ) -> RecoveryDirectory:
        _validate_component(name)
        if not isinstance(purpose, str) or not purpose:
            raise TypeError("private output-directory purpose is required")
        stack = ExitStack()
        try:
            try:
                info = parent._directory.lstat(name)
            except FileNotFoundError:
                parent._directory.mkdir(name, exist_ok=False)
                parent.fsync()
                info = parent._directory.lstat(name)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _unsafe_error(f"output {purpose} namespace is redirected")
            if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
                raise _unsafe_error(
                    f"output {purpose} namespace must have mode 0700 or stricter"
                )
            directory = stack.enter_context(
                parent._directory.open_publication_child(name)
            )
            result = cls(
                parent.path / name,
                name,
                parent,
                directory,
                _directory_identity(parent.lstat(name)),
                stack,
                None,
            )
            result.assert_still_bound()
            return result
        except BaseException as error:
            try:
                stack.__exit__(type(error), error, error.__traceback__)
            except BaseException as cleanup_error:
                error.add_note(
                    f"additional private-directory cleanup failure: {cleanup_error}"
                )
            raise

    def close(self, primary: BaseException | None = None) -> None:
        close_primary = primary
        try:
            if close_primary is None:
                self._stack.close()
            else:
                self._stack.__exit__(
                    type(close_primary),
                    close_primary,
                    close_primary.__traceback__,
                )
        except BaseException as error:
            close_primary = error
        owned_parent = self._owned_parent
        self._owned_parent = None
        if owned_parent is not None:
            try:
                owned_parent.close(close_primary)
            except BaseException as error:
                if close_primary is not None:
                    close_primary.add_note(
                        f"additional recovery-parent cleanup failure: {error}"
                    )
                else:
                    close_primary = error
        if primary is None and close_primary is not None:
            raise close_primary

    def assert_still_bound(self) -> None:
        self._parent.assert_still_bound()
        self._directory.assert_still_named()
        current = self._parent.lstat(self.name)
        if (
            not stat.S_ISDIR(current.st_mode)
            or _directory_identity(current) != self._identity
        ):
            raise _unsafe_error("output private directory ownership changed")

    def assert_handle_owned(self) -> None:
        self._parent.assert_handle_bound()
        self._directory.assert_handle_safe()
        current = self._parent.lstat(self.name)
        if (
            not stat.S_ISDIR(current.st_mode)
            or _directory_identity(current) != self._identity
        ):
            raise _unsafe_error("output private directory ownership changed")

    @property
    def identity(self) -> tuple[int, int]:
        return self._identity

    def path_for(self, name: str) -> Path:
        _validate_component(name)
        return self.path / name

    def lstat(self, name: str) -> os.stat_result:
        return self._directory.publication_lstat(name)

    def open_read(self, name: str) -> int:
        return self._directory.open_publication_read(name)

    def open_read_write(self, name: str) -> int:
        return self._directory.open_publication_read_write(name)

    def open_locked_slot(
        self,
        name: str,
        owner: AdvisoryFileLock,
        *,
        create_if_missing: bool = True,
    ) -> LockedSlotFile:
        """Open and lock one exact fixed-slot inode before any mutation."""
        _validate_component(name)
        if not isinstance(owner, AdvisoryFileLock) or not owner.anchored:
            raise _unsafe_error("output private slot requires an anchored owner")
        owner.assert_owned()
        descriptor_owner: _OpenedDescriptorOwner | None = None
        held: LockedSlotFile | None = None
        local_lock: Lock | None = None
        local_key = ""
        try:
            if create_if_missing:
                try:
                    descriptor_owner = _OpenedDescriptorOwner(
                        self._directory.open_new_publication_fd(name)
                    )
                except FileExistsError:
                    descriptor_owner = None
            if descriptor_owner is None:
                before = self._directory.publication_lstat(name)
                if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                    raise _unsafe_error("output private slot is redirected")
                descriptor_owner = _OpenedDescriptorOwner(
                    self._directory.open_publication_read_write(name)
                )
                opened = os.fstat(descriptor_owner.descriptor)
                after = self._directory.publication_lstat(name)
                if not (
                    _directory_identity(before)
                    == _directory_identity(opened)
                    == _directory_identity(after)
                ):
                    raise _unsafe_error("output private slot changed while opened")
            identity = _directory_identity(os.fstat(descriptor_owner.descriptor))
            local_key = f"output-slot:{identity[0]}:{identity[1]}"
            local_lock = _acquire_local_advisory_lock(local_key)
            if local_lock is None:
                raise BlockingIOError(
                    errno.EWOULDBLOCK,
                    "output private slot is already active",
                )
            adapter = _SystemAdvisoryFileLock()
            held = LockedSlotFile(
                self,
                name,
                descriptor_owner.descriptor,
                identity,
                adapter,
                local_key,
                local_lock,
                locked=False,
            )
            descriptor_owner.transfer()
            descriptor_owner = None
            local_lock = None
            if not adapter.acquire_nonblocking(held.descriptor):
                raise BlockingIOError(
                    errno.EWOULDBLOCK,
                    "output private slot is already active",
                )
            held._locked = True
            held.assert_owned()
            owner.assert_owned()
            return held
        except BaseException as error:
            if held is not None:
                held.close(error)
            elif descriptor_owner is not None:
                descriptor_owner.close(error, detail="output-slot handle")
            if local_lock is not None:
                _release_local_advisory_lock(local_key, local_lock)
            raise

    def open_fixed_pending(
        self,
        name: str,
        owner: AdvisoryFileLock | None = None,
    ) -> int:
        """Create a new bounded pending inode; existing slots require a lock."""
        if owner is not None:
            owner.assert_owned()
        try:
            descriptor = self._directory.open_new_publication_fd(name)
        except FileExistsError:
            raise _unsafe_error(
                "existing output private pending entry has no transaction owner "
                "or inode lock"
            )
        if owner is not None:
            try:
                owner.assert_owned()
            except BaseException:
                os.close(descriptor)
                raise
        return descriptor

    def acquire_advisory_lock(self, name: str) -> AdvisoryFileLock:
        """Acquire a fixed, never-unlinked process lock in this bound directory."""
        return self._acquire_advisory_lock(name)

    def _acquire_advisory_lock(
        self,
        name: str,
        *,
        publication: PublicationDirectory | None = None,
        anchor_name: str | None = None,
    ) -> AdvisoryFileLock:
        _validate_component(name)
        if (publication is None) != (anchor_name is None):
            raise ValueError("publication and output-lock anchor must be paired")
        if publication is not None:
            if publication is not self._parent:
                raise _unsafe_error("output lock parent does not own private directory")
            if anchor_name is None:
                raise AssertionError("validated output-lock anchor is missing")
            _validate_component(anchor_name)
        descriptor_owner: _OpenedDescriptorOwner | None = None
        anchor_owner: _OpenedDescriptorOwner | None = None
        held: AdvisoryFileLock | None = None
        local_path = (
            publication.path_for(anchor_name)
            if publication is not None and anchor_name is not None
            else self.path_for(name)
        )
        local_key = os.path.normcase(os.path.abspath(local_path))
        if os.name == "nt" or sys.platform == "darwin":
            local_key = unicodedata.normalize("NFC", local_key).casefold()
        local_lock = _acquire_local_advisory_lock(local_key)
        if local_lock is None:
            raise BlockingIOError(
                errno.EWOULDBLOCK,
                "output transaction is already active",
            )
        created = False
        try:
            expected_directory_identity: tuple[int, int] | None = None
            expected_lock_identity: tuple[int, int] | None = None
            anchor_identity: tuple[int, int] | None = None
            if publication is not None and anchor_name is not None:
                try:
                    before_anchor = publication.lstat(anchor_name)
                    if stat.S_ISLNK(before_anchor.st_mode) or not stat.S_ISREG(
                        before_anchor.st_mode
                    ):
                        raise _unsafe_error("output transaction anchor is redirected")
                    anchor_owner = _OpenedDescriptorOwner(
                        publication.open_read(anchor_name)
                    )
                    opened_anchor = os.fstat(anchor_owner.descriptor)
                    payload = _read_small_descriptor(anchor_owner.descriptor)
                    after_anchor = publication.lstat(anchor_name)
                    if not (
                        _directory_identity(before_anchor)
                        == _directory_identity(opened_anchor)
                        == _directory_identity(after_anchor)
                    ):
                        raise _unsafe_error(
                            "output transaction anchor changed while opened"
                        )
                    anchor_identity = _directory_identity(opened_anchor)
                    (
                        expected_directory_identity,
                        expected_lock_identity,
                    ) = _parse_output_lock_anchor(payload)
                    if expected_directory_identity != self.identity:
                        raise _unsafe_error(
                            "output transaction private directory does not match anchor"
                        )
                except FileNotFoundError:
                    if anchor_owner is not None:
                        raced_anchor_owner = anchor_owner
                        anchor_owner = None
                        raced_anchor_owner.close(
                            detail="raced output-transaction anchor handle"
                        )

            try:
                if expected_lock_identity is not None:
                    before = self._directory.publication_lstat(name)
                    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                        raise _unsafe_error("output transaction lock is redirected")
                    descriptor_owner = _OpenedDescriptorOwner(
                        self._directory.open_publication_read_write(name)
                    )
                    opened = os.fstat(descriptor_owner.descriptor)
                    after_open = self._directory.publication_lstat(name)
                    if not (
                        _directory_identity(before)
                        == _directory_identity(opened)
                        == _directory_identity(after_open)
                        == expected_lock_identity
                    ):
                        raise _unsafe_error(
                            "output transaction lock does not match anchor"
                        )
                else:
                    descriptor_owner = _OpenedDescriptorOwner(
                        self._directory.open_new_publication_fd(name)
                    )
                    created = True
            except FileExistsError:
                before = self._directory.publication_lstat(name)
                if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                    raise _unsafe_error("output transaction lock is redirected")
                descriptor_owner = _OpenedDescriptorOwner(
                    self._directory.open_publication_read_write(name)
                )
                opened = os.fstat(descriptor_owner.descriptor)
                after_open = self._directory.publication_lstat(name)
                if not (
                    _directory_identity(before)
                    == _directory_identity(opened)
                    == _directory_identity(after_open)
                ):
                    raise _unsafe_error("output transaction lock changed while opened")

            adapter = _SystemAdvisoryFileLock()
            if descriptor_owner is None:
                raise RuntimeError("output transaction descriptor was not opened")
            lock_identity = _directory_identity(os.fstat(descriptor_owner.descriptor))
            held = AdvisoryFileLock(
                name,
                descriptor_owner.descriptor,
                adapter,
                local_key,
                local_lock,
                locked=False,
                directory=self,
                lock_identity=lock_identity,
                publication=publication,
                anchor_name=anchor_name,
                anchor_descriptor=(
                    anchor_owner.descriptor if anchor_owner is not None else None
                ),
                anchor_identity=anchor_identity,
                directory_identity=self.identity if publication is not None else None,
            )
            descriptor_owner.transfer()
            if anchor_owner is not None:
                anchor_owner.transfer()
            descriptor_owner = None
            anchor_owner = None
            locked_descriptor = held._descriptor
            if locked_descriptor is None:
                raise RuntimeError(
                    "acquired output-transaction lock lost its descriptor"
                )
            if not adapter.acquire_nonblocking(locked_descriptor):
                raise BlockingIOError(
                    errno.EWOULDBLOCK,
                    "output transaction is already active",
                )
            held._locked = True
            locked_info = os.fstat(locked_descriptor)
            if locked_info.st_size == 0:
                os.ftruncate(locked_descriptor, 1)
                os.fsync(locked_descriptor)
            current = self._directory.publication_lstat(name)
            if (
                stat.S_ISLNK(current.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or _directory_identity(current)
                != _directory_identity(os.fstat(locked_descriptor))
            ):
                raise _unsafe_error("output transaction lock changed while acquired")
            if created:
                self._directory.fsync()
            if publication is not None and anchor_name is not None:
                if held._anchor_descriptor is None:
                    payload = _output_lock_anchor_payload(
                        self.identity,
                        _directory_identity(os.fstat(locked_descriptor)),
                    )
                    new_anchor_owner = _OpenedDescriptorOwner(
                        publication.open_new(anchor_name)
                    )
                    try:
                        _write_all(new_anchor_owner.descriptor, payload)
                        os.fsync(new_anchor_owner.descriptor)
                        new_anchor_info = os.fstat(new_anchor_owner.descriptor)
                        current_anchor = publication.lstat(anchor_name)
                        if not stat.S_ISREG(
                            current_anchor.st_mode
                        ) or _directory_identity(current_anchor) != _directory_identity(
                            new_anchor_info
                        ):
                            raise _unsafe_error(
                                "output transaction anchor changed while created"
                            )
                        publication.fsync()
                    except BaseException as error:
                        new_anchor_owner.close(
                            error,
                            detail="output-transaction anchor handle",
                        )
                        raise
                    held._anchor_descriptor = new_anchor_owner.transfer()
                    held._anchor_identity = _directory_identity(new_anchor_info)
                held.assert_owned()
            else:
                self.assert_still_bound()
            return held
        except BaseException as error:
            if held is not None:
                held.close(error)
            elif descriptor_owner is not None:
                descriptor_owner.close(
                    error,
                    detail="output-transaction lock handle",
                )
            if anchor_owner is not None:
                anchor_owner.close(
                    error,
                    detail="output-transaction anchor handle",
                )
            if held is None:
                _release_local_advisory_lock(local_key, local_lock)
            raise

    def link_parent_file(
        self,
        source: str,
        destination: str,
        owner: AdvisoryFileLock | None = None,
    ) -> bool:
        _validate_component(source)
        _validate_component(destination)
        parent_descriptor = self._parent._directory.descriptor
        if parent_descriptor is None or self._directory.descriptor is None:
            return False
        if owner is not None:
            owner.assert_owned()
        os.link(
            source,
            destination,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=self._directory.descriptor,
            follow_symlinks=False,
        )
        if owner is not None:
            owner.assert_owned()
        return True

    def link_file(
        self,
        source: str,
        destination: str,
        owner: AdvisoryFileLock | None = None,
    ) -> bool:
        _validate_component(source)
        _validate_component(destination)
        if self._directory.descriptor is None:
            return False
        if owner is not None:
            owner.assert_owned()
        os.link(
            source,
            destination,
            src_dir_fd=self._directory.descriptor,
            dst_dir_fd=self._directory.descriptor,
            follow_symlinks=False,
        )
        if owner is not None:
            owner.assert_owned()
        return True

    def replace(self, source: str, destination: str) -> None:
        self._directory.replace_publication(source, destination)

    def replace_owned(
        self,
        source: str,
        destination: str,
        source_slot: LockedSlotFile,
        owner: AdvisoryFileLock,
        *,
        source_alias: bool = False,
    ) -> None:
        """Rename a locked source only after locking an existing destination."""
        _validate_component(source)
        _validate_component(destination)
        if source_slot._directory is not self or (
            not source_alias and source_slot.name != source
        ):
            raise _unsafe_error("output private source slot does not match rename")
        owner.assert_owned()
        self.assert_handle_owned()
        source_slot.assert_owned()
        source_info = self.lstat(source)
        if _directory_identity(source_info) != source_slot.identity:
            raise _unsafe_error("output private source alias is not locked")
        destination_slot: LockedSlotFile | None = None
        primary: BaseException | None = None
        try:
            try:
                destination_info = self.lstat(destination)
            except FileNotFoundError:
                destination_info = None
            if (
                destination_info is not None
                and _directory_identity(destination_info) == source_slot.identity
            ):
                current_source = self.lstat(source)
                if _directory_identity(current_source) != source_slot.identity:
                    raise _unsafe_error("locked output private alias changed")
                # Both fixed names already identify the held inode.  There is
                # no portable unlink-if-identity operation: a pathname check
                # followed by unlink could delete a replacement entry.  Keep
                # the bounded alias and its inode lock instead of mutating it.
                source_slot.assert_owned()
                owner.assert_owned()
                return
            try:
                destination_slot = self.open_locked_slot(
                    destination,
                    owner,
                    create_if_missing=False,
                )
            except FileNotFoundError:
                destination_slot = None
            source_slot.assert_owned()
            owner.assert_owned()
            if destination_slot is None:
                _rename_bound_publication(
                    self._directory,
                    source,
                    self._directory,
                    destination,
                    replace=False,
                )
            else:
                destination_slot.assert_owned()
                self._directory.replace_publication(source, destination)
            if source_alias:
                source_slot.assert_owned()
                installed = self.lstat(destination)
                if _directory_identity(installed) != source_slot.identity:
                    raise _unsafe_error("locked output private alias was not installed")
            else:
                source_slot.rename_to(destination)
            owner.assert_owned()
        except BaseException as error:
            primary = error
            raise
        finally:
            if destination_slot is not None:
                destination_slot.close(primary)

    def fsync(self) -> None:
        self._directory.fsync()
