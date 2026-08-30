"""Bounded-memory lossless WebP encoding and output validation."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import tempfile
import warnings
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO, cast

import av
import numpy as np
from PIL import Image, ImageChops

from rembggui.core.errors import AppError, ErrorCode, ValidationError
from rembggui.core.geometry import FramingPlan, solve_proportional_scale
from rembggui.core.rgba import RgbaOwnershipTracker
from rembggui.core.specs import (
    MIN_FINAL_DIMENSION,
    FramingSpec,
    is_local_path_syntax,
)
from rembggui.core.timebase import MAX_OUTPUT_FRAMES

_RIFF_LIMIT = 1 << 32
_MAX_WEBP_DELAY_MS = (1 << 24) - 1
_MAX_FIT_ENCODINGS = 12


@dataclass(frozen=True)
class WebPInfo:
    """Facts independently decoded from a complete WebP artifact."""

    width: int
    height: int
    frames: int
    duration_ms: int
    delays_ms: tuple[int, ...]
    loop: int
    has_alpha: bool
    lossless: bool
    file_size: int

    @property
    def size_bytes(self) -> int:
        return self.file_size


@dataclass(frozen=True)
class EncodeSummary:
    """Stable summary of one validated, atomically promoted encode."""

    destination: Path
    width: int
    height: int
    frames: int
    duration_ms: int
    file_size: int

    @property
    def path(self) -> Path:
        return self.destination

    @property
    def size_bytes(self) -> int:
        return self.file_size

    @property
    def bytes_written(self) -> int:
        return self.file_size


@dataclass(frozen=True)
class _FrameSet:
    paths: tuple[Path, ...]
    delays_ms: tuple[int, ...]
    size: tuple[int, int]
    identities: tuple[_FileIdentity, ...]
    animated: bool

    @property
    def encoded_duration_ms(self) -> int:
        return sum(self.delays_ms) if self.animated else 0


@dataclass(frozen=True)
class _RiffFacts:
    width: int
    height: int
    delays_ms: tuple[int, ...]
    loop: int
    has_alpha_flag: bool

    @property
    def frames(self) -> int:
        return len(self.delays_ms) if self.delays_ms else 1

    @property
    def duration_ms(self) -> int:
        return sum(self.delays_ms)

    @property
    def lossless(self) -> bool:
        return True


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


def encode_lossless_webp(
    frame_paths: Sequence[Path],
    delays_ms: Sequence[int],
    destination: Path,
    *,
    rgba_ownership_tracker: RgbaOwnershipTracker | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> EncodeSummary:
    """Encode PNG-backed RGBA frames and atomically replace *destination*."""

    if rgba_ownership_tracker is None:
        frames = _validate_frame_inputs(frame_paths, delays_ms)
    else:
        frames = _validate_frame_inputs(
            frame_paths,
            delays_ms,
            rgba_ownership_tracker,
        )
    emitted = (
        _collapse_identical_frames(frames, rgba_ownership_tracker)
        if frames.animated
        else frames
    )
    destination = _validate_destination(destination)
    temporary = _sibling_temporary(destination)
    primary: BaseException | None = None
    try:
        if not emitted.animated:
            if rgba_ownership_tracker is None:
                _encode_still(emitted.paths[0], emitted.identities[0], temporary)
            else:
                _encode_still(
                    emitted.paths[0],
                    emitted.identities[0],
                    temporary,
                    rgba_ownership_tracker,
                )
            if progress is not None:
                progress(1, 1)
        else:
            _encode_animation(emitted, temporary, rgba_ownership_tracker, progress)
        _fsync_file(temporary)
        if rgba_ownership_tracker is None:
            info = validate_webp(
                temporary,
                expected_frames=len(emitted.paths),
                expected_duration_ms=frames.encoded_duration_ms,
            )
        else:
            info = validate_webp(
                temporary,
                expected_frames=len(emitted.paths),
                expected_duration_ms=frames.encoded_duration_ms,
                rgba_ownership_tracker=rgba_ownership_tracker,
            )
        if (info.width, info.height) != frames.size:
            raise _invalid_output("encoded dimensions do not match the input frames")
        expected_delays = emitted.delays_ms if emitted.animated else ()
        if info.delays_ms != expected_delays:
            raise _invalid_output(
                "encoded frame delays do not match the input sequence"
            )
        _validate_encoded_pixels(
            emitted.paths,
            temporary,
            emitted.identities,
            rgba_ownership_tracker,
        )
        os.replace(temporary, destination)
    except AppError as error:
        primary = error
        raise
    except OSError as error:
        wrapped = _output_os_error(error, "lossless WebP encoding failed")
        primary = wrapped
        raise wrapped from error
    except (ValueError, av.FFmpegError) as error:
        wrapped = _invalid_output(f"lossless WebP encoding failed: {error}")
        primary = wrapped
        raise wrapped from error
    except BaseException as error:
        primary = error
        raise
    finally:
        _cleanup_file(temporary, primary)

    return EncodeSummary(
        destination=destination,
        width=info.width,
        height=info.height,
        frames=info.frames,
        duration_ms=info.duration_ms,
        file_size=info.file_size,
    )


def validate_webp(
    source: Path | BinaryIO,
    expected_frames: int,
    expected_duration_ms: int,
    *,
    rgba_ownership_tracker: RgbaOwnershipTracker | None = None,
) -> WebPInfo:
    """Validate a WebP path or caller-owned stable binary file.

    An open binary is never closed and its logical position is restored.  RIFF
    parsing and Pillow decoding use a duplicate of that exact file descriptor,
    so neither phase reopens a mutable pathname.
    """

    if (
        not isinstance(expected_frames, int)
        or isinstance(expected_frames, bool)
        or not 1 <= expected_frames <= MAX_OUTPUT_FRAMES
    ):
        raise _invalid_output("expected frame count must be between 1 and 100000")
    if (
        not isinstance(expected_duration_ms, int)
        or isinstance(expected_duration_ms, bool)
        or expected_duration_ms < 0
    ):
        raise _invalid_output("expected duration must be a non-negative integer")

    try:
        with _open_webp_validation_source(source) as (held, file_size):
            if file_size >= _RIFF_LIMIT:
                raise _invalid_output("WebP output must be smaller than 4 GiB")
            riff = _parse_riff(held, file_size)
            held.seek(0)
            with _open_pillow(held) as image:
                if image.format != "WEBP":
                    raise _invalid_output("output is not a WebP image")
                width, height = FramingSpec().validate_final_dimensions(*image.size)
                decoded_frames = getattr(image, "n_frames", 1)
                has_alpha = False
                for index in range(decoded_frames):
                    image.seek(index)
                    image.load()
                    if rgba_ownership_tracker is not None:
                        rgba_ownership_tracker.register(image)
                    has_alpha = has_alpha or image.mode in {"RGBA", "LA"}
    except AppError:
        raise
    except (OSError, EOFError, ValueError, SyntaxError) as error:
        raise _invalid_output(f"invalid or truncated WebP output: {error}") from error

    if decoded_frames != riff.frames or decoded_frames != expected_frames:
        raise _invalid_output(
            "WebP frame count does not match the expected complete output"
        )
    if riff.duration_ms != expected_duration_ms:
        raise _invalid_output("WebP duration does not match the expected duration")
    if (width, height) != (riff.width, riff.height):
        raise _invalid_output("decoded dimensions do not match the RIFF canvas")
    if riff.loop != 0:
        raise _invalid_output("animated WebP must loop infinitely")
    if not riff.lossless:
        raise _invalid_output("WebP output contains a lossy frame")
    if riff.has_alpha_flag and not has_alpha:
        raise _invalid_output("WebP alpha flag does not match decoded pixels")

    return WebPInfo(
        width=width,
        height=height,
        frames=decoded_frames,
        duration_ms=riff.duration_ms,
        delays_ms=riff.delays_ms,
        loop=riff.loop,
        has_alpha=has_alpha,
        lossless=riff.lossless,
        file_size=file_size,
    )


def fit_webp_to_size(
    source_frame_paths: Sequence[Path],
    delays_ms: Sequence[int],
    target_bytes: int,
    work_dir: Path,
    destination: Path,
    *,
    is_cancelled: Callable[[], bool] | None = None,
    rgba_ownership_tracker: RgbaOwnershipTracker | None = None,
    summary_out: list[EncodeSummary] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Fit a validated WebP to a byte target using at most twelve encodes."""

    _raise_if_fit_cancelled(is_cancelled)
    if rgba_ownership_tracker is None:
        source = _validate_frame_inputs(
            source_frame_paths, delays_ms, is_cancelled=is_cancelled
        )
    else:
        source = _validate_frame_inputs(
            source_frame_paths,
            delays_ms,
            rgba_ownership_tracker,
            is_cancelled=is_cancelled,
        )
    if (
        not isinstance(target_bytes, int)
        or isinstance(target_bytes, bool)
        or target_bytes <= 0
    ):
        raise _impossible_size("target size must be a positive integer byte count")
    work_dir = _require_path(work_dir, "work directory")
    destination = _validate_destination(destination)
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="webp-fit-", dir=work_dir))
    except OSError as error:
        raise _output_os_error(error, "cannot create auto-fit workspace") from error

    primary: BaseException | None = None
    prepared_output: Path | None = None
    scratch_cleaned = False
    try:
        _raise_if_fit_cancelled(is_cancelled)
        source = _snapshot_frame_set(
            source,
            scratch / "source-snapshot",
            rgba_ownership_tracker,
            is_cancelled=is_cancelled,
        )
        _raise_if_fit_cancelled(is_cancelled)
        cumulative_scale = Decimal(1)
        current_paths = source.paths
        current_size = source.size
        active_scaled_dir: Path | None = None
        for attempt in range(_MAX_FIT_ENCODINGS):
            _raise_if_fit_cancelled(is_cancelled)
            candidate = scratch / f"candidate-{attempt:02d}.webp"
            summary = _encode_candidate(
                current_paths,
                source.delays_ms,
                candidate,
                rgba_ownership_tracker,
                progress,
            )
            _raise_if_fit_cancelled(is_cancelled)
            if summary.file_size <= target_bytes:
                candidate_frames = _validate_frame_inputs(
                    current_paths,
                    source.delays_ms,
                    rgba_ownership_tracker,
                    is_cancelled=is_cancelled,
                )
                emitted = (
                    _collapse_identical_frames(
                        candidate_frames, rgba_ownership_tracker
                    )
                    if candidate_frames.animated
                    else candidate_frames
                )
                prepared_output = _prepare_candidate(
                    candidate,
                    destination,
                    emitted,
                    source.encoded_duration_ms,
                    current_size,
                    target_bytes,
                    rgba_ownership_tracker,
                )
                _raise_if_fit_cancelled(is_cancelled)
                _cleanup_tree(scratch, None)
                scratch_cleaned = True
                try:
                    os.replace(prepared_output, destination)
                except OSError as error:
                    raise _output_os_error(
                        error, "cannot atomically promote fitted WebP"
                    ) from error
                prepared_output = None
                if summary_out is not None:
                    summary_out.append(
                        EncodeSummary(
                            destination,
                            summary.width,
                            summary.height,
                            summary.frames,
                            summary.duration_ms,
                            summary.file_size,
                        )
                    )
                return destination

            if attempt + 1 == _MAX_FIT_ENCODINGS:
                break
            next_scale = solve_proportional_scale(
                source.size[0],
                source.size[1],
                current_scale=cumulative_scale,
                target_bytes=target_bytes,
                current_bytes=summary.file_size,
                min_dimension=MIN_FINAL_DIMENSION,
            )
            if next_scale >= Decimal(1):
                break
            next_size = _scaled_dimensions(source.size, next_scale)
            if rgba_ownership_tracker is not None:
                rgba_ownership_tracker.include_size(next_size)
            if (
                next_size == (MIN_FINAL_DIMENSION, MIN_FINAL_DIMENSION)
                and (
                    summary.width,
                    summary.height,
                )
                == next_size
            ):
                break
            _cleanup_file(candidate, None)
            if active_scaled_dir is not None:
                _cleanup_tree(active_scaled_dir, None)
            scaled_dir = scratch / f"scaled-{attempt + 1:02d}"
            _raise_if_fit_cancelled(is_cancelled)
            if rgba_ownership_tracker is None:
                current_paths = _resize_from_sources(
                    source.paths,
                    next_size,
                    scaled_dir,
                    is_cancelled=is_cancelled,
                )
            else:
                current_paths = _resize_from_sources(
                    source.paths,
                    next_size,
                    scaled_dir,
                    rgba_ownership_tracker,
                    is_cancelled=is_cancelled,
                )
            _raise_if_fit_cancelled(is_cancelled)
            current_size = next_size
            active_scaled_dir = scaled_dir
            cumulative_scale = next_scale

        raise _impossible_size(
            "lossless WebP cannot meet the requested size within 12 encodes and "
            "the 128 px minimum"
        )
    except BaseException as error:
        primary = error
        raise
    finally:
        if prepared_output is not None:
            _cleanup_file(prepared_output, primary)
        if not scratch_cleaned:
            _cleanup_tree(scratch, primary)


