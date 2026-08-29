from __future__ import annotations

import threading
from fractions import Fraction

import pytest
from PIL import Image
from PySide6.QtCore import QSize, QSizeF
from PySide6.QtGui import QImage

from rembggui.core.errors import AppError, ErrorCode
from rembggui.jobs.cache import PixmapCache, ThumbnailCacheKey, ThumbnailDiskCache
from rembggui.jobs.thumbnails import (
    ThumbnailRequest,
    filmstrip_timestamps,
    generate_thumbnail,
)
from tests.fixtures.media_factory import make_video


def test_thumbnail_worker_returns_only_target_scaled_qimage(tmp_path):
    path = make_video(
        tmp_path / "source.mp4",
        [Image.new("RGB", (320, 180), "red")],
        Fraction(1),
    )
    request = ThumbnailRequest(
        "s1", Fraction(0), QSize(100, 60), 2.0, generation=3
    )

    result = generate_thumbnail(path, request)

    assert isinstance(result.image, QImage)
    assert result.image.size() == QSize(200, 120)
    assert result.image.width() < 320
    assert result.request == request


def test_thumbnail_request_copies_and_normalizes_logical_size():
    mutable = QSizeF(100.25, 60.5)
    request = ThumbnailRequest("s1", Fraction(1, 3), mutable, 1.5, generation=2)
    mutable.setWidth(999)

    assert request.logical_size == (100.25, 60.5)
    assert request.physical_size == QSize(150, 91)


@pytest.mark.parametrize(
    ("width", "expected_count"),
    [(1, 12), (1080, 12), (1800, 20), (9000, 48)],
)
def test_filmstrip_samples_are_even_and_bounded(width, expected_count):
    samples = filmstrip_timestamps(Fraction(10), width)

    assert len(samples) == expected_count
    assert samples[0] == Fraction(0)
    assert samples[-1] == Fraction(10)
    assert len(set(b - a for a, b in zip(samples, samples[1:]))) == 1


def _key(
    source_id: str = "source-1",
    fingerprint: str = "a" * 64,
    generation: int = 1,
    timestamp: Fraction = Fraction(0),
    size: QSize = QSize(10, 10),
) -> ThumbnailCacheKey:
    return ThumbnailCacheKey(
        source_id=source_id,
        source_fingerprint=fingerprint,
        timestamp=timestamp,
        physical_size=size,
        generation=generation,
    )


def test_disk_cache_uses_actual_bytes_lru_and_removes_corruption(tmp_path):
    cache = ThumbnailDiskCache(directory=tmp_path / "cache", max_bytes=180)
    black = QImage(10, 10, QImage.Format.Format_RGBA8888)
    black.fill(0)
    noisy = QImage(10, 10, QImage.Format.Format_RGBA8888)
    for y in range(10):
        for x in range(10):
            noisy.setPixelColor(x, y, ((x * 17) << 16) | ((y * 19) << 8) | 255)
    first = _key(timestamp=Fraction(0))
    second = _key(timestamp=Fraction(1))

    cache.put(first, black, current_source_id="source-1", current_generation=1)
    cache.put(second, noisy, current_source_id="source-1", current_generation=1)

    assert cache.total_bytes <= 180
    assert cache.get(second, current_source_id="source-1", current_generation=1)
    entry = cache.path_for(second)
    entry.write_bytes(b"not an image")
    assert cache.get(second, current_source_id="source-1", current_generation=1) is None
    assert not entry.exists()


def test_disk_cache_removes_corrupt_entries_during_startup_scan(tmp_path):
    directory = tmp_path / "cache"
    directory.mkdir()
    corrupt = directory / f"{'f' * 64}.png"
    corrupt.write_bytes(b"broken")

    cache = ThumbnailDiskCache(directory=directory)

    assert cache.total_bytes == 0
    assert not corrupt.exists()


