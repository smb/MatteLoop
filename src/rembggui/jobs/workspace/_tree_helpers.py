from __future__ import annotations

from typing import TYPE_CHECKING

# ruff: noqa: F403,F405
from ._common import *  # noqa: F403,F401

if TYPE_CHECKING:
    from ._errors import _unsafe_error
    from ._filesystem import _BoundDirectory
    from ._fs_helpers import _same_lexical_path
    from ._manifest_validation import _validate_component

__all__ = (
    "_bounded_bound_tree_size",
    "_bounded_tree_size",
    "_cleanup_snapshot",
    "_cleanup_staged_cut",
    "_remove_bound_contents",
    "_remove_bound_tree",
    "_remove_tree",
)


def _remove_tree(path: Path) -> None:
    """Remove one exact tree without ever traversing a link/reparse target."""
    if path.parent == path or not path.name:
        raise OSError("refusing to remove an unbounded path")
    with _BoundDirectory.open(path.parent) as parent:
        _remove_bound_tree(parent, path.name)


def _remove_bound_tree(parent: _BoundDirectory, name: str) -> None:
    _validate_component(name)
    info = parent.lstat(name)
    if stat.S_ISLNK(info.st_mode):
        parent.unlink(name)
        return
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("tree target is not a directory")
    with parent.open_child(name) as child:
        _remove_bound_contents(child, [0])
    parent.rmdir(name)
    parent.fsync()


def _remove_bound_contents(bound: _BoundDirectory, removed: list[int]) -> None:
    while True:
        selected: tuple[str, os.stat_result] | None = None
        for name, info in bound.iter_entries():
            removed[0] += 1
            if removed[0] > MAX_FRAME_COUNT + 16:
                raise OSError("tree entry count exceeds cleanup bound")
            selected = name, info
            break
        if selected is None:
            return
        name, info = selected
        _validate_component(name)
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            with bound.open_child(name) as child:
                _remove_bound_contents(child, removed)
            bound.rmdir(name)
        else:
            bound.unlink(name)


def _bounded_tree_size(path: Path) -> int:
    with _BoundDirectory.open(path) as bound:
        counter = [0]
        return _bounded_bound_tree_size(bound, counter)


def _bounded_bound_tree_size(bound: _BoundDirectory, counter: list[int]) -> int:
    total = 0
    for name, info in bound.iter_entries():
        counter[0] += 1
        if counter[0] > MAX_FRAME_COUNT + 16:
            raise _unsafe_error("scratch tree exceeds the size-scan bound")
        if stat.S_ISLNK(info.st_mode):
            raise _unsafe_error("scratch tree contains a symbolic link")
        if stat.S_ISDIR(info.st_mode):
            with bound.open_child(name) as child:
                total += _bounded_bound_tree_size(child, counter)
        elif stat.S_ISREG(info.st_mode):
            total += info.st_size
        else:
            raise _unsafe_error("scratch tree contains an unsafe entry")
    return total


def _cleanup_snapshot(path: Path, primary: AppError) -> None:
    if not path.exists():
        return
    try:
        _remove_tree(path)
    except (
        AppError,
        OSError,
        UnsafeCacheError,
        BoundDirectoryCloseError,
    ) as cleanup_error:
        primary.add_note(f"additional scratch cleanup failure: {cleanup_error}")


def _cleanup_staged_cut(
    path: Path,
    primary: AppError,
    *,
    parent: _BoundDirectory | None = None,
) -> None:
    try:
        if parent is not None:
            if not _same_lexical_path(parent.path, path.parent):
                raise _unsafe_error("staged-cut cleanup bound to the wrong directory")
            try:
                _remove_bound_tree(parent, path.name)
            except FileNotFoundError:
                pass
        elif path.exists():
            _remove_tree(path)
    except (
        AppError,
        OSError,
        UnsafeCacheError,
        BoundDirectoryCloseError,
    ) as cleanup_error:
        primary.add_note(f"additional staged-cut cleanup failure: {cleanup_error}")
