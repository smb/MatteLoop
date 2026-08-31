"""Serialized disk-QImage and GUI-owned QPixmap thumbnail LRUs."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from threading import RLock

from PySide6.QtCore import QSize, QThread
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication

from matteloop.core.errors import AppError, ErrorCode
from matteloop.jobs.source import SourceRevision
from matteloop.jobs.thumbnails import ThumbnailResult
from matteloop.paths import cache_subdirectory

MIB = 1024 * 1024
DEFAULT_DISK_CACHE_BYTES = 256 * MIB
DEFAULT_PIXMAP_CACHE_BYTES = 64 * MIB
THUMBNAIL_PIPELINE_VERSION = "thumbnail-v2"


@dataclass(frozen=True, slots=True, init=False)
class ThumbnailCacheKey:
    """Exact scaled-image identity, including its bound file revision."""

    source_id: str
    source_fingerprint: str
    source_revision: SourceRevision
    timestamp: Fraction
    physical_dimensions: tuple[int, int]
    generation: int
    pipeline_version: str

    def __init__(
        self,
        source_id: str,
        source_fingerprint: str,
        timestamp: Fraction,
        physical_size: QSize | tuple[int, int],
        generation: int,
        source_revision: SourceRevision,
        pipeline_version: str = THUMBNAIL_PIPELINE_VERSION,
    ) -> None:
        dimensions = _dimensions(physical_size)
        if not isinstance(source_id, str) or not source_id:
            raise _cache_error("source_id must be a non-empty string")
        if not isinstance(source_fingerprint, str) or not source_fingerprint:
            raise _cache_error("source_fingerprint must be a non-empty string")
        if not isinstance(source_revision, SourceRevision):
            raise _cache_error("source_revision must be a SourceRevision")
        if not isinstance(timestamp, Fraction) or timestamp < 0:
            raise _cache_error("timestamp must be a non-negative Fraction")
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
        ):
            raise _cache_error("generation must be a non-negative integer")
        if not isinstance(pipeline_version, str) or not pipeline_version:
            raise _cache_error("pipeline_version must be a non-empty string")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "source_fingerprint", source_fingerprint)
        object.__setattr__(self, "source_revision", source_revision)
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "physical_dimensions", dimensions)
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "pipeline_version", pipeline_version)

    @property
    def physical_size(self) -> QSize:
        return QSize(*self.physical_dimensions)

    def with_fingerprint(self, fingerprint: str) -> ThumbnailCacheKey:
        return ThumbnailCacheKey(
            self.source_id,
            fingerprint,
            self.timestamp,
            self.physical_dimensions,
            self.generation,
            self.source_revision,
            self.pipeline_version,
        )

    def digest(self) -> str:
        revision = self.source_revision
        payload = {
            "generation": self.generation,
            "height": self.physical_dimensions[1],
            "pipeline_version": self.pipeline_version,
            "revision": [
                revision.device,
                revision.inode,
                revision.size,
                revision.mtime_ns,
                revision.ctime_ns,
            ],
            "source_fingerprint": self.source_fingerprint,
            "source_id": self.source_id,
            "timestamp_denominator": self.timestamp.denominator,
            "timestamp_numerator": self.timestamp.numerator,
            "width": self.physical_dimensions[0],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


class ThumbnailDiskCache:
    """A lock-serialized PNG LRU charged by actual filesystem bytes."""

    def __init__(
        self,
        max_bytes: int = DEFAULT_DISK_CACHE_BYTES,
        *,
        directory: Path | None = None,
    ) -> None:
        self.max_bytes = _validated_max_bytes(max_bytes)
        self.directory = (
            cache_subdirectory("thumbnails")
            if directory is None
            else Path(directory)
        )
        self._lock = RLock()
        self._entries: OrderedDict[str, int] = OrderedDict()
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise _cache_error(
                f"cache directory could not be created: {error}"
            ) from error
        with self._lock:
            self._clean_orphan_temporaries()
            self._load_entries()
            self._evict()

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return sum(self._entries.values())

    def path_for(self, key: ThumbnailCacheKey) -> Path:
        return self.directory / f"{key.digest()}.png"

    def get(
        self,
        key: ThumbnailCacheKey,
        *,
        current_source_id: str,
        current_generation: int,
        current_fingerprint: str,
        current_revision: SourceRevision,
    ) -> QImage | None:
        with self._lock:
            if not _is_current(
                key,
                current_source_id,
                current_generation,
                current_fingerprint,
                current_revision,
            ):
                return None
            path = self.path_for(key)
            if not path.is_file():
                self._entries.pop(path.name, None)
                return None
            image = self._load_valid_image(path, key)
            if image is None:
                self._remove_path(path)
                return None
            try:
                cost = path.stat().st_size
                os.utime(path, None)
            except OSError as error:
                raise _cache_error(
                    f"cache hit metadata update failed: {error}"
                ) from error
            self._entries[path.name] = cost
            self._entries.move_to_end(path.name)
            self._evict()
            return image

    def put(
        self,
        key: ThumbnailCacheKey,
        result: ThumbnailResult,
        *,
        current_source_id: str,
        current_generation: int,
        current_fingerprint: str,
        current_revision: SourceRevision,
    ) -> bool:
        with self._lock:
            if not _result_is_current(
                key,
                result,
                current_source_id,
                current_generation,
                current_fingerprint,
                current_revision,
            ):
                return False
            image = result.image
            if image.isNull() or image.size() != key.physical_size:
                raise _cache_error(
                    "disk cache accepts only its exact scaled target image"
                )
            path = self.path_for(key)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.stem}-", suffix=".tmp", dir=self.directory
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                if not image.save(
                    str(temporary),
                    "PNG",  # type: ignore[call-overload]
                ):
                    raise OSError("Qt image writer rejected PNG output")
                cost = temporary.stat().st_size
                os.replace(temporary, path)
            except BaseException as error:
                self._cleanup_temporary(temporary)
                if isinstance(error, (OSError, TypeError, ValueError, RuntimeError)):
                    raise _cache_error(
                        f"thumbnail cache write failed: {error}"
                    ) from error
                raise
            self._entries[path.name] = cost
            self._entries.move_to_end(path.name)
            self._evict()
            return path.is_file()

    def promote(
        self, provisional: ThumbnailCacheKey, complete: ThumbnailCacheKey
    ) -> bool:
        """Promote only a fingerprint while retaining revision and target."""
        with self._lock:
            if not _promotion_compatible(provisional, complete):
                raise _cache_error("promotion may change only the source fingerprint")
            source_path = self.path_for(provisional)
            destination = self.path_for(complete)
            if source_path == destination:
                image = self._load_valid_image(source_path, provisional)
                if image is None:
                    self._remove_path(source_path)
                    return False
                cost = self._file_cost(source_path, "same-key promotion")
                self._entries[source_path.name] = cost
                self._entries.move_to_end(source_path.name)
                self._evict()
                return source_path.is_file()
            if not source_path.is_file():
                if not destination.is_file():
                    return False
                destination_image = self._load_valid_image(destination, complete)
                if destination_image is None:
                    self._remove_path(destination)
                    return False
                cost = self._file_cost(destination, "complete-key promotion")
                self._entries[destination.name] = cost
                self._entries.move_to_end(destination.name)
                self._evict()
                return destination.is_file()
            source_image = self._load_valid_image(source_path, provisional)
            if source_image is None:
                self._remove_path(source_path)
                return False
            destination_image = (
                self._load_valid_image(destination, complete)
                if destination.is_file()
                else None
            )
            if destination_image is not None:
                cost = self._file_cost(destination, "promotion collision")
                self._remove_path(source_path)
            else:
                if destination.exists():
                    self._remove_path(destination)
                cost = self._file_cost(source_path, "provisional promotion")
                try:
                    os.replace(source_path, destination)
                except OSError as error:
                    raise _cache_error(
                        f"thumbnail cache promotion failed: {error}"
                    ) from error
            self._entries.pop(source_path.name, None)
            self._entries[destination.name] = cost
            self._entries.move_to_end(destination.name)
            self._evict()
            return destination.is_file()

    def _clean_orphan_temporaries(self) -> None:
        for path in self.directory.glob("*.tmp"):
            self._unlink(path, "orphan temporary cleanup")

    def _load_entries(self) -> None:
        found: list[tuple[int, str, int]] = []
        for path in self.directory.glob("*.png"):
            try:
                source_stat = path.stat()
                image = QImage(str(path))
            except (OSError, TypeError, ValueError, RuntimeError, OverflowError):
                self._remove_path(path)
                continue
            if image.isNull():
                self._remove_path(path)
                continue
            found.append((source_stat.st_mtime_ns, path.name, source_stat.st_size))
        for _, name, cost in sorted(found):
            self._entries[name] = cost

    def _load_valid_image(self, path: Path, key: ThumbnailCacheKey) -> QImage | None:
        try:
            image = QImage(str(path))
        except (OSError, TypeError, ValueError, RuntimeError, OverflowError):
            return None
        if image.isNull() or image.size() != key.physical_size:
            return None
        return image

    def _evict(self) -> None:
        while sum(self._entries.values()) > self.max_bytes and self._entries:
            name = next(iter(self._entries))
            path = self.directory / name
            self._unlink(path, "LRU eviction")
            self._entries.pop(name)

    def _remove_path(self, path: Path) -> None:
        if path.exists():
            self._unlink(path, "cache entry removal")
        self._entries.pop(path.name, None)

    def _unlink(self, path: Path, operation: str) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            raise _cache_error(
                f"{operation} failed for {path.name}: {error}"
            ) from error

    def _file_cost(self, path: Path, operation: str) -> int:
        try:
            return path.stat().st_size
        except OSError as error:
            raise _cache_error(f"{operation} stat failed: {error}") from error

    def _cleanup_temporary(self, path: Path) -> None:
        if path.exists():
            self._unlink(path, "atomic temporary cleanup")


class PixmapCache:
    """Live-QApplication-only defensive QPixmap LRU."""

    def __init__(self, max_bytes: int = DEFAULT_PIXMAP_CACHE_BYTES) -> None:
        self._require_gui_thread()
        self.max_bytes = _validated_max_bytes(max_bytes)
        self._entries: OrderedDict[ThumbnailCacheKey, tuple[QPixmap, int]] = (
            OrderedDict()
        )

    @property
    def total_bytes(self) -> int:
        self._require_gui_thread()
        return sum(cost for _, cost in self._entries.values())

    def get(
        self,
        key: ThumbnailCacheKey,
        *,
        current_source_id: str,
        current_generation: int,
        current_fingerprint: str,
        current_revision: SourceRevision,
    ) -> QPixmap | None:
        self._require_gui_thread()
        if not _is_current(
            key,
            current_source_id,
            current_generation,
            current_fingerprint,
            current_revision,
        ):
            return None
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return QPixmap(entry[0])

    def put(
        self,
        key: ThumbnailCacheKey,
        result: ThumbnailResult,
        *,
        current_source_id: str,
        current_generation: int,
        current_fingerprint: str,
        current_revision: SourceRevision,
    ) -> QPixmap | None:
        self._require_gui_thread()
        if not _result_is_current(
            key,
            result,
            current_source_id,
            current_generation,
            current_fingerprint,
            current_revision,
        ):
            return None
        image = result.image
        if image.isNull() or image.size() != key.physical_size:
            raise _cache_error(
                "pixmap cache accepts only its exact scaled target image"
            )
        try:
            pixmap = QPixmap.fromImage(image)
        except (TypeError, ValueError, RuntimeError, OverflowError) as error:
            raise _cache_error(f"QPixmap conversion failed: {error}") from error
        if pixmap.isNull():
            raise _cache_error("QPixmap conversion failed")
        stored = QPixmap(pixmap)
        cost = _pixmap_cost(stored)
        self._entries[key] = (stored, cost)
        self._entries.move_to_end(key)
        self._evict()
        return QPixmap(stored) if key in self._entries else None

    def _evict(self) -> None:
        while sum(cost for _, cost in self._entries.values()) > self.max_bytes:
            self._entries.popitem(last=False)

    @staticmethod
    def _require_gui_thread() -> None:
        application = QApplication.instance()
        if not isinstance(application, QApplication):
            raise RuntimeError("QPixmap cache requires a live QApplication")
        try:
            application_thread = application.thread()
        except RuntimeError as error:
            raise RuntimeError("QPixmap cache QApplication was destroyed") from error
        if QThread.currentThread() != application_thread:
            raise RuntimeError("QPixmap cache access is restricted to the GUI thread")


def _dimensions(value: QSize | tuple[int, int]) -> tuple[int, int]:
    dimensions = (value.width(), value.height()) if isinstance(value, QSize) else value
    if (
        not isinstance(dimensions, tuple)
        or len(dimensions) != 2
        or not all(
            isinstance(item, int) and not isinstance(item, bool) for item in dimensions
        )
        or dimensions[0] <= 0
        or dimensions[1] <= 0
    ):
        raise _cache_error("physical dimensions must be positive integers")
    return dimensions


def _result_is_current(
    key: ThumbnailCacheKey,
    result: ThumbnailResult,
    source_id: str,
    generation: int,
    fingerprint: str,
    revision: SourceRevision,
) -> bool:
    request = result.request
    return (
        _is_current(key, source_id, generation, fingerprint, revision)
        and request.source_id == key.source_id
        and request.timestamp == key.timestamp
        and request.physical_dimensions == key.physical_dimensions
        and request.generation == key.generation
        and request.source_fingerprint == key.source_fingerprint
        and request.source_revision == key.source_revision
    )


def _is_current(
    key: ThumbnailCacheKey,
    source_id: str,
    generation: int,
    fingerprint: str,
    revision: SourceRevision,
) -> bool:
    return (
        key.source_id == source_id
        and key.generation == generation
        and key.source_fingerprint == fingerprint
        and key.source_revision == revision
    )


def _promotion_compatible(
    provisional: ThumbnailCacheKey, complete: ThumbnailCacheKey
) -> bool:
    return (
        provisional.source_id == complete.source_id
        and provisional.source_revision == complete.source_revision
        and provisional.timestamp == complete.timestamp
        and provisional.physical_dimensions == complete.physical_dimensions
        and provisional.generation == complete.generation
        and provisional.pipeline_version == complete.pipeline_version
    )


def _pixmap_cost(pixmap: QPixmap) -> int:
    row_bytes = ((pixmap.width() * pixmap.depth() + 31) // 32) * 4
    return row_bytes * pixmap.height()


def _validated_max_bytes(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _cache_error("max_bytes must be a positive integer")
    return value


def _cache_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.INVALID_THUMBNAIL,
        "thumbnail-cache",
        "thumbnail.cache.failed",
        detail,
        "regenerate-thumbnails",
    )
