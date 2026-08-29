from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

import rembggui.jobs.models.cache_fs as cache_fs
from rembggui.jobs.models.cache_fs import UnsafeCacheError, _bind_windows

_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_WRITE_DATA = 0x00000002
_FILE_WRITE_ATTRIBUTES = 0x00000100
_GENERIC_READ = 0x80000000
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
    version = root / "2.0.72"
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

    bound = _bind_windows(root, "2.0.72", "u2net", create=False, api=api)

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

    bound = _bind_windows(root, "2.0.72", "u2net", create=False, api=api)

    assert bound is not None
    assert redirect_blocked is True
    assert protected.is_dir()
    assert not protected.is_symlink()
    bound.close()


def test_windows_binding_creates_each_missing_segment_under_held_ancestors(
    tmp_path: Path,
) -> None:
    root = tmp_path / "new-parent" / "cache"
    version = root / "2.0.72"
    model = version / "u2net"
    api = FakeWindowsDirectoryApi()

    bound = _bind_windows(root, "2.0.72", "u2net", create=True, api=api)

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


def test_windows_binding_fails_when_foreign_writer_already_owns_component(
    tmp_path: Path,
) -> None:
    root, _version, _model = _namespace(tmp_path)
    api = FakeWindowsDirectoryApi()
    api.foreign_write_paths.add(root)

    with pytest.raises(OSError, match="sharing violation"):
        _bind_windows(root, "2.0.72", "u2net", create=False, api=api)

    opened_before_conflict = _ancestor_chain(root)[:-1]
    handles = list(range(100, 100 + len(opened_before_conflict)))
    assert api.opened == opened_before_conflict
    assert api.closed == list(reversed(handles))


def test_windows_bound_directory_allows_attribute_mutation_but_not_data_writer(
    tmp_path: Path,
) -> None:
    root, _version, model = _namespace(tmp_path)
    outside = tmp_path / "outside-reparse"
    outside.mkdir()
    api = FakeWindowsDirectoryApi()

    bound = _bind_windows(root, "2.0.72", "u2net", create=False, api=api)

    assert bound is not None
    assert api.attempt_in_place_reparse(model, _FILE_WRITE_DATA) is False
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

    bound = _bind_windows(root, "2.0.72", "u2net", create=False, api=api)

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
    bound = _bind_windows(root, "2.0.72", "u2net", create=False, api=api)
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
    bound = _bind_windows(root, "2.0.72", "u2net", create=False, api=api)
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
    bound = _bind_windows(root, "2.0.72", "u2net", create=False, api=api)
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
        _bind_windows(root, "2.0.72", "u2net", create=False, api=api)

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
    assert calls == [
        (
            123,
            "cache",
            _GENERIC_READ,
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
        PureWindowsPath("C:/Users/alice/cache"), "2.0.72", "u2net"
    ) == (
        PureWindowsPath("C:/"),
        PureWindowsPath("C:/Users"),
        PureWindowsPath("C:/Users/alice"),
        PureWindowsPath("C:/Users/alice/cache"),
        PureWindowsPath("C:/Users/alice/cache/2.0.72"),
        PureWindowsPath("C:/Users/alice/cache/2.0.72/u2net"),
    )
    assert cache_fs._windows_path_chain(  # type: ignore[attr-defined]
        PureWindowsPath("//server/share/cache"), "2.0.72", "u2net"
    ) == (
        PureWindowsPath("//server/share/"),
        PureWindowsPath("//server/share/cache"),
        PureWindowsPath("//server/share/cache/2.0.72"),
        PureWindowsPath("//server/share/cache/2.0.72/u2net"),
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
            root, "2.0.72", "u2net"
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
            root, "2.0.72", "u2net"
        )


def test_windows_binding_validates_each_open_handle_and_holds_until_close(
    tmp_path: Path,
) -> None:
    root, version, model = _namespace(tmp_path)
    api = FakeWindowsDirectoryApi()

    bound = _bind_windows(root, "2.0.72", "u2net", create=False, api=api)

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
        _bind_windows(root, "2.0.72", "u2net", create=False, api=api)

    expected = [*_ancestor_chain(root), root / "2.0.72", model]
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
        _bind_windows(root, "2.0.72", "u2net", create=False, api=api)

    expected = [*_ancestor_chain(root), root / "2.0.72", model]
    handles = list(range(100, 100 + len(expected)))
    assert api.opened[-1] == model
    assert api.queried == handles
    assert api.closed == list(reversed(handles))


def test_windows_missing_namespace_closes_each_prior_handle_once_per_call(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    (root / "2.0.72").mkdir(parents=True)
    api = FakeWindowsDirectoryApi()

    assert _bind_windows(root, "2.0.72", "u2net", create=False, api=api) is None
    assert _bind_windows(root, "2.0.72", "u2net", create=False, api=api) is None

    expected_call = [*_ancestor_chain(root), root / "2.0.72"]
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
    (root / "2.0.72").mkdir(parents=True)
    api = FakeWindowsDirectoryApi()
    expected = [*_ancestor_chain(root), root / "2.0.72"]
    handles = list(range(100, 100 + len(expected)))
    api.close_failures.add(handles[-1])

    with pytest.raises(OSError, match="synthetic close failure"):
        _bind_windows(root, "2.0.72", "u2net", create=False, api=api)

    assert api.closed == list(reversed(handles))
