from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

import matteloop.jobs.models.cache_fs as cache_fs
from matteloop.jobs.models.cache_fs import (
    BoundModelDirectory,
    UnsafeCacheError,
    _bind_windows,
)

_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
# FILE_SHARE_READ | FILE_SHARE_WRITE: see cache_fs._WINDOWS_DIRECTORY_SHARE.
_FILE_SHARE_READ = 0x00000001 | 0x00000002
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_FILE_WRITE_DATA = 0x00000002
_FILE_WRITE_ATTRIBUTES = 0x00000100
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_HARDENED_DIRECTORY_FLAGS = 0x02000000 | 0x00200000


class FakeWindowsDirectoryApi:
    def __init__(self) -> None:
        self.opened: list[Path] = []
        self.open_requests: list[tuple[Path, int, int, int]] = []
        self.created: list[Path] = []
        self.queried: list[int] = []
        self.closed: list[int] = []
        self.close_failures: set[int] = set()
        self.open_failures: dict[Path, BaseException] = {}
        self.attributes: dict[int, int] = {}
        self.attribute_overrides: dict[Path, int] = {}
        self.before_open: dict[Path, object] = {}
        self.active_when_created: dict[Path, tuple[Path, ...]] = {}
        self.foreign_write_paths: set[Path] = set()
        self.reparse_mutations: list[Path] = []
        self.named_redirects: dict[Path, Path] = {}
        self.handle_paths: dict[int, Path] = {}
        self.relative_directory_opens: list[tuple[int, str, bool]] = []
        self.relative_file_operations: list[tuple[str, int, str]] = []
        self.flushed_handles: list[int] = []

    def open_directory(
        self,
        path: Path,
        *,
        desired_access: int,
        share_mode: int,
        flags: int,
    ) -> int:
        callback = self.before_open.get(path)
        if callable(callback):
            callback()
        self.open_requests.append((path, desired_access, share_mode, flags))
        if path in self.foreign_write_paths and not share_mode & _FILE_SHARE_WRITE:
            raise OSError(32, "synthetic sharing violation")
        failure = self.open_failures.get(path)
        if failure is not None:
            raise failure
        if not path.exists():
            raise FileNotFoundError(path)
        handle = 100 + len(self.opened)
        self.opened.append(path)
        self.handle_paths[handle] = path
        self.attributes.setdefault(
            handle,
            self.attribute_overrides.get(path, _FILE_ATTRIBUTE_DIRECTORY),
        )
        return handle

    def open_anchor(
        self,
        path: Path,
        *,
        desired_access: int,
        share_mode: int,
        flags: int,
    ) -> int:
        return self.open_directory(
            path,
            desired_access=desired_access,
            share_mode=share_mode,
            flags=flags,
        )

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
        self.relative_directory_opens.append((parent_handle, name, create))
        path = self.handle_paths[parent_handle] / name
        if create and not path.exists():
            self.create_directory(path)
        return self.open_directory(
            path,
            desired_access=desired_access,
            share_mode=share_mode,
            flags=flags,
        )

    def create_directory(self, path: Path) -> None:
        self.created.append(path)
        self.active_when_created[path] = tuple(
            opened_path
            for index, opened_path in enumerate(self.opened)
            if 100 + index not in self.closed
        )
        path.mkdir(mode=0o700)

    def file_attributes(self, handle: int) -> int:
        self.queried.append(handle)
        return self.attributes[handle]

    def close_handle(self, handle: int) -> None:
        self.closed.append(handle)
        if handle in self.close_failures:
            raise OSError(f"synthetic close failure for {handle}")

    def attempt_in_place_reparse(
        self, path: Path, desired_access: int, outside: Path | None = None
    ) -> bool:
        for index, opened_path in enumerate(self.opened):
            handle = 100 + index
            if opened_path != path or handle in self.closed:
                continue
            _request_path, _access, share_mode, _flags = self.open_requests[index]
            if desired_access & _FILE_WRITE_DATA and not share_mode & _FILE_SHARE_WRITE:
                return False
        self.reparse_mutations.append(path)
        if outside is not None:
            self.named_redirects[path] = outside
        return True

    def lstat_at(self, directory_handle: int, filename: str) -> os.stat_result:
        self.relative_file_operations.append(("lstat", directory_handle, filename))
        return (self.handle_paths[directory_handle] / filename).lstat()

    def open_read_at(self, directory_handle: int, filename: str) -> int:
        self.relative_file_operations.append(("open-read", directory_handle, filename))
        return os.open(self.handle_paths[directory_handle] / filename, os.O_RDONLY)

    def open_new_at(self, directory_handle: int, filename: str) -> int:
        self.relative_file_operations.append(("open-new", directory_handle, filename))
        return os.open(
            self.handle_paths[directory_handle] / filename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )

    def unlink_at(
        self, directory_handle: int, filename: str, *, require_regular: bool
    ) -> None:
        operation = "unlink-regular" if require_regular else "unlink-entry"
        self.relative_file_operations.append((operation, directory_handle, filename))
        (self.handle_paths[directory_handle] / filename).unlink()

    def replace_at(self, directory_handle: int, source: str, destination: str) -> None:
        self.relative_file_operations.append(
            ("replace", directory_handle, f"{source}->{destination}")
        )
        os.replace(
            self.handle_paths[directory_handle] / source,
            self.handle_paths[directory_handle] / destination,
        )

    def flush_directory(self, directory_handle: int) -> None:
        self.flushed_handles.append(directory_handle)

    def assert_directory_handle(self, directory_handle: int) -> None:
        self.relative_file_operations.append(("assert", directory_handle, ""))
        if self.handle_paths[directory_handle] in self.reparse_mutations:
            raise UnsafeCacheError("opened model cache handle is a reparse point")


