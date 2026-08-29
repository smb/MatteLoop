"""Deterministic source and frozen-runtime resource discovery."""

from __future__ import annotations

import sys
from pathlib import Path

_RESOURCE_DIRECTORY = "resources"


def resource_path(name: str, *, runtime_root: Path | None = None) -> Path:
    """Resolve one packaged resource without guessing through parent depths.

    ``runtime_root`` is an explicit seam for frozen-runtime validation. Nuitka
    standalone binaries resolve data relative to the executable; source runs
    resolve data relative to the repository's stable ``src`` layout.
    """
    if not name or Path(name).name != name:
        raise ValueError("resource name must be one plain filename")

    roots = _runtime_roots(runtime_root)
    candidates = tuple(root / _RESOURCE_DIRECTORY / name for root in roots)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _runtime_roots(runtime_root: Path | None) -> tuple[Path, ...]:
    if runtime_root is not None:
        return (Path(runtime_root).resolve(),)
    if getattr(sys, "frozen", False) or globals().get("__compiled__") is not None:
        executable_dir = Path(sys.executable).resolve().parent
        return (
            executable_dir,
            executable_dir.parent / "Resources",
        )
    return (Path(__file__).resolve().parents[2],)
