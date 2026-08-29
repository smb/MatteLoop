from __future__ import annotations

import struct
from fractions import Fraction
from types import SimpleNamespace

import av
import pytest
from PIL import Image

from rembggui.core.errors import AppError, ErrorCode
from rembggui.jobs.source import (
    DecodedFrame,
    _display_dimensions,
    _rotation_from_display_matrix,
    _rotation_from_metadata,
    decode_frame,
    probe_source,
)
from tests.fixtures.media_factory import make_video


def _solid_frames() -> list[Image.Image]:
    return [
        Image.new("RGB", (16, 8), "red"),
        Image.new("RGB", (16, 8), "lime"),
        Image.new("RGB", (16, 8), "blue"),
    ]


def _make_lossless_oriented_video(path):
    frames = [
        Image.new("RGB", (16, 8), "red"),
        Image.new("RGB", (16, 8), "lime"),
        Image.new("RGB", (16, 8), "blue"),
    ]
    for y in range(8):
        for x in range(8, 16):
            frames[0].putpixel((x, y), (0, 0, 255))
    with av.open(path, "w") as container:
        stream = container.add_stream("libx264rgb", rate=20)
        stream.width = 16
        stream.height = 8
        stream.pix_fmt = "rgb24"
        stream.codec_context.time_base = Fraction(1, 20)
        stream.sample_aspect_ratio = Fraction(2)
        stream.codec_context.sample_aspect_ratio = Fraction(2)
        stream.metadata["rotate"] = "90"
        for index, image in enumerate(frames):
            frame = av.VideoFrame.from_image(image)
            frame.pts = index
            frame.time_base = Fraction(1, 20)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


def _make_seekable_video(path):
    with av.open(path, "w") as container:
        stream = container.add_stream("libx264rgb", rate=20)
        stream.width = 16
        stream.height = 8
        stream.pix_fmt = "rgb24"
        stream.options = {"g": "10", "keyint_min": "10", "sc_threshold": "0"}
        for index in range(120):
            image = Image.new("RGB", (16, 8), (index, 0, 0))
            frame = av.VideoFrame.from_image(image)
            frame.pts = index
            frame.time_base = Fraction(1, 20)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


def test_probe_reports_exact_vfr_stream_metadata_and_ignores_fixture_sidecar(
    tmp_path,
):
    path = make_video(
        tmp_path / "Unicode clip ä.mp4",
        _solid_frames(),
        Fraction(20),
        pts=[Fraction(0), Fraction(1, 20), Fraction(3, 20)],
        rotation=90,
    )

    info = probe_source(path)

    assert info.path == path
    assert (info.width, info.height) == (16, 8)
    assert info.rotation == 0
    assert info.duration == Fraction(1, 5)
    assert info.time_base == Fraction(1, 10240)
    assert info.average_rate == Fraction(15)
    assert info.frame_count == 3
    assert info.pixel_aspect == Fraction(1)


@pytest.mark.parametrize(
    ("timestamp", "actual_pts", "dominant_channel"),
    [
        (Fraction(-1), Fraction(0), 0),
        (Fraction(49, 1000), Fraction(0), 0),
        (Fraction(1, 20), Fraction(1, 20), 1),
        (Fraction(7, 100), Fraction(1, 20), 1),
        (Fraction(3, 20), Fraction(3, 20), 2),
        (Fraction(99), Fraction(3, 20), 2),
    ],
)
def test_vfr_decode_selects_frame_owning_half_open_interval(
    tmp_path, timestamp, actual_pts, dominant_channel
):
    path = make_video(
        tmp_path / "vfr.mp4",
        _solid_frames(),
        Fraction(20),
        pts=[Fraction(0), Fraction(1, 20), Fraction(3, 20)],
    )

    decoded = decode_frame(path, timestamp, request_id=4)

    assert isinstance(decoded, DecodedFrame)
    assert decoded.request_id == 4
    assert decoded.requested_timestamp == timestamp
    assert decoded.actual_pts == actual_pts
    assert decoded.image.mode == "RGBA"
    pixel = decoded.image.getpixel((0, 0))
    assert pixel[3] == 255
    assert max(range(3), key=pixel.__getitem__) == dominant_channel


