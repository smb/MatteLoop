"""Private-container PyAV source probing and timestamp-authoritative decoding."""

from __future__ import annotations

import math
import os
import struct
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import av
from PIL import Image

from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.specs import is_local_path_syntax

MAX_SOURCE_WIDTH = 3840
MAX_SOURCE_HEIGHT = 2160
MAX_SOURCE_FPS = Fraction(60)
MAX_SOURCE_DURATION = Fraction(10 * 60)

_HDR_TRANSFERS = {16, 18}  # SMPTE ST 2084 (PQ), ARIB STD-B67 (HLG)
_HDR_PRIMARIES = {9}  # BT.2020 is outside the V1 SDR/sRGB input envelope.
_HDR_SIDE_DATA = {
    "CONTENT_LIGHT_LEVEL",
    "DOVI_METADATA",
    "DOVI_RPU_BUFFER",
    "DYNAMIC_HDR_PLUS",
    "DYNAMIC_HDR_VIVID",
    "MASTERING_DISPLAY_METADATA",
}

CancelCheck = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """Validated presentation metadata in oriented, square-pixel coordinates."""

    path: Path
    width: int
    height: int
    duration: Fraction
    time_base: Fraction
    average_rate: Fraction | None
    base_rate: Fraction | None
    guessed_rate: Fraction | None
    frame_count: int | None
    rotation: int
    pixel_aspect: Fraction
    coded_width: int
    coded_height: int
    pixel_format: str
    color_range: str
    color_space: str
    color_primaries: str
    color_transfer: str


@dataclass(frozen=True, slots=True)
class DecodedFrame:
    """One normalized display frame and its exact source presentation time."""

    image: Image.Image
    requested_timestamp: Fraction
    actual_pts: Fraction
    request_id: int


def probe_source(path: Path | str) -> SourceInfo:
    """Probe one local source using an InputContainer owned only by this call."""
    source = _validate_source_path(path)
    try:
        with av.open(str(source), mode="r") as container:
            stream = _video_stream(container)
            first_frame = _first_decoded_frame(container, stream)
            return _source_info(source, container, stream, first_frame)
    except AppError:
        raise
    except (OSError, ValueError, av.FFmpegError) as error:
        raise _source_error(
            ErrorCode.SOURCE_CORRUPT,
            "source.probe.corrupt",
            f"PyAV could not probe or decode the source: {error}",
            "choose-another-file",
        ) from error


def decode_frame(
    path: Path | str,
    timestamp: Fraction,
    request_id: int,
    *,
    is_cancelled: CancelCheck | None = None,
) -> DecodedFrame:
    """Decode the frame whose half-open presentation interval owns ``timestamp``.

    Requests before the first valid PTS select the first frame; requests after the
    final valid PTS select the final frame. A frame beginning exactly at the
    request timestamp wins. Cancellation is cooperative between native decode
    calls, because an in-progress codec call cannot safely be interrupted.
    """
    source = _validate_source_path(path)
    if not isinstance(timestamp, Fraction):
        raise _source_error(
            ErrorCode.SOURCE_CORRUPT,
            "source.decode.invalid-timestamp",
            "timestamp must be a Fraction",
            "reload-source",
        )
    if not isinstance(request_id, int) or isinstance(request_id, bool):
        raise _source_error(
            ErrorCode.SOURCE_CORRUPT,
            "source.decode.invalid-request",
            "request_id must be an integer",
            "reload-source",
        )
    _raise_if_cancelled(is_cancelled)
    try:
        with av.open(str(source), mode="r") as container:
            stream = _video_stream(container)
            metadata_frame = _first_decoded_frame(container, stream, is_cancelled)
            source_info = _source_info(source, container, stream, metadata_frame)
            _seek_for_timestamp(container, stream, timestamp, source_info.duration)
            candidate, _ = _frame_at_timestamp(
                container, stream, timestamp, is_cancelled
            )
            actual_pts = _frame_timestamp(candidate)
            image = _normalized_image(candidate, stream, metadata_frame)
            return DecodedFrame(image, timestamp, actual_pts, request_id)
    except AppError:
        raise
    except (OSError, ValueError, av.FFmpegError) as error:
        raise _source_error(
            ErrorCode.SOURCE_CORRUPT,
            "source.decode.failed",
            f"PyAV or Pillow could not decode the requested frame: {error}",
            "reload-source",
        ) from error


