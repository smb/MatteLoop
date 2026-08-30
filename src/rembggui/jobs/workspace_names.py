"""Presentation-only names for durable cut-set directories."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

_CACHE_KEY_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MAX_STEM_LENGTH = 118
_INVALID_FILENAME_CHARS = frozenset('<>:"/\\|?*')


def readable_workspace_name(source: Path, cache_key: str) -> str:
    """Return a browsable name while leaving the full key authoritative."""
    if not isinstance(source, Path):
        raise TypeError("source must be a Path")
    if (
        not isinstance(cache_key, str)
        or _CACHE_KEY_PATTERN.fullmatch(cache_key) is None
    ):
        raise ValueError("cache_key must be a lowercase SHA-256")
    stem = unicodedata.normalize("NFKC", source.stem)
    safe = "".join(
        "_"
        if char in _INVALID_FILENAME_CHARS or ord(char) < 32 or ord(char) == 127
        else char
        for char in stem
    ).strip().lstrip(".")[:_MAX_STEM_LENGTH].rstrip(" .")
    if not safe:
        safe = "source"
    return f"{safe}-{cache_key[:8]}"
