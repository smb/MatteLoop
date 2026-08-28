"""Small deterministic media fixtures generated only with PyAV."""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction
from math import lcm
from pathlib import Path

import av
from PIL import Image


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
    timestamp, keeping fixture content deterministic.
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

    timestamps = tuple(pts) if pts is not None else tuple(
        Fraction(index, 1) / fps for index in range(len(frames))
    )
    if any(timestamp < 0 for timestamp in timestamps):
        raise ValueError("timestamps must be non-negative")
    if any(later <= earlier for earlier, later in zip(timestamps, timestamps[1:])):
        raise ValueError("timestamps must be strictly increasing")

    denominator = lcm(*(timestamp.denominator for timestamp in timestamps))
    time_base = Fraction(1, denominator)
    path.parent.mkdir(parents=True, exist_ok=True)

    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("libx264", rate=fps)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuv420p"
        stream.codec_context.time_base = time_base
        if rotation:
            stream.metadata["rotate"] = str(rotation)

        for image, timestamp in zip(frames, timestamps):
            frame = av.VideoFrame.from_image(image.convert("RGB"))
            frame.pts = int(timestamp / time_base)
            frame.time_base = time_base
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)

    return path
