from __future__ import annotations

from pathlib import Path, PureWindowsPath

import pytest

import rembggui.jobs.models.cache_fs as cache_fs
from rembggui.jobs.models.cache_fs import UnsafeCacheError, _bind_windows

_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400


class FakeWindowsDirectoryApi:
    def __init__(self) -> None:
        self.opened: list[Path] = []
        self.created: list[Path] = []
        self.queried: list[int] = []
        self.closed: list[int] = []
        self.close_failures: set[int] = set()
        self.open_failures: dict[Path, BaseException] = {}
        self.attributes: dict[int, int] = {}
        self.attribute_overrides: dict[Path, int] = {}
        self.before_open: dict[Path, object] = {}
        self.active_when_created: dict[Path, tuple[Path, ...]] = {}

    def open_directory(self, path: Path) -> int:
        callback = self.before_open.get(path)
        if callable(callback):
            callback()
        failure = self.open_failures.get(path)
        if failure is not None:
            raise failure
        if not path.exists():
            raise FileNotFoundError(path)
        handle = 100 + len(self.opened)
        self.opened.append(path)
        self.attributes.setdefault(
            handle,
            self.attribute_overrides.get(path, _FILE_ATTRIBUTE_DIRECTORY),
        )
        return handle

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


def test_windows_native_open_uses_reparse_identity_flags_without_delete_share() -> None:
    calls: list[tuple[object, ...]] = []
    api = object.__new__(cache_fs._CtypesWindowsDirectoryApi)

    def create_file(*args: object) -> int:
        calls.append(args)
        return 123

    api._create_file = create_file
    api._invalid = -1

    assert api.open_directory(Path("C:/cache")) == 123
    assert len(calls) == 1
    call = calls[0]
    assert call[2] == 0x00000001 | 0x00000002
    assert call[4] == 3
    assert call[5] == 0x02000000 | 0x00200000


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
