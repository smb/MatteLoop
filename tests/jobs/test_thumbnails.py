from __future__ import annotations

import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction

import av
import pytest
from PIL import Image
from PySide6.QtCore import QSize, QSizeF
from PySide6.QtGui import QBitmap, QColor, QImage

from rembggui.core.errors import AppError, ErrorCode
from rembggui.jobs.cache import (
    PixmapCache,
    ThumbnailCacheKey,
    ThumbnailDiskCache,
    _pixmap_cost,
)
from rembggui.jobs.source import SourceRevision, probe_source
from rembggui.jobs.thumbnails import (
    MAX_THUMBNAIL_DIMENSION,
    MAX_THUMBNAIL_PIXELS,
    ThumbnailRequest,
    ThumbnailResult,
    filmstrip_timestamps,
    generate_thumbnail,
)
from tests.fixtures.media_factory import make_video


def _make_seekable_vfr_video(path):
    with av.open(path, "w") as container:
        stream = container.add_stream("libx264rgb", rate=20)
        stream.width = 16
        stream.height = 8
        stream.pix_fmt = "rgb24"
        stream.options = {"g": "10", "keyint_min": "10", "sc_threshold": "0"}
        stream.codec_context.color_primaries = 1
        stream.codec_context.color_trc = 13
        stream.codec_context.colorspace = 0
        stream.codec_context.color_range = 2
        for index in range(120):
            frame = av.VideoFrame.from_image(Image.new("RGB", (16, 8), (index, 0, 0)))
            frame.pts = index + int(index >= 60)
            frame.time_base = Fraction(1, 20)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


def test_thumbnail_worker_returns_only_target_scaled_qimage(tmp_path):
    path = make_video(
        tmp_path / "source.mp4",
        [Image.new("RGB", (320, 180), "red")],
        Fraction(1),
    )
    info = probe_source(path)
    request = ThumbnailRequest(
        "s1",
        Fraction(0),
        QSize(100, 60),
        2.0,
        generation=3,
        source_fingerprint="a" * 64,
        source_revision=info.revision,
    )

    result = generate_thumbnail(path, request)

    assert isinstance(result.image, QImage)
    assert result.image.size() == QSize(200, 120)
    assert result.image.width() < 320
    assert result.request == request


def test_filmstrip_reuses_one_proof_and_each_thumbnail_decode_is_keyframe_local(
    tmp_path, monkeypatch
):
    path = _make_seekable_vfr_video(tmp_path / "filmstrip.mkv")
    import rembggui.jobs.source as source_module

    real_open = source_module.av.open
    real_derive = source_module._derive_timeline
    decode_counts = []
    validation_scans = 0

    class CountingContainer:
        def __init__(self, container):
            self._container = container
            self._count = 0
            decode_counts.append(self)

        def __getattr__(self, name):
            return getattr(self._container, name)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self._container.close()

        def decode(self, stream):
            for frame in self._container.decode(stream):
                self._count += 1
                yield frame

    def counted_derive(*args, **kwargs):
        nonlocal validation_scans
        validation_scans += 1
        return real_derive(*args, **kwargs)

    monkeypatch.setattr(
        source_module.av,
        "open",
        lambda *args, **kwargs: CountingContainer(real_open(*args, **kwargs)),
    )
    monkeypatch.setattr(source_module, "_derive_timeline", counted_derive)

    info = probe_source(path)
    mismatched_revision = SourceRevision(
        device=info.revision.device,
        inode=info.revision.inode,
        size=info.revision.size,
        mtime_ns=info.revision.mtime_ns,
        ctime_ns=info.revision.ctime_ns + 1,
    )
    with pytest.raises(AppError) as mismatch_error:
        ThumbnailRequest(
            "filmstrip",
            Fraction(0),
            QSize(10, 5),
            1.0,
            0,
            source_fingerprint="a" * 64,
            source_revision=mismatched_revision,
            validation_proof=info.validation_proof,
        )
    assert mismatch_error.value.code is ErrorCode.INVALID_THUMBNAIL

    for generation, timestamp in enumerate(
        (Fraction(81, 20), Fraction(101, 20), Fraction(111, 20)),
        start=1,
    ):
        request = ThumbnailRequest(
            "filmstrip",
            timestamp,
            QSize(10, 5),
            1.0,
            generation,
            source_fingerprint="a" * 64,
            source_revision=info.revision,
            validation_proof=info.validation_proof,
        )
        generate_thumbnail(path, request)

    assert validation_scans == 1
    assert len(decode_counts) == 4
    assert decode_counts[0]._count >= 120
    assert all(container._count < 30 for container in decode_counts[1:])


