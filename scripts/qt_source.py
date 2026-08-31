"""Pinned Qt/PySide corresponding-source companion construction."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.media_stack.manifest import SourceSpec

_MANIFEST_FIELDS = frozenset(
    ("schema_version", "qt_version", "distributions", "sources")
)
_DISTRIBUTIONS = frozenset(
    ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6")
)
_SOURCE_NAMES = frozenset(("qtbase", "qtimageformats", "pyside-setup"))
_SOURCE_FIELDS = frozenset(("name", "version", "url", "sha256", "archive_root"))
_EXACT_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)+")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class QtSourceManifest:
    schema_version: int
    qt_version: str
    distributions: Mapping[str, str]
    sources: tuple[SourceSpec, ...]


def load_qt_source_manifest(path: Path) -> QtSourceManifest:
    """Load the strict corresponding-source contract from checked-in TOML."""
    with path.open("rb") as manifest_file:
        raw = tomllib.load(manifest_file)
    _require_keys(raw, _MANIFEST_FIELDS, "manifest")
    if raw["schema_version"] != 1:
        raise ValueError("unsupported manifest schema_version")
    version = _pinned_version(raw["qt_version"], "qt_version")
    distributions = _load_distributions(raw["distributions"], version)
    sources = _load_sources(raw["sources"], version)
    return QtSourceManifest(1, version, distributions, sources)


def qt_source_identity(
    manifest_path: Path,
    distributions: Mapping[str, str],
    evidence: Mapping[str, bytes],
    *,
    recipe_revision: int,
) -> str:
    """Return an identity covering the raw contract and included evidence."""
    digest = hashlib.sha256(manifest_path.read_bytes())
    contract = {
        "distributions": sorted(distributions.items()),
        "recipe_revision": recipe_revision,
    }
    digest.update(b"\0" + json.dumps(contract, separators=(",", ":")).encode())
    for name, content in sorted(evidence.items()):
        digest.update(b"\0" + name.encode() + b"\0" + content)
    return digest.hexdigest()[:24]


def _load_distributions(value: Any, version: str) -> dict[str, str]:
    distributions = _mapping(value, "distributions")
    _require_keys(distributions, _DISTRIBUTIONS, "distribution names")
    loaded = {
        name: _pinned_version(distributions[name], f"distributions.{name}")
        for name in sorted(_DISTRIBUTIONS)
    }
    if any(actual != version for actual in loaded.values()):
        raise ValueError("Qt/PySide distribution versions must match qt_version")
    return loaded


def _load_sources(value: Any, version: str) -> tuple[SourceSpec, ...]:
    if not isinstance(value, list):
        raise ValueError("sources must be an array of tables")
    sources = tuple(
        _load_source(raw, index, version) for index, raw in enumerate(value)
    )
    names = tuple(source.name for source in sources)
    if len(names) != len(set(names)) or set(names) != _SOURCE_NAMES:
        raise ValueError(f"source names must be exactly one each of {_SOURCE_NAMES!r}")
    return sources


def _load_source(value: Any, index: int, version: str) -> SourceSpec:
    context = f"sources[{index}]"
    source = _mapping(value, context)
    _require_keys(source, _SOURCE_FIELDS, f"{context} keys")
    name = _string(source["name"], f"{context}.name")
    source_version = _pinned_version(source["version"], f"{context}.version")
    if source_version != version:
        raise ValueError(f"{name} version must match qt_version")
    url = _source_url(source["url"], name, source_version)
    sha256 = _string(source["sha256"], f"{context}.sha256")
    if not _SHA256.fullmatch(sha256):
        raise ValueError(f"{name} sha256 must be 64 lowercase hexadecimal characters")
    archive_root = _string(source["archive_root"], f"{context}.archive_root")
    archive_name = Path(urlsplit(url).path).name
    if not archive_name.endswith(".tar.xz") or archive_root != archive_name[:-7]:
        raise ValueError(f"{name} archive_root must match its .tar.xz filename")
    return SourceSpec(name, source_version, url, sha256, archive_root)


def _source_url(value: Any, name: str, version: str) -> str:
    url = _string(value, f"{name}.url")
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise ValueError(f"{name} source URL must use HTTPS")
    if parsed.netloc != "download.qt.io" or "/official_releases/" not in parsed.path:
        raise ValueError(f"{name} source URL must use official Qt releases")
    if version not in parsed.path:
        raise ValueError(f"{name} source URL must contain its pinned version")
    return url


def _pinned_version(value: Any, context: str) -> str:
    version = _string(value, context)
    if not _EXACT_VERSION.fullmatch(version):
        raise ValueError(f"{context} must be an exact pinned version")
    return version


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} must be a non-empty string")
    return value


def _mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{context} must be a table")
    return value


def _require_keys(
    value: Mapping[str, Any], expected: frozenset[str], context: str
) -> None:
    if set(value) != expected:
        raise ValueError(f"{context} keys must be exactly {expected!r}")
