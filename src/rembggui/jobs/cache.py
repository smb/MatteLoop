"""Bounded disk-QImage and GUI-owned QPixmap thumbnail LRUs."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from platformdirs import user_cache_dir
from PySide6.QtCore import QCoreApplication, QSize, QThread
from PySide6.QtGui import QImage, QPixmap

from rembggui.core.errors import AppError, ErrorCode

MIB = 1024 * 1024
DEFAULT_DISK_CACHE_BYTES = 256 * MIB
DEFAULT_PIXMAP_CACHE_BYTES = 64 * MIB
THUMBNAIL_PIPELINE_VERSION = "thumbnail-v1"


@dataclass(frozen=True, slots=True, init=False)
class ThumbnailCacheKey:
    """Exact thumbnail identity for either provisional or complete fingerprints."""

    source_id: str
    source_fingerprint: str
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
        pipeline_version: str = THUMBNAIL_PIPELINE_VERSION,
    ) -> None:
        if isinstance(physical_size, QSize):
            dimensions = (physical_size.width(), physical_size.height())
        elif isinstance(physical_size, tuple) and len(physical_size) == 2:
            dimensions = physical_size
        else:
            raise _cache_error("physical_size must be QSize or an integer pair")
        if not isinstance(source_id, str) or not source_id:
            raise _cache_error("source_id must be a non-empty string")
        if not isinstance(source_fingerprint, str) or not source_fingerprint:
            raise _cache_error("source_fingerprint must be a non-empty string")
        if not isinstance(timestamp, Fraction):
            raise _cache_error("timestamp must be a Fraction")
        if (
            not all(
                isinstance(item, int) and not isinstance(item, bool)
                for item in dimensions
            )
            or dimensions[0] <= 0
            or dimensions[1] <= 0
        ):
            raise _cache_error("physical dimensions must be positive integers")
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
            self.pipeline_version,
        )

    def digest(self) -> str:
        payload = {
            "generation": self.generation,
            "height": self.physical_dimensions[1],
            "pipeline_version": self.pipeline_version,
            "source_fingerprint": self.source_fingerprint,
            "source_id": self.source_id,
            "timestamp_denominator": self.timestamp.denominator,
            "timestamp_numerator": self.timestamp.numerator,
            "width": self.physical_dimensions[0],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


class ThumbnailDiskCache:
    """A PNG-on-disk LRU charged by actual encoded file bytes."""

    def __init__(
        self,
        max_bytes: int = DEFAULT_DISK_CACHE_BYTES,
        *,
        directory: Path | None = None,
    ) -> None:
        self.max_bytes = _validated_max_bytes(max_bytes)
        self.directory = (
            Path(user_cache_dir("rembggui")) / "thumbnails"
            if directory is None
            else Path(directory)
        )
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise _cache_error(
                f"cache directory could not be created: {error}"
            ) from error
        self._entries: OrderedDict[str, int] = OrderedDict()
        self._load_entries()
        self._evict()

    @property
    def total_bytes(self) -> int:
        return sum(self._entries.values())

    def path_for(self, key: ThumbnailCacheKey) -> Path:
        return self.directory / f"{key.digest()}.png"

    def get(
        self,
        key: ThumbnailCacheKey,
        *,
        current_source_id: str,
        current_generation: int,
    ) -> QImage | None:
        if not _is_current(key, current_source_id, current_generation):
            return None
        path = self.path_for(key)
        if not path.is_file():
            self._entries.pop(path.name, None)
            return None
        try:
            image = QImage(str(path))
        except (OSError, TypeError, ValueError, RuntimeError, OverflowError):
            self._remove_path(path)
            return None
        if image.isNull() or image.size() != key.physical_size:
            self._remove_path(path)
            return None
        try:
            cost = path.stat().st_size
            os.utime(path, None)
        except OSError:
            self._remove_path(path)
            return None
        self._entries[path.name] = cost
        self._entries.move_to_end(path.name)
        return image

    def put(
        self,
        key: ThumbnailCacheKey,
        image: QImage,
        *,
        current_source_id: str,
        current_generation: int,
    ) -> bool:
        if not _is_current(key, current_source_id, current_generation):
            return False
        if image.isNull() or image.size() != key.physical_size:
            raise _cache_error("disk cache accepts only its exact scaled target image")
        path = self.path_for(key)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}-", suffix=".tmp", dir=self.directory
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            if not image.save(str(temporary), "PNG"):  # type: ignore[call-overload]
                raise OSError("Qt image writer rejected PNG output")
            os.replace(temporary, path)
            cost = path.stat().st_size
        except (OSError, ValueError, RuntimeError) as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise _cache_error(f"thumbnail cache write failed: {error}") from error
        self._entries[path.name] = cost
        self._entries.move_to_end(path.name)
        self._evict()
        return path.is_file()

    def promote(
        self, provisional: ThumbnailCacheKey, complete: ThumbnailCacheKey
    ) -> bool:
        """Atomically re-key one verified provisional entry as a complete key."""
        if (
            provisional.source_id != complete.source_id
            or provisional.timestamp != complete.timestamp
            or provisional.physical_dimensions != complete.physical_dimensions
            or provisional.generation != complete.generation
            or provisional.pipeline_version != complete.pipeline_version
        ):
            raise _cache_error("promotion may change only the source fingerprint")
        source_path = self.path_for(provisional)
        destination = self.path_for(complete)
        if not source_path.is_file():
            return destination.is_file()
        try:
            os.replace(source_path, destination)
            os.utime(destination, None)
            cost = destination.stat().st_size
        except OSError as error:
            raise _cache_error(f"thumbnail cache promotion failed: {error}") from error
        self._entries.pop(source_path.name, None)
        self._entries[destination.name] = cost
        self._entries.move_to_end(destination.name)
        self._evict()
        return destination.is_file()

    def _load_entries(self) -> None:
        found: list[tuple[int, str, int]] = []
        try:
            paths = tuple(self.directory.glob("*.png"))
        except OSError:
            paths = ()
        for path in paths:
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

    def _evict(self) -> None:
        while self.total_bytes > self.max_bytes and self._entries:
            name, _ = self._entries.popitem(last=False)
            try:
                (self.directory / name).unlink(missing_ok=True)
            except OSError:
                pass

    def _remove_path(self, path: Path) -> None:
        self._entries.pop(path.name, None)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


class PixmapCache:
    """GUI-thread-only QPixmap LRU charged by actual pixmap storage depth."""

    def __init__(self, max_bytes: int = DEFAULT_PIXMAP_CACHE_BYTES) -> None:
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
    ) -> QPixmap | None:
        self._require_gui_thread()
        if not _is_current(key, current_source_id, current_generation):
            return None
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry[0]

    def put(
        self,
        key: ThumbnailCacheKey,
        image: QImage,
        *,
        current_source_id: str,
        current_generation: int,
    ) -> QPixmap | None:
        self._require_gui_thread()
        if not _is_current(key, current_source_id, current_generation):
            return None
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
        cost = pixmap.width() * pixmap.height() * max(1, (pixmap.depth() + 7) // 8)
        self._entries[key] = (pixmap, cost)
        self._entries.move_to_end(key)
        self._evict()
        return pixmap if key in self._entries else None

    def _evict(self) -> None:
        while sum(cost for _, cost in self._entries.values()) > self.max_bytes:
            self._entries.popitem(last=False)

    @staticmethod
    def _require_gui_thread() -> None:
        application = QCoreApplication.instance()
        if application is None or QThread.currentThread() != application.thread():
            raise RuntimeError("QPixmap cache access is restricted to the GUI thread")


def _is_current(
    key: ThumbnailCacheKey, current_source_id: str, current_generation: int
) -> bool:
    return (
        key.source_id == current_source_id and key.generation == current_generation
    )


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
