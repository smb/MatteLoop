from __future__ import annotations

from pathlib import Path

import pytest

from rembggui.jobs.models.cache_fs import UnsafeCacheError, _bind_windows

_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400


class FakeWindowsDirectoryApi:
    def __init__(self) -> None:
        self.opened: list[Path] = []
        self.queried: list[int] = []
        self.closed: list[int] = []
        self.close_failures: set[int] = set()
        self.attributes: dict[int, int] = {}
        self.before_open: dict[Path, object] = {}

    def open_directory(self, path: Path) -> int:
        callback = self.before_open.get(path)
        if callable(callback):
            callback()
        handle = 100 + len(self.opened)
        self.opened.append(path)
        self.attributes.setdefault(handle, _FILE_ATTRIBUTE_DIRECTORY)
        return handle

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


def test_windows_binding_validates_each_open_handle_and_holds_until_close(
    tmp_path: Path,
) -> None:
    root, version, model = _namespace(tmp_path)
    api = FakeWindowsDirectoryApi()

    bound = _bind_windows(root, "2.0.72", "u2net", create=False, api=api)

    assert bound is not None
    assert api.opened == [root, version, model]
    assert api.queried == [100, 101, 102]
    assert api.closed == []

    bound.close()
    bound.close()

    assert api.closed == [102, 101, 100]


def test_windows_binding_rejects_junction_swapped_between_precheck_and_open(
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

    assert api.opened == [root, root / "2.0.72", model]
    assert api.queried == [100, 101, 102]
    assert api.closed == [102, 101, 100]


def test_windows_binding_rejects_open_handle_without_directory_identity(
    tmp_path: Path,
) -> None:
    root, _version, model = _namespace(tmp_path)
    api = FakeWindowsDirectoryApi()
    api.attributes[102] = 0

    with pytest.raises(UnsafeCacheError, match="not a directory"):
        _bind_windows(root, "2.0.72", "u2net", create=False, api=api)

    assert api.opened[-1] == model
    assert api.queried == [100, 101, 102]
    assert api.closed == [102, 101, 100]


def test_windows_missing_namespace_closes_each_prior_handle_once_per_call(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    (root / "2.0.72").mkdir(parents=True)
    api = FakeWindowsDirectoryApi()

    assert _bind_windows(root, "2.0.72", "u2net", create=False, api=api) is None
    assert _bind_windows(root, "2.0.72", "u2net", create=False, api=api) is None

    assert api.opened == [root, root / "2.0.72", root, root / "2.0.72"]
    assert api.queried == [100, 101, 102, 103]
    assert api.closed == [101, 100, 103, 102]


def test_windows_early_return_attempts_every_close_after_one_close_failure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    (root / "2.0.72").mkdir(parents=True)
    api = FakeWindowsDirectoryApi()
    api.close_failures.add(101)

    with pytest.raises(OSError, match="synthetic close failure"):
        _bind_windows(root, "2.0.72", "u2net", create=False, api=api)

    assert api.closed == [101, 100]