def test_thumbnail_request_copies_and_normalizes_logical_size():
    mutable = QSizeF(100.25, 60.5)
    request = ThumbnailRequest(
        "s1",
        Fraction(1, 3),
        mutable,
        1.5,
        generation=2,
        source_fingerprint="a" * 64,
        source_revision=REVISION,
    )
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
    revision: SourceRevision | None = None,
) -> ThumbnailCacheKey:
    return ThumbnailCacheKey(
        source_id=source_id,
        source_fingerprint=fingerprint,
        timestamp=timestamp,
        physical_size=size,
        generation=generation,
        source_revision=revision or REVISION,
    )


REVISION = SourceRevision(1, 2, 100, 3, 4)


def _result(key: ThumbnailCacheKey, image: QImage) -> ThumbnailResult:
    request = ThumbnailRequest(
        key.source_id,
        key.timestamp,
        key.physical_dimensions,
        1.0,
        key.generation,
        source_fingerprint=key.source_fingerprint,
        source_revision=key.source_revision,
    )
    return ThumbnailResult(request, image)


def _current() -> dict[str, object]:
    return {
        "current_source_id": "source-1",
        "current_generation": 1,
        "current_fingerprint": "a" * 64,
        "current_revision": REVISION,
    }


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

    cache.put(first, _result(first, black), **_current())
    cache.put(second, _result(second, noisy), **_current())

    assert cache.total_bytes <= 180
    assert cache.get(second, **_current())
    entry = cache.path_for(second)
    entry.write_bytes(b"not an image")
    assert cache.get(second, **_current()) is None
    assert not entry.exists()


def test_disk_cache_removes_corrupt_entries_during_startup_scan(tmp_path):
    directory = tmp_path / "cache"
    directory.mkdir()
    corrupt = directory / f"{'f' * 64}.png"
    corrupt.write_bytes(b"broken")

    cache = ThumbnailDiskCache(directory=directory)

    assert cache.total_bytes == 0
    assert not corrupt.exists()


def test_disk_cache_cleans_crash_temporaries_at_startup(tmp_path):
    directory = tmp_path / "cache"
    directory.mkdir()
    orphan = directory / ".abandoned.tmp"
    orphan.write_bytes(b"partial png")

    cache = ThumbnailDiskCache(directory=directory)

    assert cache.total_bytes == 0
    assert not orphan.exists()


def test_disk_cache_promotes_provisional_key_atomically(tmp_path):
    cache = ThumbnailDiskCache(directory=tmp_path / "cache")
    image = QImage(10, 10, QImage.Format.Format_RGBA8888)
    image.fill(0xFF00FFFF)
    provisional = _key(fingerprint="a" * 64)
    complete = _key(fingerprint="b" * 64)
    cache.put(
        provisional,
        _result(provisional, image),
        **_current(),
    )

    assert cache.promote(provisional, complete)
    assert cache.get(provisional, **_current()) is None
    promoted = cache.get(
        complete,
        current_source_id="source-1",
        current_generation=1,
        current_fingerprint="b" * 64,
        current_revision=REVISION,
    )
    assert promoted is not None
    assert promoted.size() == QSize(10, 10)


def test_disk_cache_promotion_prefers_valid_complete_collision(tmp_path):
    cache = ThumbnailDiskCache(directory=tmp_path / "cache")
    provisional = _key(fingerprint="a" * 64)
    complete = _key(fingerprint="b" * 64)
    red = QImage(10, 10, QImage.Format.Format_RGBA8888)
    red.fill(QColor("red"))
    blue = QImage(10, 10, QImage.Format.Format_RGBA8888)
    blue.fill(QColor("blue"))
    cache.put(provisional, _result(provisional, red), **_current())
    cache.put(
        complete,
        _result(complete, blue),
        current_source_id="source-1",
        current_generation=1,
        current_fingerprint="b" * 64,
        current_revision=REVISION,
    )

    assert cache.promote(provisional, complete)
    image = cache.get(
        complete,
        current_source_id="source-1",
        current_generation=1,
        current_fingerprint="b" * 64,
        current_revision=REVISION,
    )
    assert image is not None
    assert image.pixelColor(0, 0) == QColor("blue")
    assert not cache.path_for(provisional).exists()


def test_disk_cache_promotion_of_identical_key_is_a_noop(tmp_path):
    cache = ThumbnailDiskCache(directory=tmp_path / "cache")
    key = _key()
    image = QImage(10, 10, QImage.Format.Format_RGBA8888)
    image.fill(QColor("green"))
    cache.put(key, _result(key, image), **_current())

    assert cache.promote(key, key)
    loaded = cache.get(key, **_current())
    assert loaded is not None
    assert loaded.pixelColor(0, 0) == QColor("green")