def test_cfr_decode_uses_exact_boundary_and_preserves_each_request_id(tmp_path):
    path = make_video(tmp_path / "cfr.mp4", _solid_frames(), Fraction(2))

    before = decode_frame(path, Fraction(499, 1000), request_id=40)
    boundary = decode_frame(path, Fraction(1, 2), request_id=41)

    assert (before.actual_pts, before.request_id) == (Fraction(0), 40)
    assert (boundary.actual_pts, boundary.request_id) == (Fraction(1, 2), 41)
    assert max(range(3), key=before.image.getpixel((0, 0)).__getitem__) == 0
    assert max(range(3), key=boundary.image.getpixel((0, 0)).__getitem__) == 1


def test_every_probe_and_decode_owns_and_closes_its_container(tmp_path, monkeypatch):
    path = make_video(
        tmp_path / "close.mp4",
        _solid_frames()[:2],
        Fraction(2),
    )
    import rembggui.jobs.source as source_module

    real_open = source_module.av.open
    containers = []

    class TrackedContainer:
        def __init__(self, container):
            self._container = container
            self.closed = False

        def __getattr__(self, name):
            return getattr(self._container, name)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.closed = True
            self._container.close()

    def tracked_open(*args, **kwargs):
        tracked = TrackedContainer(real_open(*args, **kwargs))
        containers.append(tracked)
        return tracked

    monkeypatch.setattr(source_module.av, "open", tracked_open)

    probe_source(path)
    decode_frame(path, Fraction(0), request_id=1)

    assert len(containers) == 2
    assert containers[0] is not containers[1]
    assert all(container.closed for container in containers)


def test_late_decode_seeks_to_a_nearby_keyframe_instead_of_retaining_whole_video(
    tmp_path, monkeypatch
):
    path = _make_seekable_video(tmp_path / "seekable.mkv")
    import rembggui.jobs.source as source_module

    real_open = source_module.av.open
    yielded = 0

    class CountingContainer:
        def __init__(self, container):
            self._container = container

        def __getattr__(self, name):
            return getattr(self._container, name)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self._container.close()

        def decode(self, stream):
            nonlocal yielded
            for frame in self._container.decode(stream):
                yielded += 1
                yield frame

    monkeypatch.setattr(
        source_module.av,
        "open",
        lambda *args, **kwargs: CountingContainer(real_open(*args, **kwargs)),
    )

    decoded = decode_frame(path, Fraction(101, 20), request_id=22)

    assert decoded.actual_pts == Fraction(101, 20)
    assert yielded < 30


def test_rotation_metadata_parser_accepts_only_quarter_turns():
    assert _rotation_from_metadata({"rotate": "-90"}) == 90
    assert _rotation_from_metadata({"rotate": "450.0"}) == 270
    assert _rotation_from_metadata({"rotate": "12"}) == 0
    assert _rotation_from_metadata({}) == 0


def test_display_matrix_boundary_uses_ffmpeg_clockwise_sign():
    class DisplayMatrix:
        type = SimpleNamespace(name="DISPLAYMATRIX")

        def __bytes__(self):
            return struct.pack(
                "=9i", 0, 65536, 0, -65536, 0, 0, 0, 0, 1 << 30
            )

    frame = SimpleNamespace(side_data=[DisplayMatrix()])

    assert _rotation_from_display_matrix(frame) == 270


def test_display_envelope_allows_landscape_or_portrait_uHD_after_orientation():
    assert _display_dimensions(3840, 2160, Fraction(1), 0) == (3840, 2160)
    assert _display_dimensions(3840, 2160, Fraction(1), 90) == (2160, 3840)


