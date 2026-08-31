"""Pinned Qt/PySide corresponding-source companion construction."""

from __future__ import annotations

import gzip
import hashlib
import importlib.metadata
import io
import json
import os
import re
import tarfile
import tomllib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

if __package__:
    from scripts.media_stack.manifest import SourceSpec
    from scripts.media_stack.sources import ensure_source
else:
    from media_stack.manifest import SourceSpec
    from media_stack.sources import ensure_source

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
_RECIPE_REVISION = 1
_PROJECT_EVIDENCE = (
    "packaging/entrypoint.py",
    "packaging/pysidedeploy.spec",
    "packaging/smoke_child.py",
    "pyproject.toml",
    "scripts/build.py",
    "scripts/qt_source.py",
    "uv.lock",
)
_LEGAL_EVIDENCE = {
    "legal/GPL-3.0.txt": "legal/GPL-3.0.txt",
    "legal/LGPL-3.0.txt": "legal/LGPL-3.0.txt",
    "legal/QT-PYSIDE-LGPL-NOTICE.md": "legal/QT-PYSIDE-LGPL-NOTICE.md",
    "legal/RELINK.md": "legal/RELINK.md",
    "legal/patches/README.md": "patches/README.md",
}
_INVENTORY_FIELDS = frozenset(("files", "version", "wheel_tags"))

SourceEnsurer = Callable[[SourceSpec, Path], Path]


@dataclass(frozen=True, slots=True)
class QtSourceManifest:
    schema_version: int
    qt_version: str
    distributions: Mapping[str, str]
    sources: tuple[SourceSpec, ...]


@dataclass(frozen=True, slots=True)
class QtSourceCompanion:
    archive: Path
    checksum: Path
    identity: str


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


def installed_qt_distribution_inventory() -> dict[str, dict[str, object]]:
    """Return exact wheel metadata and installed-file inventory for Qt/PySide."""
    inventory: dict[str, dict[str, object]] = {}
    for name in sorted(_DISTRIBUTIONS):
        distribution = importlib.metadata.distribution(name)
        wheel = distribution.read_text("WHEEL")
        if wheel is None:
            raise ValueError(f"{name} distribution has no WHEEL metadata")
        tags = Parser().parsestr(wheel).get_all("Tag", [])
        files = sorted(str(path) for path in distribution.files or ())
        inventory[name] = {
            "files": files,
            "version": distribution.version,
            "wheel_tags": sorted(tags),
        }
    return inventory


def ensure_qt_source_companion(
    root: Path,
    cache_root: Path,
    package_inventory: Mapping[str, Mapping[str, object]],
    *,
    source_ensurer: SourceEnsurer = ensure_source,
) -> QtSourceCompanion:
    """Return a checksum-verified deterministic Qt/PySide source companion."""
    manifest_path = root / "packaging/qt-source/manifest.toml"
    manifest = load_qt_source_manifest(manifest_path)
    inventory = _normalize_inventory(manifest, package_inventory)
    evidence = _companion_evidence(root, manifest_path, manifest, inventory)
    versions = {name: item["version"] for name, item in inventory.items()}
    identity = qt_source_identity(
        manifest_path, versions, evidence, recipe_revision=_RECIPE_REVISION
    )
    output_dir = cache_root / identity
    filename = f"MatteLoop-qt-sources-{manifest.qt_version}-{identity}.tar.gz"
    archive = output_dir / filename
    checksum = archive.with_name(f"{archive.name}.sha256")
    result = QtSourceCompanion(archive, checksum, identity)
    if _valid_companion(result):
        return result

    output_dir.mkdir(parents=True, exist_ok=True)
    sources = {
        f"sources/{Path(urlsplit(source.url).path).name}": source_ensurer(
            source, output_dir / "sources"
        )
        for source in manifest.sources
    }
    _write_companion(result, evidence, sources)
    if not validate_qt_source_companion(result):
        raise RuntimeError("created Qt source companion failed checksum validation")
    return result


def validate_qt_source_companion(companion: QtSourceCompanion) -> bool:
    """Return whether a companion has its identity-bound name and checksum."""
    expected = f"MatteLoop-qt-sources-6.10.3-{companion.identity}.tar.gz"
    return companion.archive.name == expected and _valid_companion(companion)