def test_disk_cache_corrupt_promotion_source_is_removed(tmp_path):
    cache = ThumbnailDiskCache(directory=tmp_path / "cache")
    provisional = _key(fingerprint="a" * 64)
    complete = _key(fingerprint="b" * 64)
    image = QImage(10, 10, QImage.Format.Format_RGBA8888)
    image.fill(0)
    cache.put(provisional, _result(provisional, image), **_current())
    source_path = cache.path_for(provisional)
    source_path.write_bytes(b"corrupt")

    assert not cache.promote(provisional, complete)
    assert not source_path.exists()
    assert not cache.path_for(complete).exists()
    assert cache.total_bytes == 0


def test_disk_cache_missing_source_removes_corrupt_promotion_destination(tmp_path):
    cache = ThumbnailDiskCache(directory=tmp_path / "cache")
    provisional = _key(fingerprint="a" * 64)
    complete = _key(fingerprint="b" * 64)
    destination = cache.path_for(complete)
    destination.write_bytes(b"corrupt collision")

    assert not cache.promote(provisional, complete)
    assert not destination.exists()
    assert cache.total_bytes == 0


def test_disk_cache_replacement_recharges_actual_new_cost(tmp_path):
    cache = ThumbnailDiskCache(directory=tmp_path / "cache")
    key = _key()
    image = QImage(10, 10, QImage.Format.Format_RGBA8888)
    image.fill(0)
    cache.put(key, _result(key, image), **_current())
    for y in range(10):
        for x in range(10):
            image.setPixelColor(x, y, QColor(x * 20, y * 20, (x + y) * 10))

    cache.put(key, _result(key, image), **_current())

    assert cache.total_bytes == cache.path_for(key).stat().st_size


