"""Small deterministic media fixtures generated only with PyAV."""

from __future__ import annotations

import json
from collections.abc import Sequence
from fractions import Fraction
from math import lcm
from pathlib import Path

import av
from PIL import Image

_ROTATION_SIDECAR_SCHEMA_VERSION = 1
_ROTATION_SIDECAR_SUFFIX = ".matteloop.json"


def _write_rotation_sidecar(path: Path, rotation: int) -> None:
    """Write the fixture-only rotation contract PyAV 16 cannot mux portably."""
    sidecar = path.with_suffix(f"{path.suffix}{_ROTATION_SIDECAR_SUFFIX}")
    sidecar.write_text(
        json.dumps(
            {
                "rotation_ccw": rotation,
                "schema_version": _ROTATION_SIDECAR_SCHEMA_VERSION,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def make_video(
    path: Path,
    frames: Sequence[Image.Image],
    fps: Fraction,
    *,
    pts: Sequence[Fraction] | None = None,
    rotation: int = 0,
) -> Path:
    """Encode RGB images as a small MP4 fixture using only PyAV.

    ``pts`` are timestamps in seconds. When omitted, timestamps are evenly
    spaced according to ``fps``. The caller controls every source image and
    timestamp, keeping fixture content deterministic. ``rotation`` is the
    counter-clockwise presentation rotation in the adjacent, versioned
    ``<video>.matteloop.json`` fixture sidecar. PyAV 16 cannot author portable
    MP4 display-matrix side data, so later source-decoder fixture tests must
    consume this explicit contract rather than infer rotation from the video.
    """
    if not frames:
        raise ValueError("at least one frame is required")
    if fps <= 0:
        raise ValueError("fps must be positive")
    if pts is not None and len(pts) != len(frames):
        raise ValueError("pts must have one timestamp per frame")

    width, height = frames[0].size
    if width <= 0 or height <= 0:
        raise ValueError("frames must have positive dimensions")
    if any(frame.size != (width, height) for frame in frames):
        raise ValueError("all frames must have the same dimensions")

    timestamps = (
        tuple(pts)
        if pts is not None
        else tuple(Fraction(index, 1) / fps for index in range(len(frames)))
    )
    if any(timestamp < 0 for timestamp in timestamps):
        raise ValueError("timestamps must be non-negative")
    if any(later <= earlier for earlier, later in zip(timestamps, timestamps[1:])):
        raise ValueError("timestamps must be strictly increasing")

    denominator = lcm(*(timestamp.denominator for timestamp in timestamps))
    time_base = Fraction(1, denominator)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_rotation_sidecar(path, rotation)

    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("libx264", rate=fps)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuv420p"
        stream.codec_context.time_base = time_base
        # Task 7 rejects untagged colorimetry rather than pretending it is
        # sRGB. Synthetic sources therefore declare their authored contract.
        stream.codec_context.color_primaries = 1  # BT.709
        stream.codec_context.color_trc = 13  # IEC 61966-2-1 sRGB
        stream.codec_context.colorspace = 1  # BT.709 YUV matrix
        stream.codec_context.color_range = 1  # MPEG/limited YUV range

        for image, timestamp in zip(frames, timestamps):
            frame = av.VideoFrame.from_image(image.convert("RGB"))
            frame.pts = int(timestamp / time_base)
            frame.time_base = time_base
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)

    return path
