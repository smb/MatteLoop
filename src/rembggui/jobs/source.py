"""Private-container PyAV source probing and timestamp-authoritative decoding."""

from __future__ import annotations

import math
import os
import stat
import struct
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import av
from av.video.reformatter import ColorRange, Colorspace, VideoReformatter
from PIL import Image

from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.specs import is_local_path_syntax

MAX_SOURCE_WIDTH = 3840
MAX_SOURCE_HEIGHT = 2160
MAX_SOURCE_FPS = Fraction(60)
MAX_SOURCE_DURATION = Fraction(10 * 60)
MAX_TIMELINE_DECODED_FRAMES = 36_002
SUPPORTED_CONTAINER_SUFFIXES = frozenset({".mp4", ".mov", ".webm", ".mkv"})

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
class SourceRevision:
    """Immutable identity for one regular-file revision."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """Validated presentation metadata in oriented, square-pixel coordinates."""

    path: Path
    revision: SourceRevision
    width: int
    height: int
    duration: Fraction
    time_base: Fraction
    average_rate: Fraction | None
    base_rate: Fraction | None
    guessed_rate: Fraction | None
    peak_rate: Fraction
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
    source_revision: SourceRevision


def probe_source(path: Path | str) -> SourceInfo:
    """Probe one local source using an InputContainer owned only by this call."""
    source = _validate_source_path(path)
    try:
        with _input_container(source, None) as (container, revision):
            _validate_container_format(source, container)
            stream, first_frame = _decodable_video_stream(container)
            return _source_info(source, revision, container, stream, first_frame)
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
    expected_revision: SourceRevision | None = None,
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
    if timestamp < 0:
        raise _source_error(
            ErrorCode.SOURCE_CORRUPT,
            "source.decode.negative-timestamp",
            "public presentation timestamps must be non-negative",
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
        with _input_container(source, expected_revision) as (container, revision):
            _validate_container_format(source, container)
            stream, metadata_frame = _decodable_video_stream(container, is_cancelled)
            source_info = _source_info(
                source,
                revision,
                container,
                stream,
                metadata_frame,
                is_cancelled,
            )
            presentation_origin = _presentation_origin(
                stream, metadata_frame, source_info.time_base
            )
            _seek_for_timestamp(
                container,
                stream,
                timestamp,
                presentation_origin,
                source_info.duration,
            )
            candidate, _ = _frame_at_timestamp(
                container,
                stream,
                timestamp,
                presentation_origin,
                is_cancelled,
            )
            actual_pts = _frame_timestamp(candidate) - presentation_origin
            image = _normalized_image(candidate, stream, metadata_frame)
            return DecodedFrame(image, timestamp, actual_pts, request_id, revision)
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
    if source.suffix.casefold() not in SUPPORTED_CONTAINER_SUFFIXES:
        raise _source_error(
            ErrorCode.SOURCE_FORMAT_UNSUPPORTED,
            "source.path.unsupported-suffix",
            "source suffix must be MP4, MOV, WebM, or MKV",
            "choose-another-file",
        )
    try:
        source_stat = source.lstat()
        if (
            not stat.S_ISREG(source_stat.st_mode)
            or source.is_symlink()
            or not os.access(source, os.R_OK)
        ):
            raise OSError("path is not a readable non-symlink regular file")
    except OSError as error:
        raise _source_error(
            ErrorCode.SOURCE_UNREADABLE,
            "source.path.unreadable",
            "source must be an existing readable local regular file",
            "choose-another-file",
        ) from error
    return source


@contextmanager
def _input_container(
    source: Path, expected_revision: SourceRevision | None
) -> Iterator[tuple[Any, SourceRevision]]:
    before = _path_revision(source)
    if expected_revision is not None and before != expected_revision:
        raise _source_changed("source revision changed before decode")
    try:
        with source.open("rb") as source_file:
            opened = _revision_from_stat(os.fstat(source_file.fileno()))
            if opened != before:
                raise _source_changed("source changed while it was opened")
            try:
                with av.open(source_file, mode="r") as container:
                    yield container, before
            finally:
                after_open = _revision_from_stat(os.fstat(source_file.fileno()))
                after_path = _path_revision(source)
                if after_open != before or after_path != before:
                    raise _source_changed("source changed during media access")
    except AppError:
        raise
    except OSError as error:
        if expected_revision is not None:
            raise _source_changed("source became unavailable during decode") from error
        raise _source_error(
            ErrorCode.SOURCE_UNREADABLE,
            "source.path.unreadable",
            "source must remain a readable non-symlink regular file",
            "choose-another-file",
        ) from error


def _path_revision(source: Path) -> SourceRevision:
    try:
        source_stat = source.lstat()
    except OSError as error:
        raise _source_changed("source path became unavailable") from error
    if not stat.S_ISREG(source_stat.st_mode) or source.is_symlink():
        raise _source_changed("source path is no longer a regular file")
    return _revision_from_stat(source_stat)


def _revision_from_stat(source_stat: os.stat_result) -> SourceRevision:
    return SourceRevision(
        device=source_stat.st_dev,
        inode=source_stat.st_ino,
        size=source_stat.st_size,
        mtime_ns=source_stat.st_mtime_ns,
        ctime_ns=source_stat.st_ctime_ns,
    )


def _source_changed(detail: str) -> AppError:
    return _source_error(
        ErrorCode.SOURCE_CHANGED,
        "source.revision.changed",
        detail,
        "reload-source",
    )


def _validate_container_format(source: Path, container: Any) -> None:
    format_name = str(getattr(getattr(container, "format", None), "name", ""))
    suffix = source.suffix.casefold()
    compatible = {
        ".mp4": ("mov", "mp4"),
        ".mov": ("mov", "mp4"),
        ".mkv": ("matroska",),
        ".webm": ("matroska", "webm"),
    }[suffix]
    names = {item.casefold() for item in format_name.split(",")}
    if not names.intersection(compatible):
        raise _source_error(
            ErrorCode.SOURCE_FORMAT_UNSUPPORTED,
            "source.probe.container-mismatch",
            f"{suffix} suffix does not match demuxer {format_name or 'unknown'}",
            "choose-another-file",
        )


def _decodable_video_stream(
    container: Any, is_cancelled: CancelCheck | None = None
) -> tuple[Any, Any]:
    """Select the first ordinary video stream with a timestamped frame."""
    streams = tuple(container.streams.video)
    if not streams:
        raise _source_error(
            ErrorCode.SOURCE_NO_VIDEO,
            "source.probe.no-video",
            "source contains no video stream",
            "choose-another-file",
        )
    excluded = (
        av.stream.Disposition.attached_pic
        | av.stream.Disposition.timed_thumbnails
        | av.stream.Disposition.still_image
    )
    candidates = tuple(
        stream
        for stream in streams
        if not (getattr(stream, "disposition", av.stream.Disposition(0)) & excluded)
    )
    if not candidates:
        raise _source_error(
            ErrorCode.SOURCE_NO_VIDEO,
            "source.probe.no-display-video",
            "source contains only attached pictures or thumbnail streams",
            "choose-another-file",
        )
    for index, stream in enumerate(candidates):
        _raise_if_cancelled(is_cancelled)
        if index:
            try:
                container.seek(0, backward=True, any_frame=False)
            except (OSError, ValueError, av.FFmpegError):
                continue
        try:
            for frame in container.decode(stream):
                _raise_if_cancelled(is_cancelled)
                if frame.pts is not None and frame.time_base is not None:
                    return stream, frame
        except (OSError, ValueError, av.FFmpegError):
            continue
    raise _source_error(
        ErrorCode.SOURCE_CORRUPT,
        "source.probe.no-frames",
        "source contains no decodable timestamped video stream",
        "choose-another-file",
    )


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


def _seek_for_timestamp(
    container: Any,
    stream: Any,
    timestamp: Fraction,
    presentation_origin: Fraction,
    duration: Fraction,
) -> None:
    time_base = Fraction(stream.time_base)
    latest_seek = presentation_origin + max(Fraction(0), duration - time_base)
    seek_timestamp = min(presentation_origin + timestamp, latest_seek)
    offset = (
        seek_timestamp.numerator
        * time_base.denominator
        // (seek_timestamp.denominator * time_base.numerator)
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
    presentation_origin: Fraction,
    is_cancelled: CancelCheck | None,
) -> tuple[Any, Any]:
    candidate = None
    first_frame = None
    # The caller seeks backward to a keyframe, never by frame/fps arithmetic.
    for frame in _decoded_frames(container, stream):
        _raise_if_cancelled(is_cancelled)
        _validate_frame_color(stream, frame)
        if frame.pts is None or frame.time_base is None:
            continue
        frame_pts = _frame_timestamp(frame) - presentation_origin
        if frame_pts < 0:
            continue
        if first_frame is None:
            first_frame = frame
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


@dataclass(frozen=True, slots=True)
class _DerivedTimeline:
    duration: Fraction
    peak_rate: Fraction
    frame_count: int


def _derive_timeline(
    container: Any,
    stream: Any,
    presentation_origin: Fraction,
    nominal_rate: Fraction | None,
    is_cancelled: CancelCheck | None = None,
) -> _DerivedTimeline:
    """Boundedly prove duration and maximum cadence from exact decoded PTS."""
    _raise_if_cancelled(is_cancelled)
    time_base = Fraction(stream.time_base)
    offset = (
        presentation_origin.numerator
        * time_base.denominator
        // (presentation_origin.denominator * time_base.numerator)
    )
    try:
        container.seek(offset, stream=stream, backward=True, any_frame=False)
    except (OSError, ValueError, av.FFmpegError) as error:
        raise _source_error(
            ErrorCode.SOURCE_CORRUPT,
            "source.probe.timeline-seek",
            f"could not derive missing timeline metadata: {error}",
            "convert-source",
        ) from error
    _raise_if_cancelled(is_cancelled)
    timestamps: list[Fraction] = []
    decoded_count = 0
    for frame in _decoded_frames(container, stream):
        _raise_if_cancelled(is_cancelled)
        decoded_count += 1
        if decoded_count > MAX_TIMELINE_DECODED_FRAMES:
            raise _source_error(
                ErrorCode.SOURCE_DURATION_UNSUPPORTED,
                "source.probe.unbounded-timeline",
                "timeline exceeds the bounded decoded-frame proof budget",
                "trim-or-convert-source",
            )
        _validate_frame_color(stream, frame)
        if frame.pts is None or frame.time_base is None:
            raise _source_error(
                ErrorCode.SOURCE_CORRUPT,
                "source.probe.missing-pts",
                "decoded frame has no provable presentation timestamp",
                "convert-source",
            )
        timestamp = _frame_timestamp(frame) - presentation_origin
        if timestamp < 0:
            continue
        if timestamps and timestamp <= timestamps[-1]:
            raise _source_error(
                ErrorCode.SOURCE_CORRUPT,
                "source.probe.nonmonotonic-pts",
                "presentation timestamps are duplicate or non-increasing",
                "convert-source",
            )
        timestamps.append(timestamp)
        if timestamp > MAX_SOURCE_DURATION:
            raise _source_error(
                ErrorCode.SOURCE_DURATION_UNSUPPORTED,
                "source.probe.too-long",
                "video duration exceeds the 10 minute V1 limit",
                "trim-or-convert-source",
            )
    if not timestamps:
        raise _source_error(
            ErrorCode.SOURCE_CORRUPT,
            "source.probe.no-timestamps",
            "video cadence cannot be proven without presentation timestamps",
            "convert-source",
        )
    if len(timestamps) == 1:
        if nominal_rate is None or nominal_rate <= 0:
            raise _source_error(
                ErrorCode.SOURCE_FPS_UNSUPPORTED,
                "source.probe.unknown-cadence",
                "single-frame source has no provable cadence",
                "convert-source",
            )
        return _DerivedTimeline(Fraction(1, nominal_rate), nominal_rate, 1)
    deltas = tuple(
        later - earlier for earlier, later in zip(timestamps, timestamps[1:])
    )
    peak_rate = max(Fraction(1, delta) for delta in deltas)
    if peak_rate > MAX_SOURCE_FPS:
        raise _source_error(
            ErrorCode.SOURCE_FPS_UNSUPPORTED,
            "source.probe.high-frame-rate",
            "decoded source cadence exceeds the 60 fps V1 limit",
            "convert-source-to-60fps",
        )
    duration = timestamps[-1] + deltas[-1]
    return _DerivedTimeline(duration, peak_rate, len(timestamps))


def _source_info(
    source: Path,
    revision: SourceRevision,
    container: Any,
    stream: Any,
    frame: Any,
    is_cancelled: CancelCheck | None = None,
) -> SourceInfo:
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
    presentation_origin = _presentation_origin(stream, frame, time_base)
    average_rate = _fraction_or_none(stream.average_rate)
    base_rate = _fraction_or_none(getattr(stream, "base_rate", None))
    guessed_rate = _fraction_or_none(getattr(stream, "guessed_rate", None))
    validation_rates = tuple(
        rate for rate in (average_rate, guessed_rate) if rate is not None and rate > 0
    )
    validation_rate = max(validation_rates) if validation_rates else None
    duration = _duration(container, stream, time_base, presentation_origin)
    cfr_rate = _proven_cfr_rate(
        stream,
        duration,
        average_rate,
        base_rate,
        guessed_rate,
    )
    derived: _DerivedTimeline | None = None
    if cfr_rate is None:
        derived = _derive_timeline(
            container,
            stream,
            presentation_origin,
            validation_rate,
            is_cancelled,
        )
        if duration <= 0:
            duration = derived.duration
        validation_rate = max(validation_rate or Fraction(0), derived.peak_rate)
    else:
        validation_rate = max(validation_rate or Fraction(0), cfr_rate)
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
    if validation_rate is not None and validation_rate > MAX_SOURCE_FPS:
        raise _source_error(
            ErrorCode.SOURCE_FPS_UNSUPPORTED,
            "source.probe.high-frame-rate",
            "source frame rate exceeds the 60 fps V1 limit",
            "convert-source-to-60fps",
        )

    rotation, pixel_aspect = _orientation(stream, frame, frame)
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
    _validate_frame_color(stream, frame)

    frames = int(stream.frames) if getattr(stream, "frames", 0) > 0 else None
    if frames is None and derived is not None:
        frames = derived.frame_count
    return SourceInfo(
        path=source,
        revision=revision,
        width=width,
        height=height,
        duration=duration,
        time_base=time_base,
        average_rate=average_rate,
        base_rate=base_rate,
        guessed_rate=guessed_rate,
        peak_rate=(derived.peak_rate if derived is not None else validation_rate),
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


def _duration(
    container: Any,
    stream: Any,
    time_base: Fraction,
    presentation_origin: Fraction,
) -> Fraction:
    if stream.duration is not None:
        return Fraction(stream.duration) * time_base
    if container.duration is not None:
        return Fraction(container.duration, int(av.time_base))
    return Fraction(0)


def _proven_cfr_rate(
    stream: Any,
    duration: Fraction,
    average_rate: Fraction | None,
    base_rate: Fraction | None,
    guessed_rate: Fraction | None,
) -> Fraction | None:
    """Return a metadata-proven CFR rate, else require an exact PTS scan."""
    rates = (average_rate, base_rate, guessed_rate)
    if (
        getattr(stream, "duration", None) is None
        or duration <= 0
        or any(rate is None or rate <= 0 for rate in rates)
    ):
        return None
    rate = average_rate
    if rate is None or any(candidate != rate for candidate in rates):
        return None
    frame_count = int(getattr(stream, "frames", 0))
    if frame_count <= 0 or Fraction(frame_count, rate) != duration:
        return None
    return rate


def _presentation_origin(stream: Any, frame: Any, time_base: Fraction) -> Fraction:
    if stream.start_time is not None:
        return Fraction(stream.start_time) * time_base
    return _frame_timestamp(frame)


def _frame_timestamp(frame: Any) -> Fraction:
    return Fraction(frame.pts) * Fraction(frame.time_base)


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


def _orientation(stream: Any, frame: Any, metadata_frame: Any) -> tuple[int, Fraction]:
    """Resolve frame > first-frame > stream > legacy orientation surfaces.

    Lower-precedence metadata may fill an absent value, but any two explicit
    values must agree so stale legacy tags cannot silently override display data.
    Pixel aspect follows frame > first-frame > stream > codec with the same rule.
    """
    rotations = [
        _display_matrix_candidate(frame),
        _display_matrix_candidate(metadata_frame),
        _display_matrix_candidate(
            SimpleNamespace(side_data=getattr(stream, "side_data", ()))
        ),
        _rotation_metadata_candidate(getattr(stream, "metadata", {})),
    ]
    explicit_rotations = {value for value in rotations if value is not None}
    if len(explicit_rotations) > 1:
        raise _source_error(
            ErrorCode.SOURCE_CORRUPT,
            "source.orientation.conflict",
            "frame, stream, and legacy rotation metadata conflict",
            "convert-source",
        )
    rotation = next((value for value in rotations if value is not None), 0)

    codec = stream.codec_context
    aspects = [
        _fraction_or_none(getattr(frame, "sample_aspect_ratio", None)),
        _fraction_or_none(getattr(metadata_frame, "sample_aspect_ratio", None)),
        _fraction_or_none(getattr(stream, "sample_aspect_ratio", None)),
        _fraction_or_none(getattr(codec, "sample_aspect_ratio", None)),
    ]
    explicit_aspects = {value for value in aspects if value is not None and value > 0}
    if len(explicit_aspects) > 1:
        raise _source_error(
            ErrorCode.SOURCE_CORRUPT,
            "source.orientation.pixel-aspect-conflict",
            "frame, stream, and codec pixel aspect ratios conflict",
            "convert-source",
        )
    pixel_aspect = next(
        (value for value in aspects if value is not None and value > 0),
        Fraction(1),
    )
    return rotation, pixel_aspect


def _rotation_from_metadata(metadata: Mapping[str, object]) -> int:
    """Return CCW presentation degrees for legacy clockwise rotate metadata."""
    raw = next(
        (value for key, value in metadata.items() if key.casefold() == "rotate"),
        None,
    )
    try:
        return _normalized_quarter_turn(-float(str(raw))) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def _rotation_metadata_candidate(
    metadata: Mapping[str, object],
) -> int | None:
    raw = next(
        (value for key, value in metadata.items() if key.casefold() == "rotate"),
        None,
    )
    if raw is None:
        return None
    try:
        degrees = float(str(raw))
    except (TypeError, ValueError):
        return None
    rotation = _normalized_quarter_turn(-degrees)
    if rotation == 0 and not math.isclose(degrees % 360, 0, abs_tol=0.01):
        raise _source_error(
            ErrorCode.SOURCE_CORRUPT,
            "source.orientation.non-quarter-turn",
            "legacy orientation must be a 0/90/180/270 degree rotation",
            "convert-source",
        )
    return rotation


def _rotation_from_display_matrix(frame: Any) -> int:
    return _display_matrix_candidate(frame) or 0


def _display_matrix_candidate(frame: Any) -> int | None:
    for side_data in getattr(frame, "side_data", ()):
        if getattr(getattr(side_data, "type", None), "name", "") != "DISPLAYMATRIX":
            continue
        raw = bytes(side_data)
        if len(raw) < 36:
            raise _source_error(
                ErrorCode.SOURCE_CORRUPT,
                "source.orientation.invalid-display-matrix",
                "display orientation matrix is truncated",
                "convert-source",
            )
        matrix = struct.unpack("=9i", raw[:36])
        # Match av_display_rotation_get: the matrix coefficients use the
        # opposite sign from Pillow's counter-clockwise transpose operations.
        degrees = math.degrees(math.atan2(-matrix[1], matrix[0]))
        rotation = _normalized_quarter_turn(degrees)
        if rotation == 0 and not math.isclose(degrees % 360, 0, abs_tol=0.01):
            raise _source_error(
                ErrorCode.SOURCE_CORRUPT,
                "source.orientation.non-quarter-turn",
                "display orientation must be a 0/90/180/270 degree rotation",
                "convert-source",
            )
        return rotation
    return None


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
    if any(depth > 8 for depth in component_depths) or _pixel_format_is_high_depth(
        pixel_format
    ):
        raise _source_error(
            ErrorCode.SOURCE_HDR_UNSUPPORTED,
            "source.probe.high-bit-depth",
            "10-bit and higher-depth video is unsupported in V1",
            "convert-source-to-8bit-srgb",
        )


def _validate_frame_color(stream: Any, frame: Any) -> None:
    codec = stream.codec_context
    pixel_format = _pixel_format(frame, codec)
    _validate_sdr_8bit(frame, codec, pixel_format)
    side_data_names = {
        getattr(getattr(item, "type", None), "name", "")
        for owner in (frame, stream, codec)
        for item in getattr(owner, "side_data", ())
    }
    if side_data_names & _HDR_SIDE_DATA:
        raise _unsupported_color("HDR side data is unsupported")
    _color_profile(stream, frame)


@dataclass(frozen=True, slots=True)
class _ColorProfile:
    matrix: int
    color_range: int
    transfer: int
    primaries: int
    rgb_input: bool


def _color_profile(stream: Any, frame: Any) -> _ColorProfile:
    codec = stream.codec_context
    pixel_format = _pixel_format(frame, codec).lower()
    rgb_input = pixel_format.startswith(("rgb", "rgba", "gbr", "bgr"))
    primaries = _resolved_color_value(
        "primaries",
        getattr(frame, "color_primaries", None),
        getattr(codec, "color_primaries", None),
        unspecified=2,
    )
    transfer = _resolved_color_value(
        "transfer",
        getattr(frame, "color_trc", None),
        getattr(codec, "color_trc", None),
        unspecified=2,
    )
    matrix = _resolved_color_value(
        "matrix",
        getattr(frame, "colorspace", None),
        getattr(codec, "colorspace", None),
        unspecified=2,
    )
    color_range = _resolved_color_value(
        "range",
        getattr(frame, "color_range", None),
        getattr(codec, "color_range", None),
        unspecified=0,
    )
    if primaries != 1:
        raise _unsupported_color(
            f"color primaries {primaries} cannot be proven as BT.709/sRGB"
        )
    if transfer not in {1, 6, 13}:
        raise _unsupported_color(f"unsupported transfer characteristic {transfer}")
    if matrix not in {0, 1, 2, 5, 6}:
        raise _unsupported_color(f"unsupported YUV matrix {matrix}")
    if color_range not in {0, 1, 2}:
        raise _unsupported_color(f"unsupported color range {color_range}")
    if rgb_input and matrix not in {0, 2}:
        raise _unsupported_color("RGB input declares a YUV matrix")
    if not rgb_input and matrix == 2:
        raise _unsupported_color("YUV input has no declared conversion matrix")
    if not rgb_input and matrix == 0:
        raise _unsupported_color("YUV input declares the RGB identity matrix")
    if matrix == 2:
        matrix = 0
    if color_range == 0:
        if not rgb_input:
            raise _unsupported_color("YUV input has no declared color range")
        color_range = 2
    return _ColorProfile(matrix, color_range, transfer, primaries, rgb_input)


def _resolved_color_value(
    name: str,
    frame_value: object,
    codec_value: object,
    *,
    unspecified: int,
) -> int:
    frame_int = _metadata_int(frame_value)
    codec_int = _metadata_int(codec_value)
    explicit = {
        value
        for value in (frame_int, codec_int)
        if value is not None and value != unspecified
    }
    if len(explicit) > 1:
        raise _unsupported_color(f"frame and codec {name} metadata conflict")
    if frame_int is not None and frame_int != unspecified:
        return frame_int
    if codec_int is not None and codec_int != unspecified:
        return codec_int
    return unspecified


def _unsupported_color(detail: str) -> AppError:
    return _source_error(
        ErrorCode.SOURCE_HDR_UNSUPPORTED,
        "source.probe.unsupported-color",
        detail,
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
        profile = _color_profile(stream, frame)
        _validate_frame_color(stream, frame)
        if profile.rgb_input:
            converted = (
                frame.reformat(format="rgba") if hasattr(frame, "reformat") else frame
            )
        else:
            colorspace = {
                1: Colorspace.ITU709,
                5: Colorspace.ITU601,
                6: Colorspace.ITU601,
            }[profile.matrix]
            converted = VideoReformatter().reformat(
                frame,
                format="rgba",
                src_colorspace=colorspace,
                dst_colorspace=Colorspace.ITU709,
                src_color_range=(
                    ColorRange.JPEG if profile.color_range == 2 else ColorRange.MPEG
                ),
                dst_color_range=ColorRange.JPEG,
            )
        image = converted.to_image().convert("RGBA")
        if profile.transfer in {1, 6}:
            image = _convert_bt709_transfer_to_srgb(image)
        rotation, pixel_aspect = _orientation(stream, frame, metadata_frame)
        if pixel_aspect != 1:
            display_width = max(
                1, _round_fraction(Fraction(image.width) * pixel_aspect)
            )
            image = image.resize(
                (display_width, image.height), Image.Resampling.LANCZOS
            )
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


def _convert_bt709_transfer_to_srgb(image: Image.Image) -> Image.Image:
    lut: list[int] = []
    for value in range(256):
        encoded = value / 255
        linear = (
            encoded / 4.5
            if encoded < 0.081
            else ((encoded + 0.099) / 1.099) ** (1 / 0.45)
        )
        srgb = (
            linear * 12.92
            if linear <= 0.0031308
            else 1.055 * linear ** (1 / 2.4) - 0.055
        )
        lut.append(min(255, max(0, math.floor(srgb * 255 + 0.5))))
    red, green, blue, alpha = image.split()
    return Image.merge(
        "RGBA", (red.point(lut), green.point(lut), blue.point(lut), alpha)
    )


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