def _namespace(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "cache"
    version = root / "2.0.75"
    model = version / "u2net"
    model.mkdir(parents=True)
    return root, version, model


def _ancestor_chain(path: Path) -> list[Path]:
    chain = [Path(path.anchor)]
    current = chain[0]
    for component in path.parts[1:]:
        current /= component
        chain.append(current)
    return chain


def test_windows_binding_holds_anchor_through_every_cache_ancestor(
    tmp_path: Path,
) -> None:
    root, version, model = _namespace(tmp_path)
    api = FakeWindowsDirectoryApi()

    bound = _bind_windows(root, "2.0.75", "u2net", create=False, api=api)

    assert bound is not None
    expected = [*_ancestor_chain(root), version, model]
    assert api.opened == expected
    assert api.queried == list(range(100, 100 + len(expected)))
    assert api.closed == []
    assert api.open_requests == [
        (path, _GENERIC_READ, _FILE_SHARE_READ, _HARDENED_DIRECTORY_FLAGS)
        for path in expected
    ]
    assert api.relative_directory_opens == [
        (100 + index, path.name, False) for index, path in enumerate(expected[1:])
    ]

    bound.close()

    assert api.closed == list(reversed(range(100, 100 + len(expected))))


def test_windows_binding_blocks_redirect_of_ancestor_above_cache_root(
    tmp_path: Path,
) -> None:
    root, _version, _model = _namespace(tmp_path)
    protected = root.parent
    held = protected.with_name(f"{protected.name}-held")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    api = FakeWindowsDirectoryApi()
    redirect_blocked = False

    def attempt_redirect() -> None:
        nonlocal redirect_blocked
        active_paths = [
            path
            for index, path in enumerate(api.opened)
            if 100 + index not in api.closed
        ]
        if protected in active_paths:
            # Models Windows' no-FILE_SHARE_DELETE guarantee: once this ancestor
            # handle is held, a junction/rename swap cannot be performed.
            redirect_blocked = True
            return
        protected.rename(held)
        protected.symlink_to(outside, target_is_directory=True)

    api.before_open[root] = attempt_redirect

    bound = _bind_windows(root, "2.0.75", "u2net", create=False, api=api)

    assert bound is not None
    assert redirect_blocked is True
    assert protected.is_dir()
    assert not protected.is_symlink()
    bound.close()


def test_windows_binding_creates_each_missing_segment_under_held_ancestors(
    tmp_path: Path,
) -> None:
    root = tmp_path / "new-parent" / "cache"
    version = root / "2.0.75"
    model = version / "u2net"
    api = FakeWindowsDirectoryApi()

    bound = _bind_windows(root, "2.0.75", "u2net", create=True, api=api)

    assert bound is not None
    created = [root.parent, root, version, model]
    assert api.created == created
    for path in created:
        expected_held = _ancestor_chain(path)[:-1]
        assert api.active_when_created[path] == tuple(expected_held)
    assert all(
        request[1:] == (_GENERIC_READ, _FILE_SHARE_READ, _HARDENED_DIRECTORY_FLAGS)
        for request in api.open_requests
    )
    bound.close()


def test_windows_binding_tolerates_a_concurrent_writer_on_a_component(
    tmp_path: Path,
) -> None:
    # Binding used to refuse FILE_SHARE_WRITE, which Windows checks
    # symmetrically: holding one directory then blocked us from binding it
    # again ourselves, and every nested lookup does exactly that. Excluding
    # a concurrent writer never protected the contents anyway -- any process
    # running as the same user can write there -- while the guarantee that
    # matters, that a bound directory cannot be renamed or deleted under its
    # handle, comes from withholding FILE_SHARE_DELETE and is unchanged.
    root, _version, _model = _namespace(tmp_path)
    api = FakeWindowsDirectoryApi()
    api.foreign_write_paths.add(root)

    bound = _bind_windows(root, "2.0.75", "u2net", create=False, api=api)

    assert bound is not None
    assert api.opened[: len(_ancestor_chain(root))] == _ancestor_chain(root)
    assert bound.path == root / "2.0.75" / "u2net"


def test_windows_bound_directory_allows_attribute_mutation_but_not_data_writer(
    tmp_path: Path,
) -> None:
    root, _version, model = _namespace(tmp_path)
    outside = tmp_path / "outside-reparse"
    outside.mkdir()
    api = FakeWindowsDirectoryApi()

    bound = _bind_windows(root, "2.0.75", "u2net", create=False, api=api)

    assert bound is not None
    # A reparse point could always be set through FILE_WRITE_ATTRIBUTES; the
    # binding never claimed to stop that. What it stops is a rename or
    # delete under the handle, which needs FILE_SHARE_DELETE.
    assert api.attempt_in_place_reparse(model, _FILE_WRITE_ATTRIBUTES, outside) is True
    assert api.reparse_mutations == [model]
    assert api.named_redirects == {model: outside}
    bound.close()


def test_windows_bound_file_lifecycle_stays_relative_to_original_handle(
    tmp_path: Path,
) -> None:
    root, _version, model = _namespace(tmp_path)
    original = model / "source.onnx"
    original.write_bytes(b"inside")
    outside = tmp_path / "outside-lifecycle"
    outside.mkdir()
    outside_sentinel = outside / "source.onnx"
    outside_sentinel.write_bytes(b"outside")
    api = FakeWindowsDirectoryApi()

    bound = _bind_windows(root, "2.0.75", "u2net", create=False, api=api)

    assert bound is not None
    model_handle = 100 + len(api.opened) - 1
    assert api.attempt_in_place_reparse(model, _FILE_WRITE_ATTRIBUTES, outside) is True
    assert bound.lstat("source.onnx").st_size == len(b"inside")
    descriptor = bound.open_read("source.onnx")
    try:
        assert os.read(descriptor, 32) == b"inside"
    finally:
        os.close(descriptor)
    with bound.open_new("source.onnx.part") as output:
        output.write(b"replacement")
    bound.replace("source.onnx.part", "source.onnx")
    bound.fsync()
    with pytest.raises(UnsafeCacheError, match="reparse"):
        bound.assert_still_named()
    assert bound.unlink_regular("source.onnx") is True

    assert not original.exists()
    assert outside_sentinel.read_bytes() == b"outside"
    assert ("open-read", model_handle, "source.onnx") in api.relative_file_operations
    assert ("open-new", model_handle, "source.onnx.part") in (
        api.relative_file_operations
    )
    assert (
        "replace",
        model_handle,
        "source.onnx.part->source.onnx",
    ) in api.relative_file_operations
    assert ("unlink-regular", model_handle, "source.onnx") in (
        api.relative_file_operations
    )
    assert api.flushed_handles == [model_handle]
    bound.close()


def test_open_new_fdopen_failure_closes_transferred_fd_and_allows_windows_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _version, model = _namespace(tmp_path)
    api = FakeWindowsDirectoryApi()
    bound = _bind_windows(root, "2.0.75", "u2net", create=False, api=api)
    assert bound is not None
    primary = RuntimeError("synthetic fdopen failure")
    captured: list[int] = []

    def fail_fdopen(descriptor: int, *_args: object, **_kwargs: object) -> object:
        captured.append(descriptor)
        raise primary

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "fdopen", fail_fdopen)
            with pytest.raises(RuntimeError) as caught:
                bound.open_new("failed.part")

        assert caught.value is primary
        descriptor = captured[0]
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF
        assert bound.unlink_regular("failed.part") is True
        assert not (model / "failed.part").exists()
    finally:
        if captured:
            try:
                os.close(captured[0])
            except OSError:
                pass
        bound.close()