def _encode_candidate(
    frame_paths: Sequence[Path],
    delays_ms: Sequence[int],
    destination: Path,
    rgba_ownership_tracker: RgbaOwnershipTracker | None,
    progress: Callable[[int, int], None] | None,
) -> EncodeSummary:
    if rgba_ownership_tracker is None and progress is None:
        return encode_lossless_webp(frame_paths, delays_ms, destination)
    return encode_lossless_webp(
        frame_paths,
        delays_ms,
        destination,
        rgba_ownership_tracker=rgba_ownership_tracker,
        progress=progress,
    )


def _validate_frame_inputs(
    frame_paths: Sequence[Path],
    delays_ms: Sequence[int],
    rgba_ownership_tracker: RgbaOwnershipTracker | None = None,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> _FrameSet:
    if not isinstance(frame_paths, Sequence) or isinstance(frame_paths, (str, bytes)):
        raise _invalid_output("frame_paths must be a finite path sequence")
    if not isinstance(delays_ms, Sequence) or isinstance(delays_ms, (str, bytes)):
        raise _invalid_output("delays_ms must be a finite integer sequence")
    count = len(frame_paths)
    if not 1 <= count <= MAX_OUTPUT_FRAMES:
        raise _invalid_output("frame count must be between 1 and 100000")
    if len(delays_ms) != count:
        raise _invalid_output("every frame must have exactly one delay")

    delays = tuple(delays_ms)
    if any(
        not isinstance(delay, int)
        or isinstance(delay, bool)
        or not 1 <= delay <= _MAX_WEBP_DELAY_MS
        for delay in delays
    ):
        raise _invalid_output(
            "frame delays must be positive integers representable by WebP"
        )

    paths: list[Path] = []
    identities: list[_FileIdentity] = []
    expected_size: tuple[int, int] | None = None
    for raw_path in frame_paths:
        _raise_if_fit_cancelled(is_cancelled)
        path = _require_path(raw_path, "frame path")
        try:
            with _open_stable_binary(path) as (input_file, identity):
                with _open_pillow(input_file) as image:
                    if image.format != "PNG" or image.mode != "RGBA":
                        raise _invalid_output(f"frame must be an RGBA PNG: {path}")
                    size = FramingSpec().validate_final_dimensions(*image.size)
                    if rgba_ownership_tracker is not None:
                        rgba_ownership_tracker.register(image)
                    if expected_size is None:
                        expected_size = size
                    elif size != expected_size:
                        raise _invalid_output(
                            "all input frames must have identical dimensions"
                        )
                    image.verify()
                    _raise_if_fit_cancelled(is_cancelled)
        except AppError:
            raise
        except (OSError, EOFError, ValueError, SyntaxError) as error:
            raise _invalid_output(
                f"unreadable or corrupt frame {path}: {error}"
            ) from error
        paths.append(path)
        identities.append(identity)

    assert expected_size is not None
    return _FrameSet(
        tuple(paths), delays, expected_size, tuple(identities), count > 1
    )


def _collapse_identical_frames(
    frames: _FrameSet,
    rgba_ownership_tracker: RgbaOwnershipTracker | None,
) -> _FrameSet:
    run_paths: list[Path] = []
    run_delays: list[int] = []
    run_identities: list[_FileIdentity] = []
    previous_pixels: np.ndarray | None = None
    for path, identity, delay in zip(
        frames.paths, frames.identities, frames.delays_ms, strict=True
    ):
        pixels = _read_rgba_pixels(path, identity, rgba_ownership_tracker)
        if previous_pixels is not None and np.array_equal(previous_pixels, pixels):
            run_delays[-1] += delay
            del pixels
            continue
        run_paths.append(path)
        run_delays.append(delay)
        run_identities.append(identity)
        previous_pixels = pixels
    del previous_pixels
    collapsed = _FrameSet(
        tuple(run_paths),
        tuple(run_delays),
        frames.size,
        tuple(run_identities),
        frames.animated,
    )
    return _split_long_animation_runs(collapsed)


def _split_long_animation_runs(frames: _FrameSet) -> _FrameSet:
    paths: list[Path] = []
    delays: list[int] = []
    identities: list[_FileIdentity] = []
    for path, delay, identity in zip(
        frames.paths, frames.delays_ms, frames.identities, strict=True
    ):
        while delay > _MAX_WEBP_DELAY_MS:
            paths.append(path)
            delays.append(_MAX_WEBP_DELAY_MS)
            identities.append(identity)
            delay -= _MAX_WEBP_DELAY_MS
        paths.append(path)
        delays.append(delay)
        identities.append(identity)
    return _FrameSet(tuple(paths), tuple(delays), frames.size, tuple(identities), True)


def _read_rgba_pixels(
    path: Path,
    identity: _FileIdentity,
    rgba_ownership_tracker: RgbaOwnershipTracker | None,
) -> np.ndarray:
    with _open_stable_binary(path, identity) as (input_file, _opened_identity):
        with _open_pillow(input_file) as image:
            image.load()
            if rgba_ownership_tracker is not None:
                rgba_ownership_tracker.register(image)
            pixels = np.array(image, dtype=np.uint8, copy=True)
            if rgba_ownership_tracker is not None:
                rgba_ownership_tracker.register(pixels)
            return pixels


def _encode_still(
    source: Path,
    identity: _FileIdentity,
    destination: Path,
    rgba_ownership_tracker: RgbaOwnershipTracker | None = None,
) -> None:
    with _open_stable_binary(source, identity) as (input_file, _opened_identity):
        with _open_pillow(input_file) as image:
            image.load()
            if rgba_ownership_tracker is not None:
                rgba_ownership_tracker.register(image)
            image.save(
                destination,
                format="WEBP",
                lossless=True,
                quality=100,
                alpha_quality=100,
                method=6,
                exact=True,
                icc_profile=None,
                exif=b"",
                xmp=b"",
            )


def _encode_animation(
    frames: _FrameSet,
    destination: Path,
    rgba_ownership_tracker: RgbaOwnershipTracker | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    base_frames, repeat_counts = _animation_base_frames(frames)
    width, height = frames.size
    container = av.open(
        str(destination), mode="w", format="webp", options={"loop": "0"}
    )
    try:
        stream = cast(
            av.video.stream.VideoStream,
            container.add_stream("libwebp_anim"),
        )
        stream.width = width
        stream.height = height
        stream.pix_fmt = "bgra"
        stream.time_base = Fraction(1, 1000)
        stream.codec_context.time_base = Fraction(1, 1000)
        stream.options = {
            "lossless": "1",
            "quality": "100",
            "compression_level": "6",
        }
        _encode_animation_frames(
            base_frames,
            stream,
            container,
            rgba_ownership_tracker,
            progress,
        )
    finally:
        container.close()
    if len(base_frames.paths) == 1:
        _remove_last_animation_frame(destination)
    _expand_animation_frames(destination, repeat_counts)
    _rewrite_animation_durations(destination, frames.delays_ms)


def _encode_animation_frames(
    frames: _FrameSet,
    stream: av.video.stream.VideoStream,
    container: av.container.OutputContainer,
    rgba_ownership_tracker: RgbaOwnershipTracker | None,
    progress: Callable[[int, int], None] | None,
) -> None:
    if len(frames.paths) == 1:
        path = frames.paths[0]
        identity = frames.identities[0]
        delay = frames.delays_ms[0]
        _encode_animation_frame(
            path,
            identity,
            delay,
            0,
            frames.size,
            stream,
            container,
            rgba_ownership_tracker,
        )
        if progress is not None:
            progress(1, len(frames.paths))
        # libwebp_anim emits a still for one input frame; a distinct sentinel
        # forces animation metadata before the sentinel is removed.
        _encode_animation_frame(
            path,
            identity,
            1,
            delay,
            frames.size,
            stream,
            container,
            rgba_ownership_tracker,
            mutate_pixel=True,
        )
    else:
        timestamp = 0
        for index, (path, identity, delay) in enumerate(zip(
            frames.paths, frames.identities, frames.delays_ms, strict=True
        )):
            _encode_animation_frame(
                path,
                identity,
                delay,
                timestamp,
                frames.size,
                stream,
                container,
                rgba_ownership_tracker,
            )
            if progress is not None:
                progress(index + 1, len(frames.paths))
            timestamp += delay
    for packet in stream.encode():
        container.mux(packet)


def _encode_animation_frame(
    path: Path,
    identity: _FileIdentity,
    delay: int,
    timestamp: int,
    size: tuple[int, int],
    stream: av.video.stream.VideoStream,
    container: av.container.OutputContainer,
    rgba_ownership_tracker: RgbaOwnershipTracker | None,
    *,
    mutate_pixel: bool = False,
) -> None:
    with _open_stable_binary(path, identity) as (input_file, _opened_identity):
        with _open_pillow(input_file) as image:
            image.load()
            if rgba_ownership_tracker is not None:
                rgba_ownership_tracker.register(image)
            pixels = (
                np.array(image, dtype=np.uint8, copy=True)
                if mutate_pixel
                else np.asarray(image)
            )
            if mutate_pixel:
                pixels[0, 0, 0] ^= 1
            if rgba_ownership_tracker is not None:
                rgba_ownership_tracker.register(pixels)
            frame = av.VideoFrame.from_ndarray(pixels, format="rgba")
            if rgba_ownership_tracker is not None:
                frame_owner = rgba_ownership_tracker.track_nonweak(frame)
                frame = frame_owner.value
    if (frame.width, frame.height) != size:
        raise _invalid_output("encoder frame dimensions changed unexpectedly")
    frame.pts = timestamp
    frame.duration = delay
    frame.time_base = Fraction(1, 1000)
    for packet in stream.encode(frame):
        container.mux(packet)


def _animation_base_frames(
    frames: _FrameSet,
) -> tuple[_FrameSet, tuple[int, ...]]:
    paths: list[Path] = []
    delays: list[int] = []
    identities: list[_FileIdentity] = []
    repeat_counts: list[int] = []
    for path, delay, identity in zip(
        frames.paths, frames.delays_ms, frames.identities, strict=True
    ):
        if paths and path == paths[-1] and identity == identities[-1]:
            repeat_counts[-1] += 1
            continue
        paths.append(path)
        delays.append(delay)
        identities.append(identity)
        repeat_counts.append(1)
    return (
        _FrameSet(tuple(paths), tuple(delays), frames.size, tuple(identities), True),
        tuple(repeat_counts),
    )


def _expand_animation_frames(path: Path, repeat_counts: tuple[int, ...]) -> None:
    if all(repeat_count == 1 for repeat_count in repeat_counts):
        return
    with path.open("rb") as source:
        data = source.read()
    output = bytearray(data[:12])
    position = 12
    frame_index = 0
    while position < len(data):
        size = int.from_bytes(data[position + 4 : position + 8], "little")
        end = position + 8 + size + (size & 1)
        chunk = data[position:end]
        if data[position : position + 4] == b"ANMF":
            for _ in range(repeat_counts[frame_index]):
                output.extend(chunk)
            frame_index += 1
        else:
            output.extend(chunk)
        position = end
    if frame_index != len(repeat_counts):
        raise _invalid_output("encoder returned an incomplete animation")
    output[4:8] = (len(output) - 8).to_bytes(4, "little")
    with path.open("r+b") as destination:
        destination.seek(0)
        destination.write(output)
        destination.truncate()
        destination.flush()
        os.fsync(destination.fileno())


def _remove_last_animation_frame(path: Path) -> None:
    last_frame: int | None = None
    frame_count = 0
    position = 12
    with path.open("rb") as source:
        file_size = _read_riff_header(source)
        while position < file_size:
            tag, size = _read_chunk_header_at(source, position)
            if tag == b"ANMF":
                last_frame = position
                frame_count += 1
            position += 8 + size + (size & 1)
    if frame_count != 2 or last_frame is None:
        raise _invalid_output("encoder returned an incomplete animation")
    with path.open("r+b") as output:
        output.truncate(last_frame)
        output.seek(4)
        output.write((last_frame - 8).to_bytes(4, "little"))
        output.flush()
        os.fsync(output.fileno())


def _rewrite_animation_durations(path: Path, delays_ms: tuple[int, ...]) -> None:
    offsets = _animation_duration_offsets(path)
    if len(offsets) != len(delays_ms):
        raise _invalid_output("encoder returned an incomplete animation")
    alpha_flag_offset, background_offset = _animation_metadata_offsets(path)
    with path.open("r+b") as output:
        # FFmpeg's WebP muxer writes an opaque background hint and may encode
        # the first frame as a cropped rectangle. Pillow honors that hint,
        # turning the transparent canvas around a cutout opaque. Canonicalize
        # the ANIM BGRA field to transparent black before validating pixels.
        output.seek(alpha_flag_offset)
        flags = _read_exact(output, 1)[0]
        output.seek(alpha_flag_offset)
        output.write(bytes((flags | 0x10,)))
        output.seek(background_offset)
        output.write(b"\0\0\0\0")
        for offset, delay in zip(offsets, delays_ms, strict=True):
            output.seek(offset)
            output.write(delay.to_bytes(3, "little"))
        output.flush()
        os.fsync(output.fileno())


def _animation_duration_offsets(path: Path) -> tuple[int, ...]:
    offsets: list[int] = []
    with path.open("rb") as source:
        file_size = _read_riff_header(source)
        position = 12
        while position < file_size:
            source.seek(position)
            tag, size = _read_chunk_header(source)
            payload = position + 8
            end = payload + size
            if end + (size & 1) > file_size:
                raise _invalid_output("encoder returned a truncated RIFF chunk")
            if tag == b"ANMF":
                if size < 16:
                    raise _invalid_output("encoder returned a short ANMF chunk")
                offsets.append(payload + 12)
            position = end + (size & 1)
    return tuple(offsets)


def _animation_metadata_offsets(path: Path) -> tuple[int, int]:
    with path.open("rb") as source:
        file_size = _read_riff_header(source)
        position = 12
        alpha_flag_offset: int | None = None
        while position < file_size:
            tag, size = _read_chunk_header_at(source, position)
            if tag == b"VP8X":
                if size != 10 or alpha_flag_offset is not None:
                    raise _invalid_output("encoder returned invalid VP8X metadata")
                alpha_flag_offset = position + 8
            if tag == b"ANIM":
                if size != 6:
                    raise _invalid_output("encoder returned an invalid ANIM chunk")
                if alpha_flag_offset is None:
                    raise _invalid_output("encoder returned ANIM before VP8X")
                return alpha_flag_offset, position + 8
            position += 8 + size + (size & 1)
    raise _invalid_output("encoder returned animation frames without ANIM metadata")


def _read_chunk_header_at(source: BinaryIO, position: int) -> tuple[bytes, int]:
    source.seek(position)
    return _read_chunk_header(source)


def _parse_riff(source: BinaryIO, actual_size: int) -> _RiffFacts:
    declared_size = _read_riff_header(source)
    if declared_size != actual_size:
        raise _invalid_output("RIFF length does not match the complete file")
    first = _chunk_at(source, 12, actual_size)
    if first[0] == b"VP8L":
        if first[4] != actual_size:
            raise _invalid_output("still WebP must contain a single VP8L chunk")
        width, height, has_alpha = _parse_vp8l_header(source, first[2], first[1])
        _require_zero_padding(source, first[3], first[1])
        return _RiffFacts(width, height, (), 0, has_alpha)
    if first[0] != b"VP8X":
        raise _invalid_output("animated WebP must begin with one VP8X chunk")

    width, height, has_alpha_flag = _parse_vp8x(source, first)
    second = _chunk_at(source, first[4], actual_size)
    if second[0] != b"ANIM":
        if second[0] == b"VP8X":
            raise _invalid_output("animated WebP contains a duplicate VP8X chunk")
        raise _invalid_output("VP8X must be followed by one ANIM chunk")
    if second[1] != 6:
        raise _invalid_output("WebP has an invalid ANIM chunk")
    source.seek(second[2])
    animation_data = _read_exact(source, 6)
    background_has_alpha = animation_data[3] < 255
    loop = int.from_bytes(animation_data[4:6], "little")
    _require_zero_padding(source, second[3], second[1])

    position = second[4]
    delays: list[int] = []
    frame_alpha: list[bool] = []
    while position < actual_size:
        chunk = _chunk_at(source, position, actual_size)
        if chunk[0] == b"ANIM":
            raise _invalid_output("animated WebP contains a duplicate ANIM chunk")
        if chunk[0] == b"VP8X":
            raise _invalid_output("animated WebP contains a duplicate VP8X chunk")
        if chunk[0] != b"ANMF":
            raise _invalid_output("animated WebP may contain only ANMF frames")
        delay, alpha = _parse_anmf(source, chunk, (width, height))
        delays.append(delay)
        frame_alpha.append(alpha)
        if len(delays) > MAX_OUTPUT_FRAMES:
            raise _invalid_output("WebP frame count exceeds 100000")
        position = chunk[4]
    if not delays:
        raise _invalid_output("animated WebP must contain at least one ANMF frame")
    if has_alpha_flag != (background_has_alpha or any(frame_alpha)):
        raise _invalid_output("VP8X alpha flag does not match VP8L frame alpha")
    return _RiffFacts(width, height, tuple(delays), loop, has_alpha_flag)


def _parse_vp8x(
    source: BinaryIO, chunk: tuple[bytes, int, int, int, int]
) -> tuple[int, int, bool]:
    _tag, size, payload, end, _padded_end = chunk
    if size != 10:
        raise _invalid_output("WebP has an invalid VP8X chunk")
    source.seek(payload)
    data = _read_exact(source, 10)
    flags = data[0]
    if flags & 0xC1 or data[1:4] != b"\0\0\0":
        raise _invalid_output("VP8X reserved bits must be zero")
    if not flags & 0x02:
        raise _invalid_output("VP8X animation flag must be set")
    if flags & ~0x12:
        raise _invalid_output("VP8X contains unsupported metadata flags")
    width = int.from_bytes(data[4:7], "little") + 1
    height = int.from_bytes(data[7:10], "little") + 1
    if width * height > (1 << 32) - 1:
        raise _invalid_output("VP8X canvas pixel count exceeds the format limit")
    _require_zero_padding(source, end, size)
    return width, height, bool(flags & 0x10)


def _parse_anmf(
    source: BinaryIO,
    chunk: tuple[bytes, int, int, int, int],
    canvas: tuple[int, int],
) -> tuple[int, bool]:
    _tag, size, payload, end, _padded_end = chunk
    if size < 16:
        raise _invalid_output("WebP has an invalid ANMF chunk")
    source.seek(payload)
    header = _read_exact(source, 16)
    if header[15] & 0xFC:
        raise _invalid_output("ANMF reserved bits must be zero")
    x = int.from_bytes(header[0:3], "little") * 2
    y = int.from_bytes(header[3:6], "little") * 2
    width = int.from_bytes(header[6:9], "little") + 1
    height = int.from_bytes(header[9:12], "little") + 1
    delay = int.from_bytes(header[12:15], "little")
    if delay <= 0:
        raise _invalid_output("ANMF duration must be a positive integer")
    if x + width > canvas[0] or y + height > canvas[1]:
        raise _invalid_output("ANMF frame rectangle exceeds the VP8X canvas")

    nested = _chunk_at(source, payload + 16, end)
    if nested[0] != b"VP8L" or nested[4] != end:
        raise _invalid_output("ANMF must contain exactly one lossless VP8L chunk")
    bitstream_width, bitstream_height, has_alpha = _parse_vp8l_header(
        source, nested[2], nested[1]
    )
    if (bitstream_width, bitstream_height) != (width, height):
        raise _invalid_output("ANMF VP8L dimensions do not match the frame header")
    _require_zero_padding(source, nested[3], nested[1])
    _require_zero_padding(source, end, size)
    return delay, has_alpha


def _parse_vp8l_header(
    source: BinaryIO, payload: int, size: int
) -> tuple[int, int, bool]:
    if size < 5:
        raise _invalid_output("VP8L bitstream is too short")
    source.seek(payload)
    data = _read_exact(source, 5)
    if data[0] != 0x2F:
        raise _invalid_output("VP8L bitstream has an invalid signature")
    bits = int.from_bytes(data[1:5], "little")
    if bits >> 29:
        raise _invalid_output("VP8L reserved version bits must be zero")
    return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, bool(bits & (1 << 28))


def _chunk_at(
    source: BinaryIO, position: int, limit: int
) -> tuple[bytes, int, int, int, int]:
    if position + 8 > limit:
        raise _invalid_output("WebP file is truncated before a chunk header")
    source.seek(position)
    tag, size = _read_chunk_header(source)
    payload = position + 8
    end = payload + size
    padded_end = end + (size & 1)
    if padded_end > limit:
        raise _invalid_output("WebP contains a truncated RIFF chunk")
    return tag, size, payload, end, padded_end


def _require_zero_padding(source: BinaryIO, end: int, size: int) -> None:
    if size & 1:
        source.seek(end)
        if _read_exact(source, 1) != b"\0":
            raise _invalid_output("RIFF padding bytes must be zero")


def _read_riff_header(source: BinaryIO) -> int:
    source.seek(0)
    header = _read_exact(source, 12)
    if header[:4] != b"RIFF" or header[8:] != b"WEBP":
        raise _invalid_output("output does not have a RIFF/WEBP header")
    return int.from_bytes(header[4:8], "little") + 8


def _read_chunk_header(source: BinaryIO) -> tuple[bytes, int]:
    header = _read_exact(source, 8)
    return header[:4], int.from_bytes(header[4:], "little")


def _read_exact(source: BinaryIO, count: int) -> bytes:
    data = source.read(count)
    if len(data) != count:
        raise _invalid_output("WebP file is truncated")
    return data


def _validate_encoded_pixels(
    source_paths: tuple[Path, ...],
    output: Path,
    expected_identities: tuple[_FileIdentity, ...] | None = None,
    rgba_ownership_tracker: RgbaOwnershipTracker | None = None,
) -> None:
    try:
        with _open_pillow(output) as encoded:
            for index, source_path in enumerate(source_paths):
                expected_identity = (
                    expected_identities[index]
                    if expected_identities is not None
                    else None
                )
                _validate_encoded_frame(
                    encoded,
                    index,
                    source_path,
                    expected_identity,
                    rgba_ownership_tracker,
                )
    except AppError:
        raise
    except (OSError, EOFError, ValueError) as error:
        raise _invalid_output(f"cannot compare encoded WebP pixels: {error}") from error


def _validate_encoded_frame(
    encoded: Image.Image,
    index: int,
    source_path: Path,
    expected_identity: _FileIdentity | None,
    rgba_ownership_tracker: RgbaOwnershipTracker | None,
) -> None:
    encoded.seek(index)
    encoded.load()
    if rgba_ownership_tracker is not None:
        rgba_ownership_tracker.register(encoded)
    with _open_stable_binary(source_path, expected_identity) as (
        input_file,
        _opened_identity,
    ):
        with _open_pillow(input_file) as source:
            source.load()
            if rgba_ownership_tracker is not None:
                rgba_ownership_tracker.register(source)
            if encoded.mode == "RGBA":
                difference = ImageChops.difference(source, encoded)
            else:
                converted = encoded.convert("RGBA")
                if rgba_ownership_tracker is not None:
                    rgba_ownership_tracker.register(converted)
                try:
                    difference = ImageChops.difference(source, converted)
                finally:
                    converted.close()
            with difference:
                if rgba_ownership_tracker is not None:
                    rgba_ownership_tracker.register(difference)
                if difference.getbbox(alpha_only=False) is not None:
                    raise _invalid_output(
                        f"encoded frame {index} does not match its RGBA source"
                    )


def _resize_from_sources(
    source_paths: tuple[Path, ...],
    size: tuple[int, int],
    destination: Path,
    rgba_ownership_tracker: RgbaOwnershipTracker | None = None,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> tuple[Path, ...]:
    _raise_if_fit_cancelled(is_cancelled)
    destination.mkdir()
    output_paths: list[Path] = []
    for index, source_path in enumerate(source_paths):
        _raise_if_fit_cancelled(is_cancelled)
        output = destination / f"frame-{index:06d}.png"
        with _open_stable_binary(source_path) as (
            input_file,
            _opened_identity,
        ):
            with _open_pillow(input_file) as source:
                source.load()
                if rgba_ownership_tracker is not None:
                    rgba_ownership_tracker.register(source)
                premultiplied = source.convert("RGBa")
            del source
        try:
            _raise_if_fit_cancelled(is_cancelled)
            resized = premultiplied.resize(size, Image.Resampling.LANCZOS)
        finally:
            premultiplied.close()
            del premultiplied
        try:
            _raise_if_fit_cancelled(is_cancelled)
            rgba = resized.convert("RGBA")
            if rgba_ownership_tracker is not None:
                rgba_ownership_tracker.register(rgba)
        finally:
            resized.close()
            del resized
        try:
            _raise_if_fit_cancelled(is_cancelled)
            rgba.save(output, format="PNG", optimize=False)
            _raise_if_fit_cancelled(is_cancelled)
        finally:
            rgba.close()
            del rgba
        output_paths.append(output)
    return tuple(output_paths)


def _snapshot_frame_set(
    source: _FrameSet,
    destination: Path,
    rgba_ownership_tracker: RgbaOwnershipTracker | None = None,
    *,
    is_cancelled: Callable[[], bool] | None = None,
) -> _FrameSet:
    _raise_if_fit_cancelled(is_cancelled)
    destination.mkdir()
    snapshot_paths: list[Path] = []
    for index, (path, identity) in enumerate(
        zip(source.paths, source.identities, strict=True)
    ):
        _raise_if_fit_cancelled(is_cancelled)
        snapshot = destination / f"frame-{index:06d}.png"
        with _open_stable_binary(path, identity) as (
            input_file,
            _opened_identity,
        ):
            with snapshot.open("xb") as output:
                shutil.copyfileobj(input_file, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
        _raise_if_fit_cancelled(is_cancelled)
        snapshot_paths.append(snapshot)
    if rgba_ownership_tracker is None:
        return _validate_frame_inputs(
            tuple(snapshot_paths),
            source.delays_ms,
            is_cancelled=is_cancelled,
        )
    return _validate_frame_inputs(
        tuple(snapshot_paths),
        source.delays_ms,
        rgba_ownership_tracker,
        is_cancelled=is_cancelled,
    )


def _scaled_dimensions(
    source_size: tuple[int, int], cumulative_scale: Decimal
) -> tuple[int, int]:
    horizontal = FramingPlan(
        source_size=source_size,
        stretch_x=cumulative_scale,
    )
    vertical = FramingPlan(
        source_size=(source_size[1], source_size[0]),
        stretch_x=cumulative_scale,
    )
    return horizontal.output_size[0], vertical.output_size[0]


def _prepare_candidate(
    candidate: Path,
    destination: Path,
    frames: _FrameSet,
    expected_duration_ms: int,
    expected_dimensions: tuple[int, int],
    target_bytes: int,
    rgba_ownership_tracker: RgbaOwnershipTracker | None = None,
) -> Path:
    temporary = _sibling_temporary(destination)
    primary: BaseException | None = None
    try:
        with candidate.open("rb") as encoded, temporary.open("wb") as output:
            shutil.copyfileobj(encoded, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        info = validate_webp(
            temporary,
            expected_frames=len(frames.paths),
            expected_duration_ms=expected_duration_ms,
            rgba_ownership_tracker=rgba_ownership_tracker,
        )
        expected_delays = frames.delays_ms if frames.animated else ()
        if info.delays_ms != expected_delays:
            raise _invalid_output("fitted WebP frame delays changed during promotion")
        if (info.width, info.height) != expected_dimensions:
            raise _invalid_output("fitted WebP dimensions changed during promotion")
        _validate_encoded_pixels(
            frames.paths,
            temporary,
            frames.identities,
            rgba_ownership_tracker=rgba_ownership_tracker,
        )
        if info.file_size > target_bytes:
            raise _impossible_size(
                "validated WebP remains larger than the requested byte target"
            )
        return temporary
    except AppError as error:
        primary = error
        raise
    except OSError as error:
        wrapped = _output_os_error(error, "cannot prepare fitted WebP")
        primary = wrapped
        raise wrapped from error
    except BaseException as error:
        primary = error
        raise
    finally:
        if primary is not None:
            _cleanup_file(temporary, primary)


def _raise_if_fit_cancelled(
    is_cancelled: Callable[[], bool] | None,
) -> None:
    if is_cancelled is not None and is_cancelled():
        raise AppError(
            ErrorCode.JOB_CANCELLED,
            "auto-fit",
            "error.job.cancelled",
            "lossless WebP auto-fit was cancelled at a safe point",
            "none",
        )


def _validate_destination(destination: Path) -> Path:
    destination = _require_path(destination, "destination")
    if destination.exists() and destination.is_dir():
        raise _invalid_output("destination must not be a directory")
    if not destination.parent.is_dir():
        raise _invalid_output("destination parent directory does not exist")
    return destination


def _sibling_temporary(destination: Path) -> Path:
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp.webp",
            dir=destination.parent,
        )
        temporary = _require_path(Path(raw_path), "sibling temporary")
        os.close(descriptor)
        descriptor = None
        return temporary
    except OSError as error:
        wrapped = _output_os_error(error, "cannot create sibling output temporary")
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as retry_error:
                wrapped.add_note(
                    f"additional descriptor cleanup failure: {retry_error}"
                )
        if temporary is not None:
            _cleanup_file(temporary, wrapped)
        raise wrapped from error


def _fsync_file(path: Path) -> None:
    with path.open("rb") as output:
        os.fsync(output.fileno())


def _require_path(value: object, name: str) -> Path:
    if not isinstance(value, Path):
        raise _invalid_output(f"{name} must be a pathlib.Path")
    if not is_local_path_syntax(value):
        raise _invalid_output(f"{name} must use local path syntax")
    return value


@contextmanager
def _open_webp_validation_source(
    source: Path | BinaryIO,
) -> Iterator[tuple[BinaryIO, int]]:
    if isinstance(source, Path):
        path = _require_path(source, "WebP path")
        with _open_stable_binary(path) as (held, identity):
            yield held, identity.size
        return
    if not all(
        callable(getattr(source, method, None))
        for method in ("fileno", "read", "seek", "tell")
    ):
        raise _invalid_output("WebP source must be a path or seekable binary file")
    duplicate_descriptor = -1
    duplicate: BinaryIO | None = None
    try:
        descriptor = source.fileno()
        if type(descriptor) is not int or descriptor < 0:
            raise _invalid_output(
                "open WebP source fileno must return a non-negative integer"
            )
        duplicate_descriptor = os.dup(descriptor)
        opened_stat = os.fstat(duplicate_descriptor)
        before = _identity_from_stat(opened_stat)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise _invalid_output("WebP source must be a regular file")
        position = source.tell()
        duplicate = os.fdopen(duplicate_descriptor, "rb")
        held_descriptor = duplicate_descriptor
        duplicate_descriptor = -1
    except AppError as error:
        if duplicate_descriptor >= 0:
            try:
                os.close(duplicate_descriptor)
            except OSError as cleanup_error:
                error.add_note(
                    f"additional duplicate WebP source cleanup failure: {cleanup_error}"
                )
        raise
    except (OSError, TypeError, ValueError) as error:
        if duplicate_descriptor >= 0:
            try:
                os.close(duplicate_descriptor)
            except OSError as cleanup_error:
                error.add_note(
                    f"additional duplicate WebP source cleanup failure: {cleanup_error}"
                )
        raise _invalid_output(f"cannot inspect open WebP source: {error}") from error
    except BaseException as error:
        if duplicate_descriptor >= 0:
            try:
                os.close(duplicate_descriptor)
            except OSError as cleanup_error:
                error.add_note(
                    f"additional duplicate WebP source cleanup failure: {cleanup_error}"
                )
        raise

    primary: BaseException | None = None
    try:
        yield duplicate, before.size
        if _identity_from_stat(os.fstat(held_descriptor)) != before:
            raise _invalid_output("open WebP source changed during validation")
    except BaseException as error:
        primary = error
        raise
    finally:
        close_error: OSError | None = None
        try:
            duplicate.close()
        except OSError as error:
            close_error = error
            if primary is not None:
                primary.add_note(f"cannot close duplicate WebP source: {error}")
        try:
            source.seek(position)
        except (OSError, ValueError) as error:
            detail = f"cannot restore open WebP source position: {error}"
            if primary is not None:
                primary.add_note(detail)
            else:
                raise _invalid_output(detail) from error
        if primary is None and close_error is not None:
            raise _invalid_output(
                f"cannot close duplicate WebP source: {close_error}"
            ) from close_error


@contextmanager
def _open_stable_binary(
    path: Path, expected: _FileIdentity | None = None
) -> Iterator[tuple[BinaryIO, _FileIdentity]]:
    before = _path_identity(path)
    if expected is not None and before != expected:
        raise _invalid_output(f"input frame changed before it was opened: {path}")
    try:
        input_file = path.open("rb")
    except OSError as error:
        raise _invalid_output(
            f"cannot open local regular file {path}: {error}"
        ) from error
    primary: BaseException | None = None
    try:
        opened = _identity_from_stat(os.fstat(input_file.fileno()))
        after_open = _path_identity(path)
        if opened != before or after_open != before:
            raise _invalid_output(f"input frame changed while it was opened: {path}")
        yield input_file, before
        final_open = _identity_from_stat(os.fstat(input_file.fileno()))
        final_path = _path_identity(path)
        if final_open != before or final_path != before:
            raise _invalid_output(
                f"input frame changed while it was being read: {path}"
            )
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            input_file.close()
        except OSError as close_error:
            if primary is not None:
                primary.add_note(f"additional file cleanup failure: {close_error}")
            else:
                raise _invalid_output(
                    f"cannot close stable local input file {path}: {close_error}"
                ) from close_error


def _path_identity(path: Path) -> _FileIdentity:
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise _invalid_output(
            f"cannot stat local input frame {path}: {error}"
        ) from error
    if stat.S_ISLNK(path_stat.st_mode):
        raise _invalid_output(f"input frame must not be a symlink: {path}")
    if not stat.S_ISREG(path_stat.st_mode):
        raise _invalid_output(f"frame is not a regular file: {path}")
    return _identity_from_stat(path_stat)


def _identity_from_stat(path_stat: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        device=path_stat.st_dev,
        inode=path_stat.st_ino,
        size=path_stat.st_size,
        modified_ns=path_stat.st_mtime_ns,
        changed_ns=path_stat.st_ctime_ns,
    )


@contextmanager
def _open_pillow(source: Path | BinaryIO) -> Iterator[Image.Image]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                yield image
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as error:
        raise ValidationError(
            ErrorCode.INVALID_FINAL_DIMENSIONS,
            "webp",
            f"Pillow rejected an image outside the configured pixel policy: {error}",
        ) from error


def _cleanup_file(path: Path, primary: BaseException | None) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        _handle_cleanup_failure(primary, "inspect temporary file", path, error)
        return
    try:
        path.unlink()
    except OSError as error:
        _handle_cleanup_failure(primary, "remove temporary file", path, error)


def _cleanup_tree(path: Path, primary: BaseException | None) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        _handle_cleanup_failure(primary, "inspect temporary tree", path, error)
        return
    try:
        shutil.rmtree(path)
    except OSError as error:
        _handle_cleanup_failure(primary, "remove temporary tree", path, error)


def _handle_cleanup_failure(
    primary: BaseException | None,
    action: str,
    path: Path,
    error: OSError,
) -> None:
    detail = f"cleanup could not {action} {path}: {error}"
    if primary is not None:
        primary.add_note(detail)
        return
    raise _invalid_output(detail) from error


def _invalid_output(detail: str) -> AppError:
    return AppError(
        ErrorCode.INVALID_OUTPUT,
        "webp",
        "error.webp.invalid-output",
        detail,
        "choose-output",
    )


def _output_os_error(error: OSError, detail: str) -> AppError:
    if error.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}:
        suffix = "disk quota or free space exhausted"
        retry_action = "free-disk-space"
    elif error.errno in {
        errno.EACCES,
        errno.EPERM,
        getattr(errno, "EROFS", errno.EACCES),
    }:
        suffix = "output location is not writable"
        retry_action = "choose-writable-output"
    else:
        suffix = f"{type(error).__name__}: {error}"
        retry_action = "retry-output"
    return AppError(
        ErrorCode.INVALID_OUTPUT,
        "webp",
        "error.webp.invalid-output",
        f"{detail}: {suffix}",
        retry_action,
    )


def _impossible_size(detail: str) -> AppError:
    return AppError(
        ErrorCode.IMPOSSIBLE_SIZE,
        "auto-fit",
        "error.webp.impossible-size",
        detail,
        "increase-size-limit",
    )
