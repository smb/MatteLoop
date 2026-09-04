from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from matteloop.jobs.models.catalog import ModelCatalog
from matteloop.ui.aligned_rows import ROW_DATA_ROLE, AlignedColumn, AlignedRow
from matteloop.ui.source_presentation import format_source_file_size

MODEL_ENTRY_ROLE = ROW_DATA_ROLE + 11


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """One catalog model plus the cache facts shown by the manager."""

    model_id: str
    display_name: str
    download_size_bytes: int
    artifact_path: Path
    disk_size_bytes: int | None
    active: bool
    outdated_size_bytes: int | None = None
    outdated_rembg_version: str | None = None

    @property
    def cached(self) -> bool:
        return self.disk_size_bytes is not None


def present_model(entry: ModelEntry) -> AlignedRow:
    """Present one model with aligned metadata and spoken status words."""
    if entry.cached:
        cache_status = "cached locally"
        cache_detail = cache_status
        if entry.outdated_rembg_version is not None:
            cache_detail += "; outdated copy from rembg " + entry.outdated_rembg_version
        glyph = "◆" if entry.active else "✓"
    elif entry.outdated_size_bytes is not None:
        cache_status = "outdated weight"
        version = entry.outdated_rembg_version or "obsolete rembg"
        cache_detail = (
            f"{cache_status} from rembg {version}; "
            f"{format_source_file_size(entry.outdated_size_bytes)} on disk"
        )
        glyph = "⟳"
    else:
        cache_status = "not cached"
        cache_detail = cache_status
        glyph = "◆" if entry.active else "↓"
    active_status = "active model" if entry.active else "not active"
    size = format_source_file_size(entry.download_size_bytes)
    detail = f"{entry.display_name}; {size}; {cache_detail}; {active_status}"
    return AlignedRow(
        glyph,
        "cached" if entry.cached else "uncached",
        (
            AlignedColumn(entry.display_name),
            AlignedColumn(size, True),
            AlignedColumn(cache_status),
            AlignedColumn(active_status),
        ),
        detail,
    )


def manager_active_id() -> str | None:
    """Default active-model callback used by a read-only fallback dialog."""
    return None


def _model_entry(
    catalog: ModelCatalog,
    cache_root: Path,
    model_id: str,
    active_id: str | None,
) -> ModelEntry:
    spec = catalog.get(model_id)
    artifact = spec.artifact
    if artifact is None:
        raise ValueError(f"V1 model {model_id!r} has no artifact")
    artifact_path = (
        cache_root / catalog.rembg_version / model_id / artifact.runtime_filename
    )
    disk_size_bytes = _regular_file_size(artifact_path)
    outdated_size_bytes: int | None = None
    outdated_rembg_version: str | None = None
    for version in catalog.obsolete_rembg_versions:
        outdated_path = cache_root / version / model_id / artifact.runtime_filename
        outdated_size_bytes = _regular_file_size(outdated_path)
        if outdated_size_bytes is not None:
            outdated_rembg_version = version
            break
    return ModelEntry(
        model_id=model_id,
        display_name=spec.display_name,
        download_size_bytes=artifact.size_bytes,
        artifact_path=artifact_path,
        disk_size_bytes=disk_size_bytes,
        active=model_id == active_id,
        outdated_size_bytes=outdated_size_bytes,
        outdated_rembg_version=outdated_rembg_version,
    )


def _obsolete_directory_size(cache_root: Path, versions: tuple[str, ...]) -> int:
    return sum(_directory_size(cache_root / version) for version in versions)


def _directory_size(path: Path) -> int:
    try:
        info = path.lstat()
    except OSError:
        return 0
    if not stat.S_ISDIR(info.st_mode):
        return 0
    total = 0
    for root, _directories, filenames in os.walk(path, followlinks=False):
        for filename in filenames:
            total += _regular_file_size(Path(root) / filename) or 0
    return total


def _regular_file_size(path: Path) -> int | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None