def test_open_new_fdopen_and_fd_close_failure_preserves_both_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _version, _model = _namespace(tmp_path)
    api = FakeWindowsDirectoryApi()
    bound = _bind_windows(root, "2.0.75", "u2net", create=False, api=api)
    assert bound is not None
    real_close = os.close
    primary = RuntimeError("synthetic fdopen failure")
    cleanup = OSError("synthetic fd cleanup failure")
    captured: list[int] = []
    close_calls: list[int] = []

    def fail_fdopen(descriptor: int, *_args: object, **_kwargs: object) -> object:
        captured.append(descriptor)
        raise primary

    def fail_close(descriptor: int) -> None:
        close_calls.append(descriptor)
        raise cleanup

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "fdopen", fail_fdopen)
            patch.setattr(os, "close", fail_close)
            with pytest.raises(BaseException) as caught:
                bound.open_new("failed.part")

        descriptor = captured[0]
        failure = caught.value
        assert isinstance(failure, cache_fs.FileDescriptorCloseError)
        assert failure is not primary
        assert failure.__cause__ is primary
        assert failure.primary_error is primary
        assert failure.close_error is cleanup
        assert close_calls == [descriptor]
    finally:
        if captured:
            real_close(captured[0])
            bound.unlink_regular("failed.part")
        bound.close()


def test_windows_native_open_new_validation_failure_closes_before_transfer() -> None:
    closed: list[int] = []
    primary = UnsafeCacheError("synthetic validation failure")

    class TestApi(cache_fs._CtypesWindowsDirectoryApi):
        def _open_relative(self, *_args: object, **_kwargs: object) -> int:
            return 321

        def _require_regular_file_handle(self, handle: int) -> None:
            assert handle == 321
            raise primary

        def _handle_to_fd(self, _handle: int, _flags: int) -> int:
            raise AssertionError("CRT ownership transfer must not run")

        def close_handle(self, handle: int) -> None:
            closed.append(handle)

    api = object.__new__(TestApi)

    with pytest.raises(UnsafeCacheError) as caught:
        api.open_new_at(123, "model.onnx.part")

    assert caught.value is primary
    assert closed == [321]


def test_open_new_success_transfers_fd_ownership_to_file_object(
    tmp_path: Path,
) -> None:
    root, _version, model = _namespace(tmp_path)
    api = FakeWindowsDirectoryApi()
    opened_descriptors: list[int] = []
    original_open_new = api.open_new_at

    def capture_open_new(directory_handle: int, filename: str) -> int:
        descriptor = original_open_new(directory_handle, filename)
        opened_descriptors.append(descriptor)
        return descriptor

    api.open_new_at = capture_open_new  # type: ignore[method-assign]
    bound = _bind_windows(root, "2.0.75", "u2net", create=False, api=api)
    assert bound is not None

    try:
        with bound.open_new("success.part") as output:
            output.write(b"verified")
            os.fstat(opened_descriptors[0])

        with pytest.raises(OSError) as closed:
            os.fstat(opened_descriptors[0])
        assert closed.value.errno == errno.EBADF
        assert (model / "success.part").read_bytes() == b"verified"
    finally:
        bound.unlink_regular("success.part")
        bound.close()