def test_disk_cache_promotes_provisional_key_atomically(tmp_path):
    cache = ThumbnailDiskCache(directory=tmp_path / "cache")
    image = QImage(10, 10, QImage.Format.Format_RGBA8888)
    image.fill(0xFF00FFFF)
    provisional = _key(fingerprint="a" * 64)
    complete = _key(fingerprint="b" * 64)
    cache.put(
        provisional,
        image,
        current_source_id="source-1",
        current_generation=1,
    )

    assert cache.promote(provisional, complete)
    assert cache.get(
        provisional, current_source_id="source-1", current_generation=1
    ) is None
    promoted = cache.get(
        complete, current_source_id="source-1", current_generation=1
    )
    assert promoted is not None
    assert promoted.size() == QSize(10, 10)


def test_stale_generation_never_enters_disk_or_pixmap_cache(tmp_path, qapp):
    disk = ThumbnailDiskCache(directory=tmp_path / "cache")
    pixmaps = PixmapCache(max_bytes=399)
    image = QImage(10, 10, QImage.Format.Format_RGBA8888)
    image.fill(0)
    stale = _key(generation=1)

    assert not disk.put(
        stale, image, current_source_id="source-1", current_generation=2
    )
    assert (
        pixmaps.put(
            stale, image, current_source_id="source-1", current_generation=2
        )
        is None
    )
    assert disk.total_bytes == 0
    assert pixmaps.total_bytes == 0

    current_generation = _key(source_id="old-source", generation=2)
    assert not disk.put(
        current_generation,
        image,
        current_source_id="new-source",
        current_generation=2,
    )
    assert (
        pixmaps.put(
            current_generation,
            image,
            current_source_id="new-source",
            current_generation=2,
        )
        is None
    )


def test_pixmap_cache_is_gui_thread_only_and_bounded(qapp):
    cache = PixmapCache(max_bytes=400)
    image = QImage(10, 10, QImage.Format.Format_RGBA8888)
    image.fill(0)
    first = _key(timestamp=Fraction(0))
    second = _key(timestamp=Fraction(1))

    cache.put(first, image, current_source_id="source-1", current_generation=1)
    cache.put(second, image, current_source_id="source-1", current_generation=1)
    assert cache.total_bytes <= 400
    assert cache.get(first, current_source_id="source-1", current_generation=1) is None
    assert cache.get(second, current_source_id="source-1", current_generation=1)

    errors = []

    def background_access():
        try:
            cache.get(
                second, current_source_id="source-1", current_generation=1
            )
        except Exception as error:  # noqa: BLE001 - crosses the test thread
            errors.append(error)

    thread = threading.Thread(target=background_access)
    thread.start()
    thread.join()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)


def test_pixmap_cache_hit_refreshes_lru_order(qapp):
    cache = PixmapCache(max_bytes=800)
    image = QImage(10, 10, QImage.Format.Format_RGBA8888)
    image.fill(0)
    first = _key(timestamp=Fraction(0))
    second = _key(timestamp=Fraction(1))
    third = _key(timestamp=Fraction(2))
    for key in (first, second):
        cache.put(key, image, current_source_id="source-1", current_generation=1)
    assert cache.get(first, current_source_id="source-1", current_generation=1)

    cache.put(third, image, current_source_id="source-1", current_generation=1)

    assert cache.get(second, current_source_id="source-1", current_generation=1) is None
    assert cache.get(first, current_source_id="source-1", current_generation=1)
    assert cache.get(third, current_source_id="source-1", current_generation=1)


@pytest.mark.parametrize(
    ("size", "dpr"),
    [(QSize(0, 1), 1), (QSize(1, 1), 0), (QSize(1, 1), float("nan"))],
)
def test_invalid_thumbnail_requests_are_structured(size, dpr):
    with pytest.raises(AppError) as error:
        ThumbnailRequest("s1", Fraction(0), size, dpr, generation=1)
    assert error.value.code is ErrorCode.INVALID_THUMBNAIL