def test_disk_cache_unlink_failure_surfaces_without_losing_accounting(
    tmp_path, monkeypatch
):
    cache = ThumbnailDiskCache(directory=tmp_path / "cache", max_bytes=120)
    first = _key(timestamp=Fraction(0))
    second = _key(timestamp=Fraction(1))
    image = QImage(10, 10, QImage.Format.Format_RGBA8888)
    image.fill(0)
    cache.put(first, _result(first, image), **_current())
    real_unlink = type(cache.path_for(first)).unlink

    def failing_unlink(path, *args, **kwargs):
        if path == cache.path_for(first):
            raise OSError("locked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(cache.path_for(first)), "unlink", failing_unlink)
    with pytest.raises(AppError):
        cache.put(second, _result(second, image), **_current())

    assert cache.path_for(first).exists()
    assert cache.total_bytes == sum(
        path.stat().st_size for path in cache.directory.glob("*.png")
    )


def test_disk_cache_serializes_concurrent_puts_and_gets(tmp_path):
    cache = ThumbnailDiskCache(directory=tmp_path / "cache", max_bytes=10_000)
    image = QImage(10, 10, QImage.Format.Format_RGBA8888)
    image.fill(0)
    keys = [_key(timestamp=Fraction(index)) for index in range(8)]

    def round_trip(key):
        cache.put(key, _result(key, image), **_current())
        loaded = cache.get(key, **_current())
        return loaded is not None and loaded.size() == QSize(10, 10)

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert all(executor.map(round_trip, keys))
    assert cache.total_bytes <= 10_000


def test_disk_cache_removes_atomic_temporary_after_baseexception(tmp_path, monkeypatch):
    cache = ThumbnailDiskCache(directory=tmp_path / "cache")
    key = _key()
    image = QImage(10, 10, QImage.Format.Format_RGBA8888)
    image.fill(0)
    import rembggui.jobs.cache as cache_module

    class FatalWrite(BaseException):
        pass

    monkeypatch.setattr(
        cache_module.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(FatalWrite()),
    )

    with pytest.raises(FatalWrite):
        cache.put(key, _result(key, image), **_current())

    assert tuple(cache.directory.glob("*.tmp")) == ()
    assert cache.total_bytes == 0


def test_stale_generation_never_enters_disk_or_pixmap_cache(tmp_path, qapp):
    disk = ThumbnailDiskCache(directory=tmp_path / "cache")
    pixmaps = PixmapCache(max_bytes=399)
    image = QImage(10, 10, QImage.Format.Format_RGBA8888)
    image.fill(0)
    stale = _key(generation=1)

    assert not disk.put(
        stale,
        _result(stale, image),
        current_source_id="source-1",
        current_generation=2,
        current_fingerprint="a" * 64,
        current_revision=REVISION,
    )
    assert (
        pixmaps.put(
            stale,
            _result(stale, image),
            current_source_id="source-1",
            current_generation=2,
            current_fingerprint="a" * 64,
            current_revision=REVISION,
        )
        is None
    )
    assert disk.total_bytes == 0
    assert pixmaps.total_bytes == 0

    current_generation = _key(source_id="old-source", generation=2)
    assert not disk.put(
        current_generation,
        _result(current_generation, image),
        current_source_id="new-source",
        current_generation=2,
        current_fingerprint="a" * 64,
        current_revision=REVISION,
    )
    assert (
        pixmaps.put(
            current_generation,
            _result(current_generation, image),
            current_source_id="new-source",
            current_generation=2,
            current_fingerprint="a" * 64,
            current_revision=REVISION,
        )
        is None
    )


def test_pixmap_cache_is_gui_thread_only_and_bounded(qapp):
    cache = PixmapCache(max_bytes=400)
    image = QImage(10, 10, QImage.Format.Format_RGBA8888)
    image.fill(0)
    first = _key(timestamp=Fraction(0))
    second = _key(timestamp=Fraction(1))

    cache.put(first, _result(first, image), **_current())
    cache.put(second, _result(second, image), **_current())
    assert cache.total_bytes <= 400
    assert cache.get(first, **_current()) is None
    assert cache.get(second, **_current())

    errors = []

    def background_access():
        try:
            cache.get(second, **_current())
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
        cache.put(key, _result(key, image), **_current())
    assert cache.get(first, **_current())

    cache.put(third, _result(third, image), **_current())

    assert cache.get(second, **_current()) is None
    assert cache.get(first, **_current())
    assert cache.get(third, **_current())


def test_pixmap_cache_returns_defensive_wrappers(qapp):
    cache = PixmapCache()
    key = _key()
    image = QImage(10, 10, QImage.Format.Format_RGBA8888)
    image.fill(QColor("black"))

    returned = cache.put(key, _result(key, image), **_current())
    assert returned is not None
    returned.fill(QColor("red"))
    loaded = cache.get(key, **_current())

    assert loaded is not None
    assert loaded.toImage().pixelColor(0, 0) == QColor("black")


def test_pixmap_cost_accounts_native_monochrome_row_alignment(qapp):
    bitmap = QBitmap(33, 2)
    assert bitmap.depth() == 1
    assert _pixmap_cost(bitmap) == 16


def test_pixmap_cache_rejects_qcore_only_process():
    script = """
from PySide6.QtCore import QCoreApplication
from rembggui.jobs.cache import PixmapCache
app = QCoreApplication([])
try:
    PixmapCache()
except RuntimeError:
    print('rejected')
"""
    environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.stdout.strip() == "rejected"


@pytest.mark.parametrize(
    ("size", "dpr"),
    [(QSize(0, 1), 1), (QSize(1, 1), 0), (QSize(1, 1), float("nan"))],
)
def test_invalid_thumbnail_requests_are_structured(size, dpr):
    with pytest.raises(AppError) as error:
        ThumbnailRequest(
            "s1",
            Fraction(0),
            size,
            dpr,
            generation=1,
            source_fingerprint="a" * 64,
            source_revision=REVISION,
        )
    assert error.value.code is ErrorCode.INVALID_THUMBNAIL


@pytest.mark.parametrize(
    ("size", "dpr"),
    [
        ((MAX_THUMBNAIL_DIMENSION + 1, 1), 1.0),
        (
            (
                MAX_THUMBNAIL_DIMENSION,
                MAX_THUMBNAIL_PIXELS // MAX_THUMBNAIL_DIMENSION + 1,
            ),
            1.0,
        ),
        ((1e308, 1), 1e308),
    ],
)
def test_thumbnail_request_rejects_huge_allocation_before_decode(size, dpr):
    with pytest.raises(AppError) as error:
        ThumbnailRequest(
            "s1",
            Fraction(0),
            size,
            dpr,
            1,
            source_fingerprint="a" * 64,
            source_revision=REVISION,
        )
    assert error.value.code is ErrorCode.INVALID_THUMBNAIL


def test_cache_rejects_pixels_bound_to_another_fingerprint_or_revision(tmp_path, qapp):
    disk = ThumbnailDiskCache(directory=tmp_path / "cache")
    pixmaps = PixmapCache()
    image = QImage(10, 10, QImage.Format.Format_RGBA8888)
    image.fill(0)
    old = _key()
    wrong_fingerprint = _key(fingerprint="b" * 64)
    wrong_revision = _key(revision=SourceRevision(1, 9, 100, 3, 4))

    assert not disk.put(old, _result(wrong_fingerprint, image), **_current())
    assert pixmaps.put(old, _result(wrong_revision, image), **_current()) is None
    assert disk.total_bytes == 0
    assert pixmaps.total_bytes == 0