def test_windows_binding_midway_native_open_error_closes_all_prior_handles(
    tmp_path: Path,
) -> None:
    root, _version, _model = _namespace(tmp_path)
    api = FakeWindowsDirectoryApi()
    api.open_failures[root] = OSError("synthetic native open failure")

    with pytest.raises(OSError, match="native open failure"):
        _bind_windows(root, "2.0.75", "u2net", create=False, api=api)

    expected_opened = _ancestor_chain(root)[:-1]
    handles = list(range(100, 100 + len(expected_opened)))
    assert api.opened == expected_opened
    assert api.closed == list(reversed(handles))


def test_windows_native_anchor_open_uses_read_only_share_and_reparse_flags() -> None:
    calls: list[tuple[object, ...]] = []
    api = object.__new__(cache_fs._CtypesWindowsDirectoryApi)

    def create_file(*args: object) -> int:
        calls.append(args)
        return 123

    api._create_file = create_file
    api._invalid = -1

    assert (
        api.open_anchor(
            Path("C:/cache"),
            desired_access=_GENERIC_READ,
            share_mode=_FILE_SHARE_READ,
            flags=_HARDENED_DIRECTORY_FLAGS,
        )
        == 123
    )
    assert len(calls) == 1
    call = calls[0]
    assert call[2] == _FILE_SHARE_READ
    assert call[4] == 3
    assert call[5] == 0x02000000 | 0x00200000


def test_windows_native_child_open_is_root_handle_relative() -> None:
    calls: list[tuple[int, str, int, int, int, int]] = []
    api = object.__new__(cache_fs._CtypesWindowsDirectoryApi)

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

    def nt_create_file(*args: object) -> int:
        handle_pointer = ctypes.cast(args[0], ctypes.POINTER(ctypes.c_void_p))
        handle_pointer.contents.value = 321
        attributes = ctypes.cast(args[2], ctypes.POINTER(ObjectAttributes)).contents
        name_info = attributes.ObjectName.contents
        name = ctypes.string_at(name_info.Buffer, name_info.Length).decode("utf-16-le")
        calls.append(
            (
                int(attributes.RootDirectory),
                name,
                int(args[1]),
                int(args[6]),
                int(args[7]),
                int(args[8]),
            )
        )
        return 0

    api._nt_create_file = nt_create_file

    handle = api.open_child_directory(
        123,
        "cache",
        create=True,
        desired_access=_GENERIC_READ,
        share_mode=_FILE_SHARE_READ,
        flags=_HARDENED_DIRECTORY_FLAGS,
    )

    assert handle == 321
    # SYNCHRONIZE must reach NtCreateFile: without it the kernel rejects
    # FILE_SYNCHRONOUS_IO_NONALERT with STATUS_INVALID_PARAMETER, which is
    # how a real Windows build failed on the first path component.
    assert calls == [
        (
            123,
            "cache",
            _GENERIC_READ | 0x00100000,
            _FILE_SHARE_READ,
            3,
            0x00000001 | 0x00000020 | 0x00200000,
        )
    ]


def test_windows_native_replace_uses_bound_source_handle_and_simple_name() -> None:
    calls: list[tuple[int, int | None, int, str, int]] = []
    closed: list[int] = []

    class TestApi(cache_fs._CtypesWindowsDirectoryApi):
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
            assert (parent_handle, name) == (123, "source.part")
            assert desired_access & 0x00010000
            assert (share_mode, disposition) == (_FILE_SHARE_READ, 1)
            assert options & 0x00200000
            return 321

        def _require_regular_file_handle(self, handle: int) -> None:
            assert handle == 321

        def close_handle(self, handle: int) -> None:
            closed.append(handle)

    class FileRenameInformation(ctypes.Structure):
        _fields_ = (
            ("ReplaceIfExists", ctypes.c_ubyte),
            ("RootDirectory", ctypes.c_void_p),
            ("FileNameLength", ctypes.c_uint32),
            ("FileName", ctypes.c_uint16 * 1),
        )

    def nt_set_information_file(*args: object) -> int:
        handle = int(args[0])
        raw = ctypes.cast(args[2], ctypes.POINTER(FileRenameInformation)).contents
        buffer_address = ctypes.addressof(args[2])
        name_bytes = ctypes.string_at(
            buffer_address + FileRenameInformation.FileName.offset,
            raw.FileNameLength,
        )
        calls.append(
            (
                handle,
                int(raw.RootDirectory) if raw.RootDirectory is not None else None,
                int(args[4]),
                name_bytes.decode("utf-16-le"),
                int(args[3]),
            )
        )
        return 0

    api = object.__new__(TestApi)
    api._nt_set_information_file = nt_set_information_file

    api.replace_at(123, "source.part", "model.onnx")

    expected_size = ctypes.sizeof(FileRenameInformation) + len(
        "model.onnx".encode("utf-16-le")
    )
    assert calls == [(321, None, 10, "model.onnx", expected_size)]
    assert closed == [321]