def _validate_source_path(value: Path | str) -> Path:
    if not isinstance(value, (Path, str)):
        raise _source_error(
            ErrorCode.SOURCE_NOT_LOCAL,
            "source.path.invalid",
            "source path must be a local filesystem path",
            "choose-local-file",
        )
    source = Path(value)
    if not is_local_path_syntax(source):
        raise _source_error(
            ErrorCode.SOURCE_NOT_LOCAL,
            "source.path.non-local",
            "network paths, URIs, and live inputs are unsupported",
            "choose-local-file",
        )
    try:
        if not source.is_file() or not os.access(source, os.R_OK):
            raise OSError("path is not a readable regular file")
    except OSError as error:
        raise _source_error(
            ErrorCode.SOURCE_UNREADABLE,
            "source.path.unreadable",
            "source must be an existing readable local regular file",
            "choose-another-file",
        ) from error
    return source


def _video_stream(container: Any) -> Any:
    streams = tuple(container.streams.video)
    if not streams:
        raise _source_error(
            ErrorCode.SOURCE_NO_VIDEO,
            "source.probe.no-video",
            "source contains no video stream",
            "choose-another-file",
        )
    return streams[0]


def _decoded_frames(container: Any, stream: Any) -> Iterator[Any]:
    try:
        yield from container.decode(stream)
    except (OSError, ValueError, av.FFmpegError) as error:
        raise _source_error(
            ErrorCode.SOURCE_CORRUPT,
            "source.decode.corrupt",
            f"video packets could not be decoded: {error}",
            "choose-another-file",
        ) from error


def _first_decoded_frame(
    container: Any, stream: Any, is_cancelled: CancelCheck | None = None
) -> Any:
    for frame in _decoded_frames(container, stream):
        _raise_if_cancelled(is_cancelled)
        if frame.pts is not None and frame.time_base is not None:
            return frame
    raise _source_error(
        ErrorCode.SOURCE_CORRUPT,
        "source.probe.no-frames",
        "video stream contains no decodable timestamped frames",
        "choose-another-file",
    )


def _seek_for_timestamp(
    container: Any, stream: Any, timestamp: Fraction, duration: Fraction
) -> None:
    time_base = Fraction(stream.time_base)
    latest_seek = max(Fraction(0), duration - time_base)
    seek_timestamp = min(max(timestamp, Fraction(0)), latest_seek)
    offset = seek_timestamp.numerator * time_base.denominator // (
        seek_timestamp.denominator * time_base.numerator
    )
    try:
        container.seek(offset, stream=stream, backward=True, any_frame=False)
    except (OSError, ValueError, av.FFmpegError) as error:
        raise _source_error(
            ErrorCode.SOURCE_CORRUPT,
            "source.decode.seek-failed",
            f"local video could not seek to the requested timestamp: {error}",
            "convert-source",
        ) from error


def _frame_at_timestamp(
    container: Any,
    stream: Any,
    timestamp: Fraction,
    is_cancelled: CancelCheck | None,
) -> tuple[Any, Any]:
    candidate = None
    first_frame = None
    # The caller seeks backward to a keyframe, never by frame/fps arithmetic.
    for frame in _decoded_frames(container, stream):
        _raise_if_cancelled(is_cancelled)
        if frame.pts is None or frame.time_base is None:
            continue
        if first_frame is None:
            first_frame = frame
        frame_pts = _frame_timestamp(frame)
        if frame_pts <= timestamp:
            candidate = frame
            continue
        # Decoding through the first later PTS establishes the candidate's
        # half-open presentation interval. Before-first clamps to this frame.
        if candidate is None:
            candidate = frame
        break
    if first_frame is None or candidate is None:
        if first_frame is not None:
            candidate = first_frame
        else:
            raise _source_error(
                ErrorCode.SOURCE_CORRUPT,
                "source.decode.no-frames",
                "video stream contains no decodable timestamped frames",
                "choose-another-file",
            )
    return candidate, first_frame


