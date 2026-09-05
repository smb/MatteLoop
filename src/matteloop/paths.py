"""Product identity and paths for user-owned data."""

from __future__ import annotations

from pathlib import Path

from platformdirs import user_cache_dir

PRODUCT_NAME = "MatteLoop"
PACKAGE_NAME = "matteloop"
CACHE_NAME = "matteloop"
WORKSPACE_NAME = ".matteloop-work"


def cache_subdirectory(*parts: str) -> Path:
    """Return a cache directory under MatteLoop's user cache."""
    return Path(user_cache_dir(CACHE_NAME)).joinpath(*parts)


def model_cache_root() -> Path:
    """Return MatteLoop's model cache."""
    return cache_subdirectory("models")