def test_windows_native_publication_file_ops_share_read_write_and_delete() -> None:
    opened: list[tuple[str, int, int, int]] = []
    closed: list[int] = []
    regular = os.stat_result((0o100600, 1, 1, 1, 0, 0, 7, 0, 0, 0))

    class TestApi(cache_fs._CtypesWindowsDirectoryApi):
        def _open_relative(
            self,
            _parent_handle: int,
            name: str,
            *,
            desired_access: int,
            share_mode: int,
            disposition: int,
            options: int,
        ) -> int:
            del options
            opened.append((name, desired_access, share_mode, disposition))
            return 321

        def _require_regular_file_handle(self, handle: int) -> None:
            assert handle == 321

        def _handle_to_fd(self, handle: int, flags: int) -> int:
            assert handle == 321
            assert flags in {os.O_RDONLY, os.O_RDWR}
            return 322 if flags == os.O_RDWR else 323

        def _stat_handle(self, handle: int) -> os.stat_result:
            assert handle == 321
            return regular

        def close_handle(self, handle: int) -> None:
            closed.append(handle)

    api = object.__new__(TestApi)

    assert api.open_new_publication_read_write_at(123, "pending") == 322
    assert api.open_publication_read_write_at(123, "pending") == 322
    assert api.publication_lstat_at(123, "pending") == regular
    assert api.open_publication_read_at(123, "pending") == 323

    publication_share = _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE
    assert opened == [
        (
            "pending",
            _GENERIC_READ | _GENERIC_WRITE,
            publication_share,
            2,
        ),
        (
            "pending",
            _GENERIC_READ | _GENERIC_WRITE,
            publication_share,
            1,
        ),
        ("pending", 0x00100080, publication_share, 1),
        ("pending", _GENERIC_READ, publication_share, 1),
    ]
    assert closed == [321]


def test_windows_native_publication_parent_open_is_scoped_and_bidirectional() -> None:
    opened: list[tuple[int, str, int, int, int]] = []

    class TestApi(cache_fs._CtypesWindowsDirectoryApi):
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
            opened.append(
                (
                    parent_handle,
                    name,
                    desired_access,
                    share_mode,
                    disposition,
                )
            )
            assert options & 0x00000001
            return 321

    api = object.__new__(TestApi)
    publication_share = _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE

    assert (
        api.open_publication_child_directory(
            123,
            "exports",
            create=False,
            desired_access=cache_fs._WINDOWS_PUBLICATION_DIRECTORY_ACCESS,
            share_mode=publication_share,
            flags=cache_fs._WINDOWS_DIRECTORY_FLAGS,
        )
        == 321
    )
    assert opened == [
        (
            123,
            "exports",
            cache_fs._WINDOWS_PUBLICATION_DIRECTORY_ACCESS,
            publication_share,
            1,
        )
    ]
    assert not cache_fs._WINDOWS_PUBLICATION_DIRECTORY_ACCESS & _GENERIC_READ
    assert not cache_fs._WINDOWS_PUBLICATION_DIRECTORY_ACCESS & _GENERIC_WRITE

    with pytest.raises(ValueError, match="publication directory"):
        api.open_publication_child_directory(
            123,
            "exports",
            create=False,
            desired_access=cache_fs._WINDOWS_PUBLICATION_DIRECTORY_ACCESS,
            share_mode=_FILE_SHARE_READ,
            flags=cache_fs._WINDOWS_DIRECTORY_FLAGS,
        )


def test_windows_native_publication_replace_uses_bidirectional_share() -> None:
    opened: list[tuple[int, str, int, int]] = []
    renames: list[tuple[int, int, int]] = []
    closed: list[int] = []

    class TestApi(cache_fs._CtypesWindowsDirectoryApi):
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
            del options
            opened.append((parent_handle, name, desired_access, share_mode))
            assert disposition == 1
            return 321

        def _require_regular_file_handle(self, handle: int) -> None:
            assert handle == 321

        def close_handle(self, handle: int) -> None:
            closed.append(handle)

    class FileRenameInformationEx(ctypes.Structure):
        _fields_ = (
            ("Flags", ctypes.c_uint32),
            ("RootDirectory", ctypes.c_void_p),
            ("FileNameLength", ctypes.c_uint32),
            ("FileName", ctypes.c_uint16 * 1),
        )

    def nt_set_information_file(*args: object) -> int:
        raw = ctypes.cast(args[2], ctypes.POINTER(FileRenameInformationEx)).contents
        renames.append((int(args[4]), int(raw.Flags), int(raw.RootDirectory)))
        return 0

    api = object.__new__(TestApi)
    api._nt_set_information_file = nt_set_information_file

    api.replace_publication_at(123, "pending", "recovery")

    assert opened == [
        (
            123,
            "pending",
            0x00010000 | 0x00000080 | 0x00100000,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        )
    ]
    assert renames == [(65, 0x00000001 | 0x00000002, 123)]
    assert closed == [321]


