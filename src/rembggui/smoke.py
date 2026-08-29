"""Offline native-package smoke test across the production media boundaries."""

from __future__ import annotations

import gc
import hashlib
import json
import multiprocessing
import os
import tempfile
from dataclasses import asdict, dataclass
from fractions import Fraction
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path
from typing import Any, cast

import av
import numpy as np
from PIL import Image, ImageDraw

from rembggui.core.webp import encode_lossless_webp, validate_webp
from rembggui.jobs.source import decode_frame, probe_source
from rembggui.smoke_child import spawn_smoke_target

_FRAME_SIZE = (128, 128)
_SPAWN_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class SmokeResult:
    """JSON-safe evidence produced by one complete native smoke run."""

    qt_platform: str
    qt_image_formats: tuple[str, ...]
    video_decoded: bool
    webp_frames: int
    webp_has_alpha: bool
    spawn_start_method: str
    shared_memory_roundtrip: bool
    shared_memory_unlinked: bool
    fake_session_used: bool
    peak_full_res_rgba_owners: int

    def to_primitives(self) -> dict[str, object]:
        return asdict(self)

    @property
    def qt(self) -> bool:
        return bool(self.qt_platform) and {"png", "webp"}.issubset(
            self.qt_image_formats
        )

    @property
    def pyav(self) -> bool:
        return self.video_decoded

    @property
    def webp(self) -> bool:
        return self.webp_frames == 2 and self.webp_has_alpha

    @property
    def spawn(self) -> bool:
        return self.spawn_start_method == "spawn"

    @property
    def shared_memory(self) -> bool:
        return self.shared_memory_roundtrip and self.shared_memory_unlinked

    @property
    def peak_rgba_frames(self) -> int:
        return self.peak_full_res_rgba_owners


@dataclass(slots=True)
class _RgbaOwnershipMeter:
    peak: int = 0

    def observe(self, owners: int) -> None:
        if not isinstance(owners, int) or isinstance(owners, bool) or owners < 0:
            raise RuntimeError("invalid RGBA ownership observation")
        self.peak = max(self.peak, owners)
        if self.peak > 3:
            raise RuntimeError("smoke exceeded three full-resolution RGBA owners")

    def observe_images(self, *images: Image.Image) -> None:
        owners = sum(
            image.mode == "RGBA" and image.size == _FRAME_SIZE for image in images
        )
        self.observe(owners)


def run_smoke(work_dir: Path, use_fake_model: bool = True) -> SmokeResult:
    """Exercise packaged runtime surfaces without weights, network, or CLI codecs."""
    if not isinstance(work_dir, Path):
        raise TypeError("work_dir must be a pathlib.Path")
    if not isinstance(use_fake_model, bool):
        raise TypeError("use_fake_model must be a bool")
    work_dir.mkdir(parents=True, exist_ok=True)
    meter = _RgbaOwnershipMeter()

    with tempfile.TemporaryDirectory(prefix="rembggui-smoke-", dir=work_dir) as raw:
        scratch = Path(raw)
        qt_platform, qt_image_formats = _check_qt_image_runtime()
        video_path = _generate_video_with_pyav(scratch / "source.mp4")
        source_info = probe_source(video_path)
        decoded = decode_frame(
            video_path,
            Fraction(0),
            1,
            expected_revision=source_info.revision,
            validation_proof=source_info.validation_proof,
        )
        meter.observe_images(decoded.image)
        try:
            video_decoded = (
                decoded.image.mode == "RGBA"
                and decoded.image.size == _FRAME_SIZE
                and decoded.source_revision == source_info.revision
            )
        finally:
            decoded.image.close()
        if not video_decoded:
            raise RuntimeError("production source path did not decode an RGBA frame")

        frame_paths = _write_alpha_frames(scratch, meter)
        output_path = scratch / "smoke.webp"
        summary = encode_lossless_webp(
            frame_paths,
            (80, 120),
            output_path,
            rgba_ownership_observer=meter.observe,
        )
        webp = validate_webp(output_path, expected_frames=2, expected_duration_ms=200)
        _reopen_and_check_animation(output_path, meter)
        if summary.frames != 2 or webp.frames != 2 or not webp.has_alpha:
            raise RuntimeError("animated alpha WebP validation failed")

        child = _run_spawn_shared_memory(use_fake_model)

    gc.collect()
    if meter.peak == 0:
        raise RuntimeError("RGBA ownership was not measured")
    return SmokeResult(
        qt_platform=qt_platform,
        qt_image_formats=qt_image_formats,
        video_decoded=video_decoded,
        webp_frames=webp.frames,
        webp_has_alpha=webp.has_alpha,
        spawn_start_method=cast(str, child["start_method"]),
        shared_memory_roundtrip=cast(bool, child["shared_memory_roundtrip"]),
        shared_memory_unlinked=cast(bool, child["shared_memory_unlinked"]),
        fake_session_used=cast(bool, child["fake_session_used"]),
        peak_full_res_rgba_owners=meter.peak,
    )