def _normalize_inventory(
    manifest: QtSourceManifest,
    value: Mapping[str, Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    if set(value) != set(manifest.distributions):
        raise ValueError("package inventory names must match Qt distributions")
    normalized: dict[str, dict[str, object]] = {}
    for name in sorted(value):
        raw = value[name]
        _require_keys(raw, _INVENTORY_FIELDS, f"{name} inventory")
        version = _string(raw["version"], f"{name} inventory version")
        if version != manifest.distributions[name]:
            raise ValueError(f"{name} inventory version must match manifest")
        normalized[name] = {
            "files": _string_list(raw["files"], f"{name} inventory files"),
            "version": version,
            "wheel_tags": _string_list(
                raw["wheel_tags"], f"{name} inventory wheel_tags"
            ),
        }
    return normalized


def _companion_evidence(
    root: Path,
    manifest_path: Path,
    manifest: QtSourceManifest,
    inventory: Mapping[str, Mapping[str, object]],
) -> dict[str, bytes]:
    evidence = {
        "component-inventory.json": _canonical_json(_component_inventory()),
        "manifest.toml": manifest_path.read_bytes(),
        "package-inventory.json": _canonical_json(inventory),
        "source-checksums.json": _canonical_json(
            [
                {
                    "archive_root": source.archive_root,
                    "filename": Path(urlsplit(source.url).path).name,
                    "name": source.name,
                    "sha256": source.sha256,
                    "url": source.url,
                    "version": source.version,
                }
                for source in manifest.sources
            ]
        ),
    }
    for source, destination in _LEGAL_EVIDENCE.items():
        evidence[destination] = _required_bytes(root, source)
    for relative in _PROJECT_EVIDENCE:
        evidence[f"project/{relative}"] = _required_bytes(root, relative)
    return evidence


def _component_inventory() -> dict[str, object]:
    return {
        "components": {
            "PySide6 bindings": "pyside-setup",
            "Qt Core/DBus/Gui/Network/Widgets": "qtbase",
            "Qt imageformats plugins": "qtimageformats",
            "Qt platform plugins": "qtbase",
            "shiboken6 runtime": "pyside-setup",
        },
        "patches": [],
        "schema_version": 1,
    }


def _required_bytes(root: Path, relative: str) -> bytes:
    path = root / relative
    if not path.is_file():
        raise FileNotFoundError(f"required Qt source evidence is missing: {relative}")
    return path.read_bytes()


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _valid_companion(companion: QtSourceCompanion) -> bool:
    if not companion.archive.is_file() or not companion.checksum.is_file():
        return False
    try:
        expected = (
            f"{_file_digest(companion.archive)}  {companion.archive.name}\n"
        )
        return companion.checksum.read_text(encoding="utf-8") == expected
    except (OSError, UnicodeError):
        return False


def _write_companion(
    companion: QtSourceCompanion,
    evidence: Mapping[str, bytes],
    sources: Mapping[str, Path],
) -> None:
    token = uuid.uuid4().hex
    archive_temporary = companion.archive.with_name(
        f".{companion.archive.name}.{token}.tmp"
    )
    checksum_temporary = companion.checksum.with_name(
        f".{companion.checksum.name}.{token}.tmp"
    )
    try:
        _write_tar_gzip(archive_temporary, evidence, sources)
        checksum_bytes = (
            f"{_file_digest(archive_temporary)}  {companion.archive.name}\n".encode()
        )
        _write_fsynced(checksum_temporary, checksum_bytes)
        os.replace(archive_temporary, companion.archive)
        os.replace(checksum_temporary, companion.checksum)
    except (OSError, tarfile.TarError):
        archive_temporary.unlink(missing_ok=True)
        checksum_temporary.unlink(missing_ok=True)
        raise


def _write_tar_gzip(
    destination: Path,
    evidence: Mapping[str, bytes],
    sources: Mapping[str, Path],
) -> None:
    names = sorted((*evidence, *sources))
    with destination.open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(
                fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
            ) as archive:
                for name in names:
                    if name in evidence:
                        _add_bytes(archive, name, evidence[name])
                    else:
                        _add_path(archive, name, sources[name])
        raw.flush()
        os.fsync(raw.fileno())


def _add_bytes(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    info = _tar_info(name, len(content))
    archive.addfile(info, io.BytesIO(content))


def _add_path(archive: tarfile.TarFile, name: str, source: Path) -> None:
    info = _tar_info(name, source.stat().st_size)
    with source.open("rb") as source_file:
        archive.addfile(info, source_file)


def _tar_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = 0
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _write_fsynced(path: Path, content: bytes) -> None:
    with path.open("xb") as destination:
        destination.write(content)
        destination.flush()
        os.fsync(destination.fileno())


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _string_list(value: object, context: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"{context} must be a list of non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{context} must not contain duplicates")
    return sorted(value)


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