@pytest.mark.parametrize("replace_existing", [False, True])
def test_windows_native_publication_rename_binds_both_directory_handles(
    replace_existing: bool,
) -> None:
    opened: list[tuple[int, str, int]] = []
    renamed: list[tuple[int, bool, int, str]] = []
    closed: list[int] = []

    class TestApi(cache_fs._CtypesWindowsDirectoryApi):
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
            del desired_access, disposition, options
            opened.append((parent_handle, name, share_mode))
            return 321

        def _require_regular_file_handle(self, handle: int) -> None:
            assert handle == 321

        def close_handle(self, handle: int) -> None:
            closed.append(handle)

    class FileRenameInformationEx(ctypes.Structure):
        _fields_ = (
            ("Flags", ctypes.c_uint32),
            ("RootDirectory", ctypes.c_void_p),
            ("FileNameLength", ctypes.c_uint32),
            ("FileName", ctypes.c_uint16 * 1),
        )

    def nt_set_information_file(*args: object) -> int:
        assert int(args[4]) == 65
        raw = ctypes.cast(args[2], ctypes.POINTER(FileRenameInformationEx)).contents
        buffer_address = ctypes.addressof(args[2])
        name_bytes = ctypes.string_at(
            buffer_address + FileRenameInformationEx.FileName.offset,
            raw.FileNameLength,
        )
        renamed.append(
            (
                int(args[0]),
                bool(raw.Flags & 0x00000001),
                int(raw.RootDirectory),
                name_bytes.decode("utf-16-le"),
            )
        )
        assert int(raw.Flags) == 0x00000002 | (0x00000001 if replace_existing else 0)
        return 0

    api = object.__new__(TestApi)
    api._nt_set_information_file = nt_set_information_file

    api.rename_publication_at(
        123,
        "source.publish",
        456,
        "output.webp",
        replace=replace_existing,
    )

    assert opened == [
        (
            123,
            "source.publish",
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        )
    ]
    assert renamed == [(321, replace_existing, 456, "output.webp")]
    assert closed == [321]


def test_windows_native_read_transfers_handle_ownership_to_crt_fd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []

    class TestApi(cache_fs._CtypesWindowsDirectoryApi):
        def _open_relative(self, *_args: object, **_kwargs: object) -> int:
            return 321

        def _require_regular_file_handle(self, handle: int) -> None:
            assert handle == 321

        def close_handle(self, handle: int) -> None:
            closed.append(handle)

    api = object.__new__(TestApi)
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(open_osfhandle=lambda handle, _flags: handle + 1),
    )

    assert api.open_read_at(123, "model.onnx") == 322
    assert closed == []

    def fail_conversion(_handle: int, _flags: int) -> int:
        raise OSError("synthetic CRT ownership failure")

    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(open_osfhandle=fail_conversion),
    )
    with pytest.raises(OSError, match="CRT ownership"):
        api.open_read_at(123, "model.onnx")
    assert closed == [321]


def test_windows_path_chain_supports_drive_and_unc_anchors() -> None:
    assert cache_fs._windows_path_chain(  # type: ignore[attr-defined]
        PureWindowsPath("C:/Users/alice/cache"), "2.0.75", "u2net"
    ) == (
        PureWindowsPath("C:/"),
        PureWindowsPath("C:/Users"),
        PureWindowsPath("C:/Users/alice"),
        PureWindowsPath("C:/Users/alice/cache"),
        PureWindowsPath("C:/Users/alice/cache/2.0.75"),
        PureWindowsPath("C:/Users/alice/cache/2.0.75/u2net"),
    )
    assert cache_fs._windows_path_chain(  # type: ignore[attr-defined]
        PureWindowsPath("//server/share/cache"), "2.0.75", "u2net"
    ) == (
        PureWindowsPath("//server/share/"),
        PureWindowsPath("//server/share/cache"),
        PureWindowsPath("//server/share/cache/2.0.75"),
        PureWindowsPath("//server/share/cache/2.0.75/u2net"),
    )


@pytest.mark.parametrize(
    "root",
    [
        PureWindowsPath("cache"),
        PureWindowsPath("C:cache"),
        PureWindowsPath("/cache"),
    ],
)
def test_windows_path_chain_rejects_unanchored_or_drive_relative_roots(
    root: PureWindowsPath,
) -> None:
    with pytest.raises(UnsafeCacheError, match="absolute"):
        cache_fs._windows_path_chain(  # type: ignore[attr-defined]
            root, "2.0.75", "u2net"
        )


@pytest.mark.parametrize(
    "root",
    [
        PureWindowsPath("C:/cache/../escape"),
        PureWindowsPath("//?/C:/cache"),
        PureWindowsPath("//./C:/cache"),
        PureWindowsPath("//server/../cache"),
        PureWindowsPath("//../share/cache"),
        PureWindowsPath("Ä:/cache"),
    ],
)
def test_windows_path_chain_rejects_traversal_and_device_namespaces(
    root: PureWindowsPath,
) -> None:
    with pytest.raises(UnsafeCacheError, match="invalid"):
        cache_fs._windows_path_chain(  # type: ignore[attr-defined]
            root, "2.0.75", "u2net"
        )


def test_windows_binding_validates_each_open_handle_and_holds_until_close(
    tmp_path: Path,
) -> None:
    root, version, model = _namespace(tmp_path)
    api = FakeWindowsDirectoryApi()

    bound = _bind_windows(root, "2.0.75", "u2net", create=False, api=api)

    assert bound is not None
    expected = [*_ancestor_chain(root), version, model]
    handles = list(range(100, 100 + len(expected)))
    assert api.opened == expected
    assert api.queried == handles
    assert api.closed == []

    bound.close()
    bound.close()

    assert api.closed == list(reversed(handles))


