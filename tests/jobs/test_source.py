from __future__ import annotations

import gc
import json
import os
import struct
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import av
import pytest
from PIL import Image

import matteloop.jobs.source as source_module
from matteloop.core.errors import AppError, ErrorCode
from matteloop.core.rgba import RgbaOwnershipHandle, RgbaOwnershipTracker
from matteloop.jobs.source import (
    MAX_TIMELINE_DECODED_FRAMES,
    DecodedFrame,
    SourceRevision,
    SourceValidationProof,
    _color_profile,
    _decodable_video_stream,
    _derive_timeline,
    _display_dimensions,
    _duration,
    _frame_at_timestamp,
    _normalized_image,
    _rotation_from_display_matrix,
    _rotation_from_metadata,
    _source_info,
    _validate_proof_identity,
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


def _declare_srgb(stream, *, rgb: bool) -> None:
    stream.codec_context.color_primaries = 1
    stream.codec_context.color_trc = 13
    stream.codec_context.colorspace = 0 if rgb else 1
    stream.codec_context.color_range = 2 if rgb else 1


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
        _declare_srgb(stream, rgb=True)
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
        _declare_srgb(stream, rgb=True)
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


def _make_nonzero_start_video(path):
    with av.open(path, "w") as container:
        stream = container.add_stream("libx264rgb", rate=1)
        stream.width = 16
        stream.height = 8
        stream.pix_fmt = "rgb24"
        stream.codec_context.time_base = Fraction(1)
        _declare_srgb(stream, rgb=True)
        for pts, color in zip((10, 11, 12), ("red", "lime", "blue")):
            frame = av.VideoFrame.from_image(Image.new("RGB", (16, 8), color))
            frame.pts = pts
            frame.time_base = Fraction(1)
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
    assert info.sustained_rate == Fraction(30720, 2562)
    assert info.frame_count == 3
    assert info.pixel_aspect == Fraction(1)


@pytest.mark.parametrize(
    ("timestamp", "actual_pts", "dominant_channel"),
    [
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


def test_decode_preserves_dimensions_for_unaligned_yuv_frame(tmp_path) -> None:
    """The padded reformat destination is cropped back to the source size."""
    frames = [Image.new("RGB", (18, 8), color) for color in ("red", "blue")]
    path = make_video(tmp_path / "unaligned.mp4", frames, Fraction(2))

    decoded = decode_frame(path, Fraction(0), request_id=1)

    try:
        assert decoded.image.mode == "RGBA"
        assert decoded.image.size == (18, 8)
    finally:
        decoded.image.close()


def test_decode_tracker_keeps_only_returned_image_live(tmp_path) -> None:
    path = make_video(tmp_path / "tracked.mp4", _solid_frames(), Fraction(2))
    tracker = RgbaOwnershipTracker((16, 8))

    decoded = decode_frame(
        path,
        Fraction(0),
        request_id=9,
        rgba_ownership_tracker=tracker,
    )
    gc.collect()

    assert tracker.peak == 3
    assert tracker.current == 1
    decoded.image.close()
    gc.collect()
    assert tracker.current == 1
    del decoded
    gc.collect()
    assert tracker.current == 0


def test_decode_tracker_observes_externally_retained_real_reformatted_frame(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_video(tmp_path / "retained-rgba.mp4", _solid_frames(), Fraction(2))
    tracker = RgbaOwnershipTracker((16, 8))
    retained: list[av.VideoFrame] = []
    real_reformatter_type = source_module.VideoReformatter

    class RetainingReformatter:
        def __init__(self) -> None:
            self._real = real_reformatter_type()

        def reformat(self, *args: object, **kwargs: object) -> av.VideoFrame:
            reformatted = self._real.reformat(*args, **kwargs)
            retained.append(reformatted)
            return reformatted

    monkeypatch.setattr(source_module, "VideoReformatter", RetainingReformatter)

    decoded = decode_frame(
        path,
        Fraction(0),
        request_id=10,
        rgba_ownership_tracker=tracker,
    )
    gc.collect()

    assert retained[0].format.name == "rgba"
    assert tracker.peak == 3
    assert tracker.current == 2
    decoded.image.close()
    del decoded
    gc.collect()
    assert tracker.current == 1
    retained.clear()
    gc.collect()
    assert tracker.current == 0


def test_decode_rejects_negative_public_timestamps(tmp_path):
    path = make_video(tmp_path / "negative-request.mp4", _solid_frames(), Fraction(2))

    with pytest.raises(AppError) as error:
        decode_frame(path, Fraction(-1), request_id=4)

    assert error.value.code is ErrorCode.SOURCE_CORRUPT


def test_nonzero_raw_pts_are_normalized_to_presentation_origin(tmp_path):
    path = _make_nonzero_start_video(tmp_path / "nonzero.mkv")

    info = probe_source(path)
    first = decode_frame(path, Fraction(0), request_id=1)
    boundary = decode_frame(path, Fraction(1), request_id=2)

    assert info.duration == Fraction(13)
    assert first.actual_pts == Fraction(0)
    assert boundary.actual_pts == Fraction(1)
    assert first.image.getpixel((0, 0)) == (255, 0, 0, 255)
    assert boundary.image.getpixel((0, 0)) == (0, 255, 0, 255)


def test_negative_raw_pts_are_selected_on_nonnegative_presentation_timeline():
    frames = []
    for pts in (-2, -1, 0):
        frame = av.VideoFrame.from_image(Image.new("RGB", (2, 2), "red"))
        frame.pts = pts
        frame.time_base = Fraction(1)
        frames.append(frame)
    stream = _color_stream(matrix=0)

    class Container:
        def decode(self, selected_stream):
            assert selected_stream is stream
            yield from frames

    selected, _ = _frame_at_timestamp(
        Container(), stream, Fraction(1), Fraction(-2), None
    )

    assert Fraction(selected.pts) * selected.time_base - Fraction(-2) == Fraction(1)


def test_duration_metadata_is_a_span_not_an_absolute_end_timestamp():
    stream = SimpleNamespace(duration=1, start_time=None)
    container = SimpleNamespace(duration=None)

    assert _duration(container, stream, Fraction(1), Fraction(-2)) == Fraction(1)

    stream.duration = None
    container.duration = 2 * int(av.time_base)
    assert _duration(container, stream, Fraction(1), Fraction(10)) == Fraction(2)


def test_decode_rejects_stream_with_only_negative_preroll_frames():
    frame = av.VideoFrame.from_image(Image.new("RGB", (2, 2), "red"))
    frame.pts = -1
    frame.time_base = Fraction(1)
    stream = _color_stream(matrix=0)

    class Container:
        def decode(self, selected_stream):
            assert selected_stream is stream
            yield frame

    with pytest.raises(AppError) as error:
        _frame_at_timestamp(Container(), stream, Fraction(0), Fraction(0), None)

    assert error.value.code is ErrorCode.SOURCE_CORRUPT


def test_missing_duration_and_rate_are_derived_from_exact_frame_pts():
    frames = []
    for pts in (0, 1, 3):
        frame = av.VideoFrame.from_image(Image.new("RGB", (2, 2), "red"))
        frame.pts = pts
        frame.time_base = Fraction(1, 10)
        frames.append(frame)
    stream = _color_stream(matrix=0)
    stream.time_base = Fraction(1, 10)

    class Container:
        def seek(self, *args, **kwargs):
            return None

        def decode(self, selected_stream):
            assert selected_stream is stream
            yield from frames

    derived = _derive_timeline(Container(), stream, Fraction(0), None)

    assert derived.duration == Fraction(1, 2)
    assert derived.sustained_rate == Fraction(30, 7)
    assert derived.frame_count == 3


def test_millisecond_quantized_60fps_source_opens_and_proof_verifies(tmp_path):
    gaps = (17, 17, 16) * 599 + (17, 16)
    timestamps = [0]
    for gap in gaps:
        timestamps.append(timestamps[-1] + gap)
    frames = [_yuv_frame(81, 90, 240, matrix=1, color_range=1) for _ in timestamps]
    for frame, pts in zip(frames, timestamps):
        frame.pts = pts
        frame.time_base = Fraction(1, 1000)
    stream = _color_stream(matrix=1)
    stream.index = 0
    stream.width = 1920
    stream.height = 1080
    stream.time_base = Fraction(1, 1000)
    stream.start_time = 0
    stream.duration = 30_000
    stream.average_rate = Fraction(2_700_000, 45_001)
    stream.base_rate = Fraction(60)
    stream.guessed_rate = Fraction(60)
    stream.frames = len(frames)

    class Container:
        duration = None

        def seek(self, *args, **kwargs):
            return None

        def decode(self, selected_stream):
            assert selected_stream is stream
            yield from frames

    info = _source_info(
        tmp_path / "quantized-60fps.mp4",
        SourceRevision(1, 2, 3, 4, 5),
        Container(),
        stream,
        frames[0],
    )

    assert info.sustained_rate == Fraction(1_800_000, 30_001)
    _validate_proof_identity(info.validation_proof, info.revision)


def test_short_millisecond_quantized_60fps_source_opens(tmp_path):
    timestamps = (1, 17, 34, 50)
    frames = [_yuv_frame(81, 90, 240, matrix=1, color_range=1) for _ in timestamps]
    for frame, pts in zip(frames, timestamps):
        frame.pts = pts
        frame.time_base = Fraction(1, 1000)
    stream = _color_stream(matrix=1)
    stream.index = 0
    stream.width = 1920
    stream.height = 1080
    stream.time_base = Fraction(1, 1000)
    stream.start_time = 0
    stream.duration = 66
    stream.average_rate = Fraction(60)
    stream.base_rate = Fraction(60)
    stream.guessed_rate = Fraction(60)
    stream.frames = len(frames)

    class Container:
        duration = None

        def seek(self, *args, **kwargs):
            return None

        def decode(self, selected_stream):
            assert selected_stream is stream
            yield from frames

    _source_info(
        tmp_path / "short-quantized-60fps.mp4",
        SourceRevision(1, 2, 3, 4, 5),
        Container(),
        stream,
        frames[0],
    )


def test_sustained_120fps_source_is_rejected(tmp_path):
    frames = [_yuv_frame(81, 90, 240, matrix=1, color_range=1) for _ in range(3)]
    for frame, pts in zip(frames, range(3)):
        frame.pts = pts
        frame.time_base = Fraction(1, 120)
    stream = _color_stream(matrix=1)
    stream.width = 16
    stream.height = 8
    stream.time_base = Fraction(1, 120)
    stream.start_time = 0
    stream.duration = 3
    stream.average_rate = Fraction(30)
    stream.base_rate = Fraction(120)
    stream.guessed_rate = Fraction(30)
    stream.frames = len(frames)

    class Container:
        duration = None

        def seek(self, *args, **kwargs):
            return None

        def decode(self, selected_stream):
            assert selected_stream is stream
            yield from frames

    with pytest.raises(AppError) as error:
        _source_info(
            tmp_path / "120fps.mp4",
            SourceRevision(1, 2, 3, 4, 5),
            Container(),
            stream,
            frames[0],
        )

    assert error.value.code is ErrorCode.SOURCE_FPS_UNSUPPORTED


def test_vfr_timestamp_scan_uses_sustained_cadence_even_with_declared_rates(
    tmp_path,
):
    frames = [_yuv_frame(81, 90, 240, matrix=1, color_range=1) for _ in range(3)]
    for frame, pts in zip(frames, (0, 1, 10)):
        frame.pts = pts
        frame.time_base = Fraction(1, 100)
    stream = _color_stream(matrix=1)
    stream.width = 16
    stream.height = 8
    stream.time_base = Fraction(1, 100)
    stream.start_time = 0
    stream.duration = 20
    stream.average_rate = Fraction(30)
    stream.guessed_rate = Fraction(30)
    stream.base_rate = Fraction(100)
    stream.frames = 3

    decoded = False

    class Container:
        duration = None

        def seek(self, *args, **kwargs):
            return None

        def decode(self, selected_stream):
            nonlocal decoded
            assert selected_stream is stream
            decoded = True
            yield from frames

    info = _source_info(
        tmp_path / "vfr.mp4",
        SourceRevision(1, 2, 3, 4, 5),
        Container(),
        stream,
        frames[0],
    )

    assert decoded
    assert info.sustained_rate == Fraction(100, 7)


def test_exact_consistent_cfr_metadata_is_the_only_scan_free_path(tmp_path):
    frame = _yuv_frame(81, 90, 240, matrix=1, color_range=1)
    frame.pts = 0
    frame.time_base = Fraction(1, 2)
    stream = _color_stream(matrix=1)
    stream.width = 16
    stream.height = 8
    stream.time_base = Fraction(1, 2)
    stream.start_time = 0
    stream.duration = 3
    stream.average_rate = Fraction(2)
    stream.guessed_rate = Fraction(2)
    stream.base_rate = Fraction(2)
    stream.frames = 3

    class Container:
        duration = None

        def decode(self, selected_stream):
            raise AssertionError("metadata-proven CFR must not scan")

    info = _source_info(
        tmp_path / "cfr.mp4",
        SourceRevision(1, 2, 3, 4, 5),
        Container(),
        stream,
        frame,
    )

    assert info.duration == Fraction(3, 2)
    assert info.sustained_rate == Fraction(2)


def test_probe_rejects_a_source_narrower_or_shorter_than_the_minimum(tmp_path):
    frame = _yuv_frame(81, 90, 240, matrix=1, color_range=1)
    frame.pts = 0
    frame.time_base = Fraction(1, 2)
    stream = _color_stream(matrix=1)
    stream.width = 4
    stream.height = 720
    stream.time_base = Fraction(1, 2)
    stream.start_time = 0
    stream.duration = 3
    stream.average_rate = Fraction(2)
    stream.guessed_rate = Fraction(2)
    stream.base_rate = Fraction(2)
    stream.frames = 3

    class Container:
        duration = None

        def decode(self, selected_stream):
            raise AssertionError("metadata-proven CFR must not scan")

    with pytest.raises(AppError) as error:
        _source_info(
            tmp_path / "narrow.mp4",
            SourceRevision(1, 2, 3, 4, 5),
            Container(),
            stream,
            frame,
        )

    assert error.value.code is ErrorCode.SOURCE_DIMENSIONS_UNSUPPORTED
    assert "8" in error.value.technical_detail


@pytest.mark.parametrize("frame_pts", [(0, 1, 1), (0, 2, 1)])
def test_timeline_rejects_duplicate_or_decreasing_pts_as_unproven(frame_pts):
    frames = []
    for pts in frame_pts:
        frame = av.VideoFrame.from_image(Image.new("RGB", (2, 2), "red"))
        frame.pts = pts
        frame.time_base = Fraction(1, 10)
        frames.append(frame)
    stream = _color_stream(matrix=0)
    stream.time_base = Fraction(1, 10)

    class Container:
        def seek(self, *args, **kwargs):
            return None

        def decode(self, selected_stream):
            yield from frames

    with pytest.raises(AppError) as error:
        _derive_timeline(Container(), stream, Fraction(0), Fraction(10))

    assert error.value.code is ErrorCode.SOURCE_CORRUPT


def test_timeline_fallback_cancels_between_decoded_frames():
    frames = []
    for pts in range(5):
        frame = av.VideoFrame.from_image(Image.new("RGB", (2, 2), "red"))
        frame.pts = pts
        frame.time_base = Fraction(1, 10)
        frames.append(frame)
    stream = _color_stream(matrix=0)
    stream.time_base = Fraction(1, 10)
    checks = 0

    class Container:
        def seek(self, *args, **kwargs):
            return None

        def decode(self, selected_stream):
            yield from frames

    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(AppError) as error:
        _derive_timeline(
            Container(),
            stream,
            Fraction(0),
            Fraction(10),
            is_cancelled=cancelled,
        )

    assert error.value.code is ErrorCode.JOB_CANCELLED
    assert checks == 3


def test_timeline_bound_counts_frames_without_pts(monkeypatch):
    import matteloop.jobs.source as source_module

    stream = _color_stream(matrix=0)
    stream.time_base = Fraction(1, 60)

    class Container:
        def seek(self, *args, **kwargs):
            return None

        def decode(self, selected_stream):
            for pts in range(MAX_TIMELINE_DECODED_FRAMES + 1):
                yield SimpleNamespace(pts=pts, time_base=Fraction(1, 60))

    monkeypatch.setattr(source_module, "_validate_frame_color", lambda *args: None)

    with pytest.raises(AppError) as error:
        _derive_timeline(Container(), stream, Fraction(0), Fraction(60))

    assert error.value.code is ErrorCode.SOURCE_DURATION_UNSUPPORTED


def test_missing_single_frame_cadence_is_rejected_as_unproven():
    frame = av.VideoFrame.from_image(Image.new("RGB", (2, 2), "red"))
    frame.pts = 0
    frame.time_base = Fraction(1)
    stream = _color_stream(matrix=0)
    stream.time_base = Fraction(1)

    class Container:
        def seek(self, *args, **kwargs):
            return None

        def decode(self, selected_stream):
            yield frame

    with pytest.raises(AppError) as error:
        _derive_timeline(Container(), stream, Fraction(0), None)
    assert error.value.code is ErrorCode.SOURCE_FPS_UNSUPPORTED


def test_timeline_rejects_decoded_frame_without_pts():
    frame = av.VideoFrame.from_image(Image.new("RGB", (2, 2), "red"))
    frame.pts = None
    stream = _color_stream(matrix=0)
    stream.time_base = Fraction(1)

    class Container:
        def seek(self, *args, **kwargs):
            return None

        def decode(self, selected_stream):
            yield frame

    with pytest.raises(AppError) as error:
        _derive_timeline(Container(), stream, Fraction(0), Fraction(1))

    assert error.value.code is ErrorCode.SOURCE_CORRUPT


def test_stream_selection_skips_first_undecodable_video_stream():
    bad = SimpleNamespace(disposition=av.stream.Disposition(0))
    good = SimpleNamespace(disposition=av.stream.Disposition(0))
    frame = av.VideoFrame.from_image(Image.new("RGB", (2, 2), "green"))
    frame.pts = 0
    frame.time_base = Fraction(1)

    class Container:
        streams = SimpleNamespace(video=(bad, good))

        def seek(self, *args, **kwargs):
            return None

        def decode(self, stream):
            if stream is bad:
                raise ValueError("broken first stream")
            yield frame

    selected, first = _decodable_video_stream(Container(), None)

    assert selected is good
    assert first is frame


def test_probe_revision_binds_decode_to_same_regular_file(tmp_path):
    path = make_video(tmp_path / "revision.mp4", _solid_frames(), Fraction(2))
    info = probe_source(path)

    assert isinstance(info.revision, SourceRevision)
    assert info.file_size == path.stat().st_size
    replacement = make_video(
        tmp_path / "replacement.mp4", list(reversed(_solid_frames())), Fraction(2)
    )
    os.replace(replacement, path)

    with pytest.raises(AppError) as error:
        decode_frame(
            path,
            Fraction(0),
            request_id=3,
            expected_revision=info.revision,
        )
    assert error.value.code is ErrorCode.SOURCE_CHANGED


def test_validation_proof_is_immutable_serializable_and_structurally_bound(tmp_path):
    path = make_video(tmp_path / "proof.mp4", _solid_frames(), Fraction(2))
    info = probe_source(path)

    assert isinstance(info.validation_proof, SourceValidationProof)
    payload = info.validation_proof.to_payload()
    serialized = json.dumps(payload)
    restored = SourceValidationProof.from_payload(json.loads(serialized))
    assert payload["source_revision"]["inode"] == info.revision.inode
    assert payload["duration"] == [3, 2]
    assert restored == info.validation_proof

    mismatched = replace(info.validation_proof, stream_index=999)
    with pytest.raises(AppError) as error:
        decode_frame(
            path,
            Fraction(0),
            request_id=5,
            expected_revision=info.revision,
            validation_proof=mismatched,
        )

    assert error.value.code is ErrorCode.SOURCE_CHANGED

    other = make_video(
        tmp_path / "other-proof-source.mp4",
        list(reversed(_solid_frames())),
        Fraction(2),
    )
    with pytest.raises(AppError) as source_error:
        decode_frame(
            other,
            Fraction(0),
            request_id=6,
            validation_proof=restored,
        )
    assert source_error.value.code is ErrorCode.SOURCE_CHANGED


def test_retarget_during_decode_is_rejected_after_native_access(tmp_path, monkeypatch):
    path = make_video(tmp_path / "retarget.mp4", _solid_frames(), Fraction(2))
    replacement = make_video(
        tmp_path / "retarget-replacement.mp4",
        list(reversed(_solid_frames())),
        Fraction(2),
    )
    info = probe_source(path)
    import matteloop.jobs.source as source_module

    real_normalize = source_module._normalized_image

    def replace_then_normalize(*args, **kwargs):
        os.replace(replacement, path)
        return real_normalize(*args, **kwargs)

    monkeypatch.setattr(source_module, "_normalized_image", replace_then_normalize)

    with pytest.raises(AppError) as error:
        decode_frame(
            path,
            Fraction(0),
            request_id=3,
            expected_revision=info.revision,
        )

    assert error.value.code is ErrorCode.SOURCE_CHANGED


def test_probe_rejects_symlink_and_container_suffix_mismatch(tmp_path):
    original = make_video(tmp_path / "original.mp4", _solid_frames(), Fraction(2))
    symlink = tmp_path / "link.mp4"
    symlink.symlink_to(original)
    with pytest.raises(AppError) as symlink_error:
        probe_source(symlink)
    assert symlink_error.value.code is ErrorCode.SOURCE_UNREADABLE

    renamed = tmp_path / "renamed.webm"
    renamed.write_bytes(original.read_bytes())
    with pytest.raises(AppError) as format_error:
        probe_source(renamed)
    assert format_error.value.code is ErrorCode.SOURCE_FORMAT_UNSUPPORTED


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
    import matteloop.jobs.source as source_module

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


def test_decode_closes_container_when_normalization_raises_baseexception(
    tmp_path, monkeypatch
):
    path = make_video(tmp_path / "baseexception.mp4", _solid_frames(), Fraction(2))
    import matteloop.jobs.source as source_module

    real_open = source_module.av.open
    containers = []

    class FatalDecode(BaseException):
        pass

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
    monkeypatch.setattr(
        source_module,
        "_normalized_image",
        lambda *args, **kwargs: (_ for _ in ()).throw(FatalDecode()),
    )

    with pytest.raises(FatalDecode):
        decode_frame(path, Fraction(0), request_id=1)

    assert len(containers) == 1
    assert containers[0].closed


def test_late_decode_seeks_to_a_nearby_keyframe_instead_of_retaining_whole_video(
    tmp_path, monkeypatch
):
    path = _make_seekable_video(tmp_path / "seekable.mkv")
    import matteloop.jobs.source as source_module

    real_open = source_module.av.open
    yielded_since_seek = 0

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
            nonlocal yielded_since_seek
            for frame in self._container.decode(stream):
                yielded_since_seek += 1
                yield frame

        def seek(self, *args, **kwargs):
            nonlocal yielded_since_seek
            yielded_since_seek = 0
            return self._container.seek(*args, **kwargs)

    monkeypatch.setattr(
        source_module.av,
        "open",
        lambda *args, **kwargs: CountingContainer(real_open(*args, **kwargs)),
    )

    decoded = decode_frame(path, Fraction(101, 20), request_id=22)

    assert decoded.actual_pts == Fraction(101, 20)
    assert yielded_since_seek < 30


def test_rotation_metadata_parser_accepts_only_quarter_turns():
    assert _rotation_from_metadata({"rotate": "-90"}) == 90
    assert _rotation_from_metadata({"rotate": "450.0"}) == 270
    assert _rotation_from_metadata({"rotate": "12"}) == 0
    assert _rotation_from_metadata({}) == 0


def test_display_matrix_boundary_uses_ffmpeg_clockwise_sign():
    class DisplayMatrix:
        type = SimpleNamespace(name="DISPLAYMATRIX")

        def __bytes__(self):
            return struct.pack("=9i", 0, 65536, 0, -65536, 0, 0, 0, 0, 1 << 30)

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


def _yuv_frame(
    y: int,
    u: int,
    v: int,
    *,
    matrix: int,
    color_range: int,
    width: int = 16,
    height: int = 16,
):
    # libswscale on Linux x86_64 corrupts the heap (glibc "corrupted size vs.
    # prev_size", SIGABRT) reformatting some 2x2/6x6/8x8 yuv420p frames through
    # VideoReformatter — reproduced with av 16.1.0 / libswscale 9.1.100, not on
    # macOS arm64. 16x16 is proven safe there; every caller fills the frame
    # uniformly and asserts a single pixel, so a larger frame changes nothing
    # about what is being tested.
    frame = av.VideoFrame(width, height, "yuv420p")
    frame.colorspace = matrix
    frame.color_range = color_range
    for plane, value in zip(frame.planes, (y, u, v)):
        plane.update(bytes([value]) * plane.buffer_size)
    return frame


def _color_stream(*, matrix: int, transfer: int = 13, primaries: int = 1):
    codec = SimpleNamespace(
        color_primaries=primaries,
        color_trc=transfer,
        colorspace=matrix,
        color_range=0,
        pix_fmt="yuv420p",
        sample_aspect_ratio=None,
    )
    return SimpleNamespace(
        codec_context=codec,
        sample_aspect_ratio=None,
        metadata={},
        side_data=(),
    )


def test_yuv_limited_and_full_range_are_explicitly_normalized_to_srgb():
    limited = _yuv_frame(16, 128, 128, matrix=1, color_range=1)
    full = _yuv_frame(0, 128, 128, matrix=1, color_range=2)
    stream = _color_stream(matrix=1)

    assert _normalized_image(limited, stream, limited).getpixel((0, 0)) == (
        0,
        0,
        0,
        255,
    )
    assert _normalized_image(full, stream, full).getpixel((0, 0)) == (
        0,
        0,
        0,
        255,
    )


def test_yuv_matrix_selection_changes_literal_rgba_conversion():
    bt709 = _yuv_frame(81, 90, 240, matrix=1, color_range=1)
    bt601 = _yuv_frame(81, 90, 240, matrix=5, color_range=1)

    bt709_pixel = _normalized_image(bt709, _color_stream(matrix=1), bt709).getpixel(
        (0, 0)
    )
    bt601_pixel = _normalized_image(bt601, _color_stream(matrix=5), bt601).getpixel(
        (0, 0)
    )

    # libswscale's exact byte-level rounding for this matrix conversion differs
    # by a channel or two between SIMD implementations: measured (255, 23, 0,
    # 255) / (253, 0, 0, 255) on Linux x86_64 and (255, 24, 0, 255) / (254, 0,
    # 0, 255) on macOS arm64. Assert the stable structure plus a tolerance band
    # on the one varying channel per pixel, rather than brittle exact bytes.
    assert bt709_pixel[0] == 255
    assert 21 <= bt709_pixel[1] <= 26
    assert bt709_pixel[2] == 0
    assert bt709_pixel[3] == 255

    assert 250 <= bt601_pixel[0] <= 255
    assert bt601_pixel[1] == 0
    assert bt601_pixel[2] == 0
    assert bt601_pixel[3] == 255

    assert bt709_pixel != bt601_pixel


def test_untagged_hd_yuv_assumes_bt709_defaults():
    # width=16, not the synthetic-minimum 4, because 4x720 hits the same
    # libswscale heap corruption documented on _yuv_frame above; height=720
    # is load-bearing for the height >= 720 "HD" branch under test.
    frame = _yuv_frame(
        81, 90, 240, matrix=2, color_range=0, width=16, height=720
    )
    stream = _color_stream(matrix=2, transfer=2, primaries=2)
    profile = _color_profile(stream, frame)

    assert (
        profile.matrix,
        profile.color_range,
        profile.transfer,
        profile.primaries,
    ) == (
        1,
        1,
        1,
        1,
    )
    assert dict(profile.assumptions) == {
        "primaries": 1,
        "transfer": 1,
        "matrix": 1,
        "range": 1,
    }
    assert _normalized_image(frame, stream, frame).size == (16, 720)


def test_untagged_sd_yuv_assumes_bt601_matrix():
    frame = _yuv_frame(
        81, 90, 240, matrix=2, color_range=0, width=4, height=480
    )
    stream = _color_stream(matrix=2, transfer=2, primaries=2)

    profile = _color_profile(stream, frame)

    assert profile.matrix == 6
    assert profile.color_range == 1
    assert dict(profile.assumptions)["matrix"] == 6


@pytest.mark.parametrize("transfer", [16, 18])
def test_hdr_transfer_characteristics_are_rejected(transfer):
    frame = _yuv_frame(81, 90, 240, matrix=1, color_range=1)
    stream = _color_stream(matrix=1, transfer=transfer)

    with pytest.raises(AppError) as error:
        _color_profile(stream, frame)

    assert error.value.code is ErrorCode.SOURCE_HDR_UNSUPPORTED
    assert f"{transfer}" in error.value.technical_detail


def test_bt2020_primaries_are_rejected():
    frame = _yuv_frame(81, 90, 240, matrix=1, color_range=1)
    stream = _color_stream(matrix=1, primaries=9)

    with pytest.raises(AppError) as error:
        _color_profile(stream, frame)

    assert error.value.code is ErrorCode.SOURCE_HDR_UNSUPPORTED
    assert "BT.2020" in error.value.technical_detail


@pytest.mark.parametrize("matrix", [9, 10])
def test_bt2020_matrices_are_rejected(matrix):
    frame = _yuv_frame(81, 90, 240, matrix=matrix, color_range=1)
    stream = _color_stream(matrix=matrix)

    with pytest.raises(AppError) as error:
        _color_profile(stream, frame)

    assert error.value.code is ErrorCode.SOURCE_HDR_UNSUPPORTED
    assert "BT.2020" in error.value.technical_detail
    assert str(matrix) in error.value.technical_detail


def test_explicit_bt709_yuv_metadata_keeps_declared_profile():
    frame = _yuv_frame(81, 90, 240, matrix=1, color_range=1, width=4, height=720)
    stream = _color_stream(matrix=1, transfer=1, primaries=1)
    stream.codec_context.color_range = 1

    profile = _color_profile(stream, frame)

    assert (
        profile.matrix,
        profile.color_range,
        profile.transfer,
        profile.primaries,
    ) == (
        1,
        1,
        1,
        1,
    )
    assert profile.assumptions == ()


def test_bt709_transfer_is_converted_to_srgb_with_deterministic_lut():
    frame = av.VideoFrame.from_image(Image.new("RGB", (2, 2), (128, 128, 128)))
    frame.colorspace = 0
    frame.color_range = 2
    stream = _color_stream(matrix=0, transfer=1)

    assert _normalized_image(frame, stream, frame).getpixel((0, 0)) == (
        140,
        140,
        140,
        255,
    )


def test_tracker_covers_rgb_transfer_sar_and_rotation_owner_lifetimes() -> None:
    frame = av.VideoFrame.from_image(Image.new("RGB", (4, 2), (128, 128, 128)))
    stream = _color_stream(matrix=0, transfer=1)
    stream.sample_aspect_ratio = Fraction(2)
    stream.codec_context.sample_aspect_ratio = Fraction(2)
    stream.metadata = {"rotate": "-90"}

    class RetainingTracker(RgbaOwnershipTracker):
        def __init__(self) -> None:
            super().__init__((4, 2))
            self.retained: list[object] = []

        def register[OwnerT](
            self,
            owner: OwnerT,
            *,
            known_full_resolution_rgba: bool = False,
        ) -> OwnerT:
            self.retained.append(owner)
            return super().register(
                owner,
                known_full_resolution_rgba=known_full_resolution_rgba,
            )

        def track_nonweak[OwnerT](self, owner: OwnerT) -> RgbaOwnershipHandle[OwnerT]:
            self.retained.append(owner)
            return super().track_nonweak(owner)

    tracker = RetainingTracker()
    image = _normalized_image(
        frame,
        stream,
        frame,
        rgba_ownership_tracker=tracker,
    )

    assert image.size == (2, 8)
    assert tracker.peak == 6
    assert tracker.current == 6
    tracker.retained.clear()
    gc.collect()
    assert tracker.current == 1
    image.close()
    gc.collect()
    assert tracker.current == 1
    del image
    gc.collect()
    assert tracker.current == 0


@pytest.mark.parametrize("primaries", [8, 12, 22])
def test_per_frame_p3_or_other_wide_gamut_metadata_is_rejected(primaries):
    frame = av.VideoFrame.from_image(Image.new("RGB", (2, 2), "red"))

    class FrameMetadata:
        color_primaries = primaries
        color_trc = 13
        colorspace = 0
        color_range = 2
        sample_aspect_ratio = None
        side_data = ()
        format = frame.format

        def to_image(self):
            return frame.to_image()

    with pytest.raises(AppError) as error:
        _normalized_image(
            FrameMetadata(),
            _color_stream(matrix=0, primaries=primaries),
            FrameMetadata(),
        )
    assert error.value.code is ErrorCode.SOURCE_HDR_UNSUPPORTED


@pytest.mark.parametrize(
    ("primaries", "transfer", "matrix", "color_range"),
    [
        (8, 13, 1, 1),
        (1, 4, 1, 1),
        (1, 13, 3, 1),
        (1, 13, 1, 3),
    ],
)
def test_unknown_color_metadata_is_rejected(
    primaries, transfer, matrix, color_range
):
    frame = _yuv_frame(81, 90, 240, matrix=matrix, color_range=color_range)
    stream = _color_stream(
        matrix=matrix,
        transfer=transfer,
        primaries=primaries,
    )
    stream.codec_context.color_range = color_range

    with pytest.raises(AppError) as error:
        _normalized_image(frame, stream, frame)

    assert error.value.code is ErrorCode.SOURCE_HDR_UNSUPPORTED


def test_conflicting_rotation_or_per_frame_sar_is_rejected():
    frame = av.VideoFrame.from_image(Image.new("RGB", (2, 2), "red"))

    class Matrix:
        type = SimpleNamespace(name="DISPLAYMATRIX")

        def __bytes__(self):
            return struct.pack("=9i", 0, -65536, 0, 65536, 0, 0, 0, 0, 1 << 30)

    class FrameMetadata:
        side_data = (Matrix(),)
        sample_aspect_ratio = Fraction(2)
        colorspace = 0
        color_range = 2
        format = frame.format

        def to_image(self):
            return frame.to_image()

    stream = _color_stream(matrix=0)
    stream.metadata = {"rotate": "90"}
    stream.sample_aspect_ratio = Fraction(1)

    with pytest.raises(AppError) as error:
        _normalized_image(FrameMetadata(), stream, FrameMetadata())
    assert error.value.code is ErrorCode.SOURCE_CORRUPT


def test_explicit_identity_matrix_conflicting_with_legacy_rotation_is_rejected():
    frame = av.VideoFrame.from_image(Image.new("RGB", (2, 2), "red"))

    class IdentityMatrix:
        type = SimpleNamespace(name="DISPLAYMATRIX")

        def __bytes__(self):
            return struct.pack("=9i", 65536, 0, 0, 0, 65536, 0, 0, 0, 1 << 30)

    class FrameMetadata:
        side_data = (IdentityMatrix(),)
        sample_aspect_ratio = None
        colorspace = 0
        color_range = 2
        format = frame.format

        def reformat(self, *, format):
            return frame.reformat(format=format)

    stream = _color_stream(matrix=0)
    stream.metadata = {"rotate": "90"}

    with pytest.raises(AppError) as error:
        _normalized_image(FrameMetadata(), stream, FrameMetadata())

    assert error.value.code is ErrorCode.SOURCE_CORRUPT


def test_per_frame_hdr_side_data_is_rejected():
    frame = av.VideoFrame.from_image(Image.new("RGB", (2, 2), "red"))

    class FrameMetadata:
        side_data = (SimpleNamespace(type=SimpleNamespace(name="DYNAMIC_HDR_PLUS")),)
        sample_aspect_ratio = None
        color_primaries = 1
        color_trc = 13
        colorspace = 0
        color_range = 2
        format = frame.format

    with pytest.raises(AppError) as error:
        _normalized_image(FrameMetadata(), _color_stream(matrix=0), FrameMetadata())

    assert error.value.code is ErrorCode.SOURCE_HDR_UNSUPPORTED


def test_probe_rejects_non_local_and_audio_only_sources(tmp_path):
    with pytest.raises(AppError) as network_error:
        probe_source("https://example.invalid/video.mp4")
    assert network_error.value.code is ErrorCode.SOURCE_NOT_LOCAL
    assert network_error.value.retry_action == "choose-local-file"

    audio_path = tmp_path / "audio-only.mkv"
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

    high_fps = make_video(tmp_path / "61fps.mp4", _solid_frames()[:2], Fraction(61))
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


def test_revision_drops_the_ctime_windows_reports_inconsistently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Windows reports a different st_ctime for os.stat(path) than for
    # os.fstat(fd) of the same file, so any revision mixing both sources
    # never matched there -- every decode on the frozen Windows build failed
    # with "source changed while it was opened".
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"data")
    info = target.lstat()

    if os.name != "nt":
        assert source_module._revision_from_stat(info).ctime_ns == info.st_ctime_ns

    monkeypatch.setattr(source_module.os, "name", "nt")
    windows = source_module._revision_from_stat(info)

    assert windows.ctime_ns == 0
    assert (windows.size, windows.mtime_ns) == (info.st_size, info.st_mtime_ns)
    assert (windows.device, windows.inode) == (info.st_dev, info.st_ino)