def _source_info(source: Path, container: Any, stream: Any, frame: Any) -> SourceInfo:
    coded_width = int(stream.width)
    coded_height = int(stream.height)
    if coded_width <= 0 or coded_height <= 0:
        raise _source_error(
            ErrorCode.SOURCE_CORRUPT,
            "source.probe.dimensions",
            "video stream has invalid dimensions",
            "choose-another-file",
        )
    time_base = _fraction_or_none(stream.time_base)
    if time_base is None or time_base <= 0:
        raise _source_error(
            ErrorCode.SOURCE_CORRUPT,
            "source.probe.time-base",
            "video stream has no valid time base",
            "choose-another-file",
        )
    duration = _duration(container, stream, time_base)
    if duration <= 0:
        raise _source_error(
            ErrorCode.SOURCE_ZERO_DURATION,
            "source.probe.zero-duration",
            "video duration must be positive",
            "choose-another-file",
        )
    if duration > MAX_SOURCE_DURATION:
        raise _source_error(
            ErrorCode.SOURCE_DURATION_UNSUPPORTED,
            "source.probe.too-long",
            "video duration exceeds the 10 minute V1 limit",
            "trim-or-convert-source",
        )

    average_rate = _fraction_or_none(stream.average_rate)
    base_rate = _fraction_or_none(getattr(stream, "base_rate", None))
    guessed_rate = _fraction_or_none(getattr(stream, "guessed_rate", None))
    validation_rates = tuple(
        rate
        for rate in (average_rate, guessed_rate)
        if rate is not None and rate > 0
    )
    validation_rate = max(validation_rates) if validation_rates else base_rate
    if validation_rate is not None and validation_rate > MAX_SOURCE_FPS:
        raise _source_error(
            ErrorCode.SOURCE_FPS_UNSUPPORTED,
            "source.probe.high-frame-rate",
            "source frame rate exceeds the 60 fps V1 limit",
            "convert-source-to-60fps",
        )

    pixel_aspect = _pixel_aspect(stream)
    rotation = _rotation(stream, frame)
    width, height = _display_dimensions(
        coded_width, coded_height, pixel_aspect, rotation
    )
    if max(width, height) > MAX_SOURCE_WIDTH or min(width, height) > MAX_SOURCE_HEIGHT:
        raise _source_error(
            ErrorCode.SOURCE_DIMENSIONS_UNSUPPORTED,
            "source.probe.too-large",
            "oriented square-pixel dimensions exceed 3840×2160",
            "resize-source",
        )

    codec = stream.codec_context
    pixel_format = _pixel_format(frame, codec)
    color_range = _metadata_name(getattr(frame, "color_range", None))
    color_space = _metadata_name(getattr(frame, "colorspace", None))
    color_primaries = _metadata_name(getattr(codec, "color_primaries", None))
    color_transfer = _metadata_name(getattr(codec, "color_trc", None))
    _validate_sdr_8bit(frame, codec, pixel_format)

    frames = int(stream.frames) if getattr(stream, "frames", 0) > 0 else None
    return SourceInfo(
        path=source,
        width=width,
        height=height,
        duration=duration,
        time_base=time_base,
        average_rate=average_rate,
        base_rate=base_rate,
        guessed_rate=guessed_rate,
        frame_count=frames,
        rotation=rotation,
        pixel_aspect=pixel_aspect,
        coded_width=coded_width,
        coded_height=coded_height,
        pixel_format=pixel_format,
        color_range=color_range,
        color_space=color_space,
        color_primaries=color_primaries,
        color_transfer=color_transfer,
    )


def _duration(container: Any, stream: Any, time_base: Fraction) -> Fraction:
    if stream.duration is not None:
        return Fraction(stream.duration) * time_base
    if container.duration is not None:
        return Fraction(container.duration, int(av.time_base))
    return Fraction(0)


def _frame_timestamp(frame: Any) -> Fraction:
    return Fraction(frame.pts) * Fraction(frame.time_base)


def _pixel_aspect(stream: Any) -> Fraction:
    aspect = _fraction_or_none(getattr(stream, "sample_aspect_ratio", None))
    if aspect is None or aspect <= 0:
        return Fraction(1)
    return aspect


def _display_dimensions(
    coded_width: int,
    coded_height: int,
    pixel_aspect: Fraction,
    rotation: int,
) -> tuple[int, int]:
    square_width = max(1, _round_fraction(Fraction(coded_width) * pixel_aspect))
    square_height = coded_height
    if rotation in (90, 270):
        return square_height, square_width
    return square_width, square_height


def _rotation(stream: Any, frame: Any) -> int:
    metadata_rotation = _rotation_from_metadata(getattr(stream, "metadata", {}))
    if metadata_rotation:
        return metadata_rotation
    return _rotation_from_display_matrix(frame)


def _rotation_from_metadata(metadata: Mapping[str, object]) -> int:
    """Return CCW presentation degrees for legacy clockwise rotate metadata."""
    raw = next(
        (value for key, value in metadata.items() if key.casefold() == "rotate"),
        None,
    )
    if raw is None:
        return 0
    try:
        degrees = float(str(raw))
    except (TypeError, ValueError):
        return 0
    return _normalized_quarter_turn(-degrees)


def _rotation_from_display_matrix(frame: Any) -> int:
    for side_data in getattr(frame, "side_data", ()):
        if getattr(getattr(side_data, "type", None), "name", "") != "DISPLAYMATRIX":
            continue
        raw = bytes(side_data)
        if len(raw) < 36:
            return 0
        matrix = struct.unpack("=9i", raw[:36])
        # Match av_display_rotation_get: the matrix coefficients use the
        # opposite sign from Pillow's counter-clockwise transpose operations.
        degrees = math.degrees(math.atan2(-matrix[1], matrix[0]))
        return _normalized_quarter_turn(degrees)
    return 0