def test_windows_binding_rejects_junction_returned_by_native_open(
    tmp_path: Path,
) -> None:
    root, _version, model = _namespace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    held = model.with_name("u2net-held")
    api = FakeWindowsDirectoryApi()

    def swap_to_junction() -> None:
        model.rename(held)
        model.symlink_to(outside, target_is_directory=True)
        next_handle = 100 + len(api.opened)
        api.attributes[next_handle] = (
            _FILE_ATTRIBUTE_DIRECTORY | _FILE_ATTRIBUTE_REPARSE_POINT
        )

    api.before_open[model] = swap_to_junction

    with pytest.raises(UnsafeCacheError, match="reparse"):
        _bind_windows(root, "2.0.75", "u2net", create=False, api=api)

    expected = [*_ancestor_chain(root), root / "2.0.75", model]
    handles = list(range(100, 100 + len(expected)))
    assert api.opened == expected
    assert api.queried == handles
    assert api.closed == list(reversed(handles))


def test_windows_binding_rejects_open_handle_without_directory_identity(
    tmp_path: Path,
) -> None:
    root, _version, model = _namespace(tmp_path)
    api = FakeWindowsDirectoryApi()
    api.attribute_overrides[model] = 0

    with pytest.raises(UnsafeCacheError, match="not a directory"):
        _bind_windows(root, "2.0.75", "u2net", create=False, api=api)

    expected = [*_ancestor_chain(root), root / "2.0.75", model]
    handles = list(range(100, 100 + len(expected)))
    assert api.opened[-1] == model
    assert api.queried == handles
    assert api.closed == list(reversed(handles))