def _check_qt_image_runtime() -> tuple[str, tuple[str, ...]]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QGuiApplication, QImage, QImageWriter
    from PySide6.QtWidgets import QApplication

    existing = QGuiApplication.instance()
    application = existing or QApplication(["rembggui-smoke"])
    created = existing is None
    try:
        platform = QGuiApplication.platformName()
        if not platform:
            raise RuntimeError("Qt did not initialize a platform plugin")
        formats = tuple(
            sorted(
                bytes(value.data()).decode("ascii").lower()
                for value in QImageWriter.supportedImageFormats()
            )
        )
        if not {"png", "webp"}.issubset(formats):
            raise RuntimeError("Qt PNG/WebP image plugins are unavailable")
        image = QImage(4, 4, QImage.Format.Format_RGBA8888)
        image.fill(0x2A4C6E80)
        for format_name in ("PNG", "WEBP"):
            # PySide6 6.10's runtime binding accepts ``str`` here even though
            # its generated type stubs currently advertise bytes-like values.
            format_token = cast(Any, format_name)
            buffer = QBuffer()
            try:
                if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
                    raise RuntimeError("Qt image buffer could not be opened")
                if not image.save(buffer, format_token):
                    raise RuntimeError(f"Qt could not encode {format_name} image data")
                decoded = QImage.fromData(QByteArray(buffer.data()), format_token)
                if decoded.isNull() or decoded.size() != image.size():
                    raise RuntimeError(f"Qt could not decode {format_name} image data")
                if decoded.pixelColor(0, 0).alpha() != image.pixelColor(0, 0).alpha():
                    raise RuntimeError(f"Qt {format_name} image path lost alpha")
            finally:
                buffer.close()
        return platform, formats
    finally:
        if created:
            application.quit()
            application.processEvents()
            del application
            gc.collect()


def _generate_video_with_pyav(path: Path) -> Path:
    time_base = Fraction(1, 2)
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("libx264", rate=2)
        stream.width, stream.height = _FRAME_SIZE
        stream.pix_fmt = "yuv420p"
        stream.codec_context.time_base = time_base
        stream.codec_context.color_primaries = 1
        stream.codec_context.color_trc = 13
        stream.codec_context.colorspace = 1
        stream.codec_context.color_range = 1
        for index in range(2):
            pixels = np.empty((_FRAME_SIZE[1], _FRAME_SIZE[0], 3), dtype=np.uint8)
            pixels[:, :, 0] = 40 + index * 120
            pixels[:, :, 1] = 90
            pixels[:, :, 2] = 180 - index * 80
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            frame.pts = index
            frame.time_base = time_base
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


def _write_alpha_frames(scratch: Path, meter: _RgbaOwnershipMeter) -> tuple[Path, Path]:
    paths: list[Path] = []
    for index, alpha in enumerate((64, 192)):
        image = Image.new("RGBA", _FRAME_SIZE, (0, 0, 0, 0))
        try:
            meter.observe_images(image)
            draw = ImageDraw.Draw(image)
            draw.ellipse((16, 16, 112, 112), fill=(240, 80, 30, alpha))
            path = scratch / f"frame-{index}.png"
            image.save(path, format="PNG")
            paths.append(path)
        finally:
            image.close()
    return paths[0], paths[1]


def _reopen_and_check_animation(path: Path, meter: _RgbaOwnershipMeter) -> None:
    with Image.open(path) as image:
        if getattr(image, "n_frames", 1) != 2:
            raise RuntimeError("WebP reopen did not expose two frames")
        for index in range(2):
            image.seek(index)
            rgba = image.convert("RGBA")
            try:
                meter.observe_images(rgba)
                alpha = rgba.getchannel("A")
                try:
                    if alpha.getextrema()[0] == 255:
                        raise RuntimeError("WebP frame lost alpha")
                finally:
                    alpha.close()
            finally:
                rgba.close()


def _run_spawn_shared_memory(use_fake_model: bool) -> dict[str, object]:
    context = multiprocessing.get_context("spawn")
    byte_count = _FRAME_SIZE[0] * _FRAME_SIZE[1] * 4
    segment = SharedMemory(create=True, size=byte_count)
    shared_name = segment.name
    unlinked = False
    receive, send = context.Pipe(duplex=False)
    process = None
    view: memoryview | None = None
    try:
        raw_buffer = segment.buf
        if raw_buffer is None:
            raise RuntimeError("shared-memory buffer is unavailable")
        attached_view = raw_buffer[:byte_count]
        view = attached_view
        attached_view[:] = bytes(range(256)) * (byte_count // 256)
        before = hashlib.sha256(attached_view).hexdigest()
        process = context.Process(
            target=spawn_smoke_target,
            args=(send, shared_name, byte_count, before, use_fake_model),
            name="rembggui-smoke-child",
        )
        process.start()
        send.close()
        if not receive.poll(_SPAWN_TIMEOUT_SECONDS):
            raise RuntimeError("spawned smoke child timed out")
        payload = json.loads(receive.recv_bytes())
        process.join(_SPAWN_TIMEOUT_SECONDS)
        if process.is_alive():
            process.terminate()
            process.join()
            raise RuntimeError("spawned smoke child did not exit")
        if process.exitcode != 0:
            raise RuntimeError(f"spawned smoke child exited with {process.exitcode}")
        if "error" in payload:
            raise RuntimeError(f"spawned smoke child failed: {payload['error']}")
        after = hashlib.sha256(attached_view).hexdigest()
        if payload["input_sha256"] != before or payload["output_sha256"] != after:
            raise RuntimeError("shared-memory roundtrip digest mismatch")
        if use_fake_model and after == before:
            raise RuntimeError("fake session did not transform shared RGBA bytes")
        result = dict(payload)
    finally:
        receive.close()
        send.close()
        if process is not None and process.is_alive():
            process.terminate()
            process.join()
        if view is not None:
            view.release()
        segment.close()
        try:
            segment.unlink()
            unlinked = True
        except FileNotFoundError:
            unlinked = True

    try:
        orphan = SharedMemory(name=shared_name, create=False)
    except FileNotFoundError:
        pass
    else:
        orphan.close()
        orphan.unlink()
        raise RuntimeError("shared memory still exists after unlink")
    result["shared_memory_unlinked"] = unlinked
    return result