def _normalized_quarter_turn(degrees: float) -> int:
    normalized = degrees % 360
    nearest = int(math.floor(normalized / 90 + 0.5)) * 90 % 360
    if math.isclose(normalized, nearest, abs_tol=0.01) or math.isclose(
        abs(normalized - nearest), 360, abs_tol=0.01
    ):
        return nearest
    return 0


def _validate_sdr_8bit(frame: Any, codec: Any, pixel_format: str) -> None:
    components = tuple(getattr(frame.format, "components", ()))
    component_depths = [int(component.bits) for component in components]
    if (
        any(depth > 8 for depth in component_depths)
        or _pixel_format_is_high_depth(pixel_format)
    ):
        raise _source_error(
            ErrorCode.SOURCE_HDR_UNSUPPORTED,
            "source.probe.high-bit-depth",
            "10-bit and higher-depth video is unsupported in V1",
            "convert-source-to-8bit-srgb",
        )
    transfer = _metadata_int(getattr(codec, "color_trc", None))
    primaries = _metadata_int(getattr(codec, "color_primaries", None))
    side_data_names = {
        getattr(getattr(item, "type", None), "name", "")
        for item in getattr(frame, "side_data", ())
    }
    if (
        transfer in _HDR_TRANSFERS
        or primaries in _HDR_PRIMARIES
        or side_data_names & _HDR_SIDE_DATA
    ):
        raise _source_error(
            ErrorCode.SOURCE_HDR_UNSUPPORTED,
            "source.probe.hdr",
            "HDR and wide-gamut BT.2020 video is unsupported in V1",
            "convert-source-to-8bit-srgb",
        )


def _pixel_format(frame: Any, codec: Any) -> str:
    frame_format = getattr(getattr(frame, "format", None), "name", None)
    if isinstance(frame_format, str) and frame_format:
        return frame_format
    codec_format = getattr(codec, "pix_fmt", None)
    return codec_format if isinstance(codec_format, str) else "unknown"


def _pixel_format_is_high_depth(name: str) -> bool:
    lowered = name.lower()
    return any(token in lowered for token in ("p9", "p10", "p12", "p14", "p16"))


def _normalized_image(frame: Any, stream: Any, metadata_frame: Any) -> Image.Image:
    try:
        image = frame.to_image().convert("RGBA")
        pixel_aspect = _pixel_aspect(stream)
        if pixel_aspect != 1:
            display_width = max(
                1, _round_fraction(Fraction(image.width) * pixel_aspect)
            )
            image = image.resize(
                (display_width, image.height), Image.Resampling.LANCZOS
            )
        rotation = _rotation(stream, metadata_frame)
        transpose = {
            90: Image.Transpose.ROTATE_90,
            180: Image.Transpose.ROTATE_180,
            270: Image.Transpose.ROTATE_270,
        }.get(rotation)
        if transpose is not None:
            image = image.transpose(transpose)
        return cast(Image.Image, image)
    except (
        OSError,
        TypeError,
        ValueError,
        RuntimeError,
        MemoryError,
        av.FFmpegError,
    ) as error:
        raise _source_error(
            ErrorCode.SOURCE_CORRUPT,
            "source.decode.color-conversion",
            f"frame could not be normalized to sRGB RGBA: {error}",
            "convert-source-to-8bit-srgb",
        ) from error


def _fraction_or_none(value: object) -> Fraction | None:
    if value is None:
        return None
    try:
        result = Fraction(str(value))
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return result


def _metadata_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        result = int(str(value))
    except (TypeError, ValueError):
        return None
    return result


def _metadata_name(value: object) -> str:
    if value is None:
        return "unspecified"
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name.lower()
    numeric = _metadata_int(value)
    return str(numeric) if numeric is not None else str(value)


def _round_fraction(value: Fraction) -> int:
    quotient, remainder = divmod(value.numerator, value.denominator)
    return quotient + int(remainder * 2 >= value.denominator)


def _raise_if_cancelled(is_cancelled: CancelCheck | None) -> None:
    if is_cancelled is not None and is_cancelled():
        raise _source_error(
            ErrorCode.JOB_CANCELLED,
            "source.decode.cancelled",
            "frame decode was cancelled between native decode calls",
            "none",
        )


def _source_error(
    code: ErrorCode,
    message_key: str,
    technical_detail: str,
    retry_action: str,
) -> AppError:
    return AppError(
        code=code,
        stage="source",
        message_key=message_key,
        technical_detail=technical_detail,
        retry_action=retry_action,
    )