def test_windows_missing_namespace_closes_each_prior_handle_once_per_call(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    (root / "2.0.75").mkdir(parents=True)
    api = FakeWindowsDirectoryApi()

    assert _bind_windows(root, "2.0.75", "u2net", create=False, api=api) is None
    assert _bind_windows(root, "2.0.75", "u2net", create=False, api=api) is None

    expected_call = [*_ancestor_chain(root), root / "2.0.75"]
    handles = list(range(100, 100 + len(expected_call) * 2))
    assert api.opened == [*expected_call, *expected_call]
    assert api.queried == handles
    split = len(expected_call)
    assert api.closed == [
        *reversed(handles[:split]),
        *reversed(handles[split:]),
    ]


def test_windows_early_return_attempts_every_close_after_one_close_failure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    (root / "2.0.75").mkdir(parents=True)
    api = FakeWindowsDirectoryApi()
    expected = [*_ancestor_chain(root), root / "2.0.75"]
    handles = list(range(100, 100 + len(expected)))
    api.close_failures.add(handles[-1])

    with pytest.raises(OSError, match="synthetic close failure"):
        _bind_windows(root, "2.0.75", "u2net", create=False, api=api)

    assert api.closed == list(reversed(handles))


def test_windows_native_mkdir_is_relative_and_flushes_bound_parent() -> None:
    opened: list[tuple[int, str, int, int, int]] = []
    closed: list[int] = []
    flushed: list[int] = []

    class TestApi(cache_fs._CtypesWindowsDirectoryApi):
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
            opened.append((parent_handle, name, desired_access, disposition, options))
            assert share_mode == _FILE_SHARE_READ
            return 77

        def file_attributes(self, handle: int) -> int:
            assert handle == 77
            return _FILE_ATTRIBUTE_DIRECTORY

        def close_handle(self, handle: int) -> None:
            closed.append(handle)

        def flush_directory_strict(self, handle: int) -> None:
            flushed.append(handle)

    api = object.__new__(TestApi)

    api.mkdir_at(12, "child", exist_ok=False)

    assert opened == [
        (
            12,
            "child",
            _GENERIC_READ | _GENERIC_WRITE,
            2,
            0x00000001 | 0x00000020 | 0x00200000,
        )
    ]
    assert closed == [77]
    assert flushed == [12]


def test_windows_native_mkdir_removes_new_child_when_parent_flush_fails() -> None:
    removed: list[tuple[int, str]] = []

    class TestApi(cache_fs._CtypesWindowsDirectoryApi):
        def _open_relative(self, *_args: object, **_kwargs: object) -> int:
            return 77

        def file_attributes(self, _handle: int) -> int:
            return _FILE_ATTRIBUTE_DIRECTORY

        def close_handle(self, _handle: int) -> None:
            pass

        def flush_directory_strict(self, _handle: int) -> None:
            raise OSError("injected directory flush failure")

        def rmdir_at(self, handle: int, name: str) -> None:
            removed.append((handle, name))

    api = object.__new__(TestApi)

    with pytest.raises(OSError, match="flush failure"):
        api.mkdir_at(12, "child", exist_ok=False)

    assert removed == [(12, "child")]


def test_windows_native_enumeration_parses_names_from_bound_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[int] = []
    regular = os.stat_result((0o100600, 1, 1, 1, 0, 0, 1, 0, 0, 0))

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

    class TestApi(cache_fs._CtypesWindowsDirectoryApi):
        def _get_information(
            self, handle: int, info_class: int, buffer: object, size: int
        ) -> int:
            assert handle == 88
            queries.append(info_class)
            if len(queries) > 1:
                return 0
            ctypes.memset(ctypes.addressof(buffer), 0, size)
            offset = 0
            for index, name in enumerate(("alpha.bin", "beta.bin")):
                encoded = name.encode("utf-16-le")
                address = ctypes.addressof(buffer) + offset
                info = ctypes.cast(
                    address, ctypes.POINTER(FileIdBothDirectoryInfo)
                ).contents
                info.FileNameLength = len(encoded)
                ctypes.memmove(
                    address + FileIdBothDirectoryInfo.FileName.offset,
                    encoded,
                    len(encoded),
                )
                if index == 0:
                    record_size = (
                        FileIdBothDirectoryInfo.FileName.offset + len(encoded) + 7
                    ) & ~7
                    info.NextEntryOffset = record_size
                    offset += record_size
            return 1

        def lstat_at(self, directory_handle: int, filename: str) -> os.stat_result:
            assert directory_handle == 88
            assert filename in {"alpha.bin", "beta.bin"}
            return regular

    monkeypatch.setattr(ctypes, "get_last_error", lambda: 18, raising=False)
    api = object.__new__(TestApi)

    entries = list(api.iter_entries_at(88, max_entries=2))

    assert [name for name, _info in entries] == ["alpha.bin", "beta.bin"]
    assert queries == [11, 10]


@pytest.mark.skipif(os.name != "nt", reason="exercises the real Windows NT API")
def test_real_windows_api_binds_a_cache_directory_end_to_end(tmp_path: Path) -> None:
    """Bind through the actual ctypes implementation, not a fake API.

    Every other Windows binding test substitutes _WindowsDirectoryApi, so a
    defect in the real NtCreateFile call reaches users untested: the frozen
    build failed with "[Errno 87] relative cache operation failed: 'Users'"
    while the whole suite was green.
    """
    bound = BoundModelDirectory.bind(tmp_path, "2.0.75", "u2netp", create=True)

    assert bound is not None
    with bound:
        assert bound.path.is_dir()
    assert (tmp_path / "2.0.75" / "u2netp").is_dir()

    reopened = BoundModelDirectory.bind(tmp_path, "2.0.75", "u2netp", create=False)

    assert reopened is not None
    reopened.close()


@pytest.mark.skipif(os.name != "nt", reason="compares the two Windows stat sources")
def test_bound_lstat_matches_os_fstat_identity(tmp_path: Path) -> None:
    """The cache's own lstat must agree with os.fstat about file identity.

    Several checks compare an identity from bound.lstat() against one from
    os.fstat(); they are only meaningful if both sources report st_dev and
    st_ino the same way.
    """
    bound = BoundModelDirectory.bind(tmp_path, "2.0.75", "u2netp", create=True)
    assert bound is not None
    with bound:
        (bound.path / "weight.onnx").write_bytes(b"weight")
        from_api = bound.lstat("weight.onnx")
        descriptor = bound.open_read("weight.onnx")
        try:
            from_os = os.fstat(descriptor)
        finally:
            os.close(descriptor)

    assert (from_api.st_dev, from_api.st_ino) == (from_os.st_dev, from_os.st_ino), (
        f"bound.lstat dev/ino {from_api.st_dev}/{from_api.st_ino} != "
        f"os.fstat {from_os.st_dev}/{from_os.st_ino}"
    )
    # Workspace frame checks compare st_mtime_ns across the same two
    # sources, so a value rounded through float seconds fails them.
    assert from_api.st_mtime_ns == from_os.st_mtime_ns, (
        f"bound.lstat mtime_ns {from_api.st_mtime_ns} != os.fstat {from_os.st_mtime_ns}"
    )
    assert from_api.st_size == from_os.st_size


@pytest.mark.skipif(os.name != "nt", reason="binds against a real Windows ACL")
def test_publication_binds_a_directory_granting_only_modify(tmp_path: Path) -> None:
    """Modify is what an ordinary output folder grants; Full is not.

    FILE_DELETE_CHILD sits outside Modify (0x1301BF) and only inside Full
    Control, and asking for it refused a plain "C:\\Temp" outright. Nothing
    needs it: entries are removed through a child handle opened with DELETE.
    """
    import getpass
    import subprocess

    from matteloop.jobs.workspace._filesystem import _BoundDirectory

    target = tmp_path / "output"
    target.mkdir()
    granted = subprocess.run(
        [
            "icacls",
            str(target),
            "/inheritance:r",
            "/grant",
            f"{getpass.getuser()}:(OI)(CI)(M)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if granted.returncode != 0:
        pytest.skip(f"could not restrict the directory: {granted.stderr.strip()}")

    bound = _BoundDirectory._open_windows_publication(target)

    try:
        assert bound.path == target
    finally:
        bound.close()


@pytest.mark.skipif(os.name != "nt", reason="binds against a real Windows ACL")
def test_binding_a_directory_needs_no_more_than_the_rights_it_uses(
    tmp_path: Path,
) -> None:
    """A writable target must not require every right GENERIC_WRITE implies.

    The final component is opened with GENERIC_WRITE, which also demands
    FILE_WRITE_EA and FILE_WRITE_ATTRIBUTES. A directory that grants
    everything the binding actually does -- listing, traversing, creating
    and deleting entries -- but withholds those two is refused outright,
    which is how "C:\\Temp" came back as "cache entry access denied".
    """
    import getpass
    import subprocess

    target = tmp_path / "restricted"
    target.mkdir()
    denied = subprocess.run(
        ["icacls", str(target), "/deny", f"{getpass.getuser()}:(WEA,WA)"],
        capture_output=True,
        text=True,
        check=False,
    )
    if denied.returncode != 0:
        pytest.skip(f"could not restrict the directory: {denied.stderr.strip()}")

    bound = BoundModelDirectory.bind(target, "2.0.75", "u2netp", create=True)

    assert bound is not None
    bound.close()
