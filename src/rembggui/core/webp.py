"""Bounded-memory lossless WebP encoding and output validation."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO, cast

import av
import numpy as np
from PIL import Image, ImageChops

from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.geometry import FramingPlan, solve_proportional_scale
from rembggui.core.specs import MIN_FINAL_DIMENSION, FramingSpec
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

    @property
    def encoded_duration_ms(self) -> int:
        return sum(self.delays_ms) if len(self.paths) > 1 else 0


@dataclass(frozen=True)
class _RiffFacts:
    frames: int
    duration_ms: int
    loop: int
    lossless: bool
    has_alpha_flag: bool


def encode_lossless_webp(
    frame_paths: Sequence[Path],
    delays_ms: Sequence[int],
    destination: Path,
) -> EncodeSummary:
    """Encode PNG-backed RGBA frames and atomically replace *destination*."""
    frames = _validate_frame_inputs(frame_paths, delays_ms)
    destination = _validate_destination(destination)
    temporary = _sibling_temporary(destination)
    try:
        if len(frames.paths) == 1:
            _encode_still(frames.paths[0], temporary)
        else:
            _encode_animation(frames, temporary)
        _fsync_file(temporary)
        info = validate_webp(
            temporary,
            expected_frames=len(frames.paths),
            expected_duration_ms=frames.encoded_duration_ms,
        )
        if (info.width, info.height) != frames.size:
            raise _invalid_output("encoded dimensions do not match the input frames")
        _validate_encoded_pixels(frames.paths, temporary)
        os.replace(temporary, destination)
    except AppError:
        raise
    except (OSError, ValueError, av.FFmpegError) as error:
        raise _invalid_output(f"lossless WebP encoding failed: {error}") from error
    finally:
        _unlink_if_present(temporary)

    return EncodeSummary(
        destination=destination,
        width=info.width,
        height=info.height,
        frames=info.frames,
        duration_ms=info.duration_ms,
        file_size=info.file_size,
    )


def validate_webp(
    path: Path,
    expected_frames: int,
    expected_duration_ms: int,
) -> WebPInfo:
    """Validate WebP structure, timing, dimensions, losslessness, and decoding."""
    path = _require_path(path, "WebP path")
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
        file_size = path.stat().st_size
        if not stat.S_ISREG(path.stat().st_mode):
            raise _invalid_output("WebP path must be a regular file")
        if file_size >= _RIFF_LIMIT:
            raise _invalid_output("WebP output must be smaller than 4 GiB")
        riff = _parse_riff(path, file_size)
        with Image.open(path) as image:
            if image.format != "WEBP":
                raise _invalid_output("output is not a WebP image")
            width, height = FramingSpec().validate_final_dimensions(*image.size)
            decoded_frames = getattr(image, "n_frames", 1)
            has_alpha = False
            for index in range(decoded_frames):
                image.seek(index)
                image.load()
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
) -> Path:
    """Fit a validated WebP to a byte target using at most twelve encodes."""
    source = _validate_frame_inputs(source_frame_paths, delays_ms)
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
        raise _invalid_output(f"cannot create auto-fit workspace: {error}") from error

    cumulative_scale = Decimal(1)
    current_paths = source.paths
    active_scaled_dir: Path | None = None
    try:
        for attempt in range(_MAX_FIT_ENCODINGS):
            candidate = scratch / f"candidate-{attempt:02d}.webp"
            summary = encode_lossless_webp(current_paths, source.delays_ms, candidate)
            if summary.file_size <= target_bytes:
                _promote_candidate(candidate, destination, source, target_bytes)
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
            if (
                next_size == (MIN_FINAL_DIMENSION, MIN_FINAL_DIMENSION)
                and (
                    summary.width,
                    summary.height,
                )
                == next_size
            ):
                break
            _unlink_if_present(candidate)
            if active_scaled_dir is not None:
                try:
                    shutil.rmtree(active_scaled_dir)
                except OSError as error:
                    raise _invalid_output(
                        f"cannot clean prior auto-fit frames: {error}"
                    ) from error
            scaled_dir = scratch / f"scaled-{attempt + 1:02d}"
            current_paths = _resize_from_sources(source.paths, next_size, scaled_dir)
            active_scaled_dir = scaled_dir
            cumulative_scale = next_scale

        raise _impossible_size(
            "lossless WebP cannot meet the requested size within 12 encodes and "
            "the 128 px minimum"
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _validate_frame_inputs(
    frame_paths: Sequence[Path], delays_ms: Sequence[int]
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
    expected_size: tuple[int, int] | None = None
    for raw_path in frame_paths:
        path = _require_path(raw_path, "frame path")
        try:
            path_stat = path.stat()
            if not stat.S_ISREG(path_stat.st_mode):
                raise _invalid_output(f"frame is not a regular file: {path}")
            with path.open("rb"):
                pass
            with Image.open(path) as image:
                if image.format != "PNG" or image.mode != "RGBA":
                    raise _invalid_output(f"frame must be an RGBA PNG: {path}")
                size = FramingSpec().validate_final_dimensions(*image.size)
                if expected_size is None:
                    expected_size = size
                elif size != expected_size:
                    raise _invalid_output(
                        "all input frames must have identical dimensions"
                    )
                image.verify()
        except AppError:
            raise
        except (OSError, EOFError, ValueError, SyntaxError) as error:
            raise _invalid_output(
                f"unreadable or corrupt frame {path}: {error}"
            ) from error
        paths.append(path)

    assert expected_size is not None
    return _FrameSet(tuple(paths), delays, expected_size)


def _encode_still(source: Path, destination: Path) -> None:
    with Image.open(source) as image:
        image.load()
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


def _encode_animation(frames: _FrameSet, destination: Path) -> None:
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
        timestamp = 0
        for path, delay in zip(frames.paths, frames.delays_ms, strict=True):
            with Image.open(path) as image:
                image.load()
                frame = av.VideoFrame.from_ndarray(np.asarray(image), format="rgba")
            frame.pts = timestamp
            frame.duration = delay
            frame.time_base = Fraction(1, 1000)
            timestamp += delay
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()
    _rewrite_animation_durations(destination, frames.delays_ms)


def _rewrite_animation_durations(path: Path, delays_ms: tuple[int, ...]) -> None:
    offsets = _animation_duration_offsets(path)
    if len(offsets) != len(delays_ms):
        raise _invalid_output("encoder returned an incomplete animation")
    with path.open("r+b") as output:
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


def _parse_riff(path: Path, actual_size: int) -> _RiffFacts:
    frames = 0
    duration_ms = 0
    loop: int | None = None
    top_level_lossless = False
    all_animation_frames_lossless = True
    has_alpha_flag = False
    animated_flag = False
    with path.open("rb") as source:
        declared_size = _read_riff_header(source)
        if declared_size != actual_size:
            raise _invalid_output("RIFF length does not match the complete file")
        position = 12
        while position < actual_size:
            source.seek(position)
            tag, size = _read_chunk_header(source)
            payload = position + 8
            end = payload + size
            padded_end = end + (size & 1)
            if padded_end > actual_size:
                raise _invalid_output("WebP contains a truncated RIFF chunk")
            if tag == b"VP8X":
                if size != 10:
                    raise _invalid_output("WebP has an invalid VP8X chunk")
                flags = _read_exact(source, 1)[0]
                animated_flag = bool(flags & 0x02)
                has_alpha_flag = bool(flags & 0x10)
            elif tag == b"ANIM":
                if size != 6:
                    raise _invalid_output("WebP has an invalid ANIM chunk")
                data = _read_exact(source, 6)
                loop = int.from_bytes(data[4:6], "little")
            elif tag == b"ANMF":
                if size < 16:
                    raise _invalid_output("WebP has an invalid ANMF chunk")
                header = _read_exact(source, 16)
                duration_ms += int.from_bytes(header[12:15], "little")
                frames += 1
                if not _animation_frame_is_lossless(source, payload + 16, end):
                    all_animation_frames_lossless = False
            elif tag == b"VP8L":
                top_level_lossless = True
            position = padded_end
        if position != actual_size:
            raise _invalid_output("WebP RIFF chunks do not consume the complete file")

    if frames:
        if not animated_flag or loop is None:
            raise _invalid_output("animated WebP is missing animation control data")
        return _RiffFacts(
            frames, duration_ms, loop, all_animation_frames_lossless, has_alpha_flag
        )
    if animated_flag or loop is not None:
        raise _invalid_output("WebP animation control data has no frames")
    if not top_level_lossless:
        raise _invalid_output("still WebP is missing a lossless VP8L image")
    return _RiffFacts(1, 0, 0, True, has_alpha_flag)


def _animation_frame_is_lossless(
    source: BinaryIO, position: int, frame_end: int
) -> bool:
    lossless = False
    while position < frame_end:
        source.seek(position)
        tag, size = _read_chunk_header(source)
        end = position + 8 + size
        padded_end = end + (size & 1)
        if padded_end > frame_end:
            raise _invalid_output("WebP contains a truncated animation frame")
        if tag == b"VP8 ":
            return False
        if tag == b"VP8L":
            lossless = True
        position = padded_end
    if position != frame_end:
        raise _invalid_output("WebP animation frame has invalid chunk padding")
    return lossless


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


def _validate_encoded_pixels(source_paths: tuple[Path, ...], output: Path) -> None:
    try:
        with Image.open(output) as encoded:
            for index, source_path in enumerate(source_paths):
                encoded.seek(index)
                with Image.open(source_path) as source:
                    source.load()
                    if encoded.mode == "RGBA":
                        difference = ImageChops.difference(source, encoded)
                    else:
                        converted = encoded.convert("RGBA")
                        try:
                            difference = ImageChops.difference(source, converted)
                        finally:
                            converted.close()
                    with difference:
                        if difference.getbbox(alpha_only=False) is not None:
                            raise _invalid_output(
                                f"encoded frame {index} does not match its RGBA source"
                            )
    except AppError:
        raise
    except (OSError, EOFError, ValueError) as error:
        raise _invalid_output(f"cannot compare encoded WebP pixels: {error}") from error


def _resize_from_sources(
    source_paths: tuple[Path, ...], size: tuple[int, int], destination: Path
) -> tuple[Path, ...]:
    destination.mkdir()
    output_paths: list[Path] = []
    for index, source_path in enumerate(source_paths):
        output = destination / f"frame-{index:06d}.png"
        with Image.open(source_path) as source:
            source.load()
            premultiplied = source.convert("RGBa")
        try:
            resized = premultiplied.resize(size, Image.Resampling.LANCZOS)
        finally:
            premultiplied.close()
        try:
            rgba = resized.convert("RGBA")
        finally:
            resized.close()
        try:
            rgba.save(output, format="PNG", optimize=False)
        finally:
            rgba.close()
        output_paths.append(output)
    return tuple(output_paths)


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


def _promote_candidate(
    candidate: Path,
    destination: Path,
    source: _FrameSet,
    target_bytes: int,
) -> None:
    temporary = _sibling_temporary(destination)
    try:
        with candidate.open("rb") as encoded, temporary.open("wb") as output:
            shutil.copyfileobj(encoded, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        info = validate_webp(
            temporary,
            expected_frames=len(source.paths),
            expected_duration_ms=source.encoded_duration_ms,
        )
        if info.file_size > target_bytes:
            raise _impossible_size(
                "validated WebP remains larger than the requested byte target"
            )
        os.replace(temporary, destination)
    except AppError:
        raise
    except OSError as error:
        raise _invalid_output(
            f"cannot atomically promote fitted WebP: {error}"
        ) from error
    finally:
        _unlink_if_present(temporary)


def _validate_destination(destination: Path) -> Path:
    destination = _require_path(destination, "destination")
    if destination.exists() and destination.is_dir():
        raise _invalid_output("destination must not be a directory")
    if not destination.parent.is_dir():
        raise _invalid_output("destination parent directory does not exist")
    return destination


def _sibling_temporary(destination: Path) -> Path:
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp.webp",
            dir=destination.parent,
        )
        os.close(descriptor)
        return Path(raw_path)
    except OSError as error:
        raise _invalid_output(
            f"cannot create sibling output temporary: {error}"
        ) from error


def _fsync_file(path: Path) -> None:
    with path.open("rb") as output:
        os.fsync(output.fileno())


def _require_path(value: object, name: str) -> Path:
    if not isinstance(value, Path):
        raise _invalid_output(f"{name} must be a pathlib.Path")
    return value


def _unlink_if_present(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _invalid_output(detail: str) -> AppError:
    return AppError(
        ErrorCode.INVALID_OUTPUT,
        "webp",
        "error.webp.invalid-output",
        detail,
        "choose-output",
    )


def _impossible_size(detail: str) -> AppError:
    return AppError(
        ErrorCode.IMPOSSIBLE_SIZE,
        "auto-fit",
        "error.webp.impossible-size",
        detail,
        "increase-size-limit",
    )
