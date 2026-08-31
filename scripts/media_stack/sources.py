"""Verified source archive staging and safe extraction helpers."""

import hashlib
import os
import tarfile
import urllib.request
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from urllib.parse import urlsplit

from .manifest import SourceSpec

UrlOpener = Callable[[str], AbstractContextManager[BinaryIO]]
_CHUNK_SIZE = 1024 * 1024


def ensure_source(
    source: SourceSpec,
    cache_dir: Path,
    *,
    opener: UrlOpener = urllib.request.urlopen,
) -> Path:
    """Return a cached source archive after checking its pinned SHA-256 digest."""
    if urlsplit(source.url).scheme != "https":
        raise ValueError(f"source URL must use HTTPS: {source.name}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / _archive_name(source)
    if archive.is_file() and _digest(archive) == source.sha256:
        return archive

    partial = archive.with_name(f"{archive.name}.part")
    digest = hashlib.sha256()
    with opener(source.url) as response, partial.open("wb") as destination:
        while chunk := response.read(_CHUNK_SIZE):
            destination.write(chunk)
            digest.update(chunk)
        destination.flush()
        os.fsync(destination.fileno())
    if digest.hexdigest() != source.sha256:
        partial.unlink(missing_ok=True)
        raise ValueError(f"source SHA-256 mismatch: {source.name}")
    partial.replace(archive)
    return archive


def extract_source(archive: Path, destination: Path, expected_root: str) -> Path:
    """Extract a tar archive into an empty directory with one declared root."""
    _prepare_destination(destination)
    with tarfile.open(archive) as source_archive:
        roots = _top_level_roots(source_archive)
        if roots != {expected_root}:
            raise ValueError(
                f"archive root must be exactly {expected_root!r}, got {sorted(roots)!r}"
            )
        _require_directory_root(source_archive, expected_root)
        source_archive.extractall(destination, filter="data")
    extracted_root = destination / expected_root
    if not extracted_root.is_dir() or extracted_root.is_symlink():
        raise ValueError("extracted archive root must be a real directory")
    return extracted_root


def _archive_name(source: SourceSpec) -> str:
    return Path(urlsplit(source.url).path).name


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        while chunk := source_file.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_destination(destination: Path) -> None:
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise ValueError("source destination must be an empty directory")
        return
    destination.mkdir(parents=True)


def _top_level_roots(source_archive: tarfile.TarFile) -> set[str]:
    roots: set[str] = set()
    for member in source_archive.getmembers():
        parts = _member_parts(member)
        if parts:
            roots.add(parts[0])
    return roots


def _require_directory_root(
    source_archive: tarfile.TarFile, expected_root: str
) -> None:
    for member in source_archive.getmembers():
        if _member_parts(member) == (expected_root,) and not member.isdir():
            raise ValueError("archive root entry must be a directory")


def _member_parts(member: tarfile.TarInfo) -> tuple[str, ...]:
    return tuple(
        part for part in PurePosixPath(member.name).parts if part not in (".", "/")
    )