def test_real_metadata_rotation_pixel_aspect_and_lossless_rgba_are_normalized(
    tmp_path,
):
    path = _make_lossless_oriented_video(tmp_path / "display.mkv")

    info = probe_source(path)
    first = decode_frame(path, Fraction(0), request_id=1)
    green = decode_frame(path, Fraction(7, 100), request_id=2)

    assert info.rotation == 270
    assert info.pixel_aspect == Fraction(2)
    assert (info.width, info.height) == (8, 32)
    assert first.image.size == (8, 32)
    assert first.image.getpixel((0, 0))[0] > 250
    assert first.image.getpixel((0, 31))[2] > 250
    assert green.actual_pts == Fraction(1, 20)
    assert green.image.getpixel((0, 0)) == (0, 255, 0, 255)


def test_probe_rejects_non_local_and_audio_only_sources(tmp_path):
    with pytest.raises(AppError) as network_error:
        probe_source("https://example.invalid/video.mp4")
    assert network_error.value.code is ErrorCode.SOURCE_NOT_LOCAL
    assert network_error.value.retry_action == "choose-local-file"

    audio_path = tmp_path / "audio-only.wav"
    with av.open(audio_path, "w") as container:
        stream = container.add_stream("pcm_s16le", rate=8000)
        frame = av.AudioFrame(format="s16", layout="mono", samples=800)
        frame.sample_rate = 8000
        frame.pts = 0
        frame.time_base = Fraction(1, 8000)
        frame.planes[0].update(bytes(1600))
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)

    with pytest.raises(AppError) as audio_error:
        probe_source(audio_path)
    assert audio_error.value.code is ErrorCode.SOURCE_NO_VIDEO


def test_probe_rejects_corrupt_high_depth_hdr_and_over_limit_sources(tmp_path):
    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(b"not media")
    with pytest.raises(AppError) as corrupt_error:
        probe_source(corrupt)
    assert corrupt_error.value.code is ErrorCode.SOURCE_CORRUPT

    high_depth = tmp_path / "ten-bit.mkv"
    with av.open(high_depth, "w") as container:
        stream = container.add_stream("ffv1", rate=1)
        stream.width = 16
        stream.height = 8
        stream.pix_fmt = "yuv420p10le"
        frame = av.VideoFrame.from_image(Image.new("RGB", (16, 8), "red"))
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    with pytest.raises(AppError) as depth_error:
        probe_source(high_depth)
    assert depth_error.value.code is ErrorCode.SOURCE_HDR_UNSUPPORTED
    assert depth_error.value.retry_action == "convert-source-to-8bit-srgb"

    hdr = tmp_path / "hdr.mkv"
    with av.open(hdr, "w") as container:
        stream = container.add_stream("ffv1", rate=1)
        stream.width = 16
        stream.height = 8
        stream.pix_fmt = "yuv420p"
        stream.codec_context.color_trc = 16
        stream.codec_context.color_primaries = 9
        frame = av.VideoFrame.from_image(Image.new("RGB", (16, 8), "red"))
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    with pytest.raises(AppError) as hdr_error:
        probe_source(hdr)
    assert hdr_error.value.code is ErrorCode.SOURCE_HDR_UNSUPPORTED

    high_fps = make_video(
        tmp_path / "61fps.mp4", _solid_frames()[:2], Fraction(61)
    )
    with pytest.raises(AppError) as fps_error:
        probe_source(high_fps)
    assert fps_error.value.code is ErrorCode.SOURCE_FPS_UNSUPPORTED

    long_video = make_video(
        tmp_path / "long.mp4",
        _solid_frames()[:2],
        Fraction(1),
        pts=[Fraction(0), Fraction(601)],
    )
    with pytest.raises(AppError) as duration_error:
        probe_source(long_video)
    assert duration_error.value.code is ErrorCode.SOURCE_DURATION_UNSUPPORTED


def test_decode_cancellation_is_checked_between_frames(tmp_path):
    path = make_video(
        tmp_path / "cancel.mp4",
        _solid_frames(),
        Fraction(20),
        pts=[Fraction(0), Fraction(1, 20), Fraction(3, 20)],
    )
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(AppError) as error:
        decode_frame(path, Fraction(3, 20), request_id=8, is_cancelled=cancelled)

    assert error.value.code is ErrorCode.JOB_CANCELLED
    assert checks == 3
