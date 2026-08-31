"""Bounded publication helpers for native corresponding-source pairs."""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PreparedComplianceEvidence:
    archive_temporary: Path
    checksum_temporary: Path
    archive: Path
    checksum: Path


def prepare_compliance_evidence(
    archive: Path, destination: Path, expected_name: str
) -> PreparedComplianceEvidence:
    """Prepare an unpublished archive/checksum pair beside the native app."""
    if archive.name != expected_name:
        raise ValueError(
            f"compliance archive must be named {expected_name}, got {archive.name}"
        )
    destination.mkdir(parents=True, exist_ok=True)
    published = destination / expected_name
    checksum = published.with_name(f"{published.name}.sha256")
    token = uuid.uuid4().hex
    archive_temporary = destination / f".{published.name}.{token}.tmp"
    checksum_temporary = destination / f".{checksum.name}.{token}.tmp"
    try:
        _copy_fsynced(archive, archive_temporary)
        _write_fsynced(
            checksum_temporary,
            f"{_sha256(archive_temporary)}  {published.name}\n".encode(),
        )
    except OSError:
        archive_temporary.unlink(missing_ok=True)
        checksum_temporary.unlink(missing_ok=True)
        raise
    return PreparedComplianceEvidence(
        archive_temporary, checksum_temporary, published, checksum
    )


def publish_compliance_evidence(evidence: PreparedComplianceEvidence) -> None:
    """Replace one pair while preserving the existing pair on partial failure."""
    token = uuid.uuid4().hex
    pairs = (
        (evidence.archive_temporary, evidence.archive),
        (evidence.checksum_temporary, evidence.checksum),
    )
    backups = tuple(
        final.with_name(f".{final.name}.{token}.bak") for _temporary, final in pairs
    )
    moved: list[tuple[Path, Path]] = []
    published: list[Path] = []
    try:
        for (_temporary, final), backup in zip(pairs, backups, strict=True):
            if final.exists():
                os.replace(final, backup)
                moved.append((final, backup))
        for temporary, final in pairs:
            os.replace(temporary, final)
            published.append(final)
    except OSError:
        for final in reversed(published):
            final.unlink(missing_ok=True)
        for final, backup in reversed(moved):
            os.replace(backup, final)
        discard_compliance_evidence(evidence, backups)
        raise
    for backup in backups:
        backup.unlink(missing_ok=True)


def discard_compliance_evidence(
    evidence: PreparedComplianceEvidence, backups: tuple[Path, ...] = ()
) -> None:
    """Remove unpublished temporary and backup files for one pair."""
    for path in (
        evidence.archive_temporary,
        evidence.checksum_temporary,
        *backups,
    ):
        path.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_fsynced(source: Path, destination: Path) -> None:
    with source.open("rb") as source_file, destination.open("xb") as output:
        shutil.copyfileobj(source_file, output)
        output.flush()
        os.fsync(output.fileno())


def _write_fsynced(destination: Path, contents: bytes) -> None:
    with destination.open("xb") as output:
        output.write(contents)
        output.flush()
        os.fsync(output.fileno())
