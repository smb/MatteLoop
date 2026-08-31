"""Product identity and compatibility paths for user-owned data."""

from __future__ import annotations

from pathlib import Path

from platformdirs import user_cache_dir

PRODUCT_NAME = "MatteLoop"
PACKAGE_NAME = "matteloop"
NEW_CACHE_NAME = "matteloop"
LEGACY_CACHE_NAME = "rembggui"
NEW_WORKSPACE_NAME = ".matteloop-work"
LEGACY_WORKSPACE_NAME = ".rembggui-work"


def cache_subdirectory(*parts: str) -> Path:
    """Return a cache directory, adopting the pre-rename one when it exists.

    The rename must not orphan user-owned data. Weights, compiled provider
    caches and thumbnails all live under the product cache directory, so the
    same preference applies to each: use the new location, unless only the
    legacy one has content.
    """
    new_root = Path(user_cache_dir(NEW_CACHE_NAME)).joinpath(*parts)
    legacy_root = Path(user_cache_dir(LEGACY_CACHE_NAME)).joinpath(*parts)
    if new_root.exists() or not legacy_root.exists():
        return new_root
    return legacy_root


def model_cache_root() -> Path:
    """Return the preferred model cache, preserving an existing legacy cache."""
    return cache_subdirectory("models")
