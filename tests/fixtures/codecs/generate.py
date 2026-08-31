"""Generate the committed, deterministic H.264 and H.265 decoder fixtures."""

from __future__ import annotations

import tempfile
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

_FIXTURE_DIRECTORY = Path(__file__).parent
_FRAME_SIZE = (64, 48)
_FRAME_RATE = 2
_TIME_BASE = Fraction(1, _FRAME_RATE)
_FIXTURES = (
    ("h264-sdr.mp4", "libx264", None),
    ("h265-sdr.mp4", "libx265", "log-level=error:pools=1:frame-threads=1"),
)


def _encode(path: Path, encoder: str, x265_params: str | None) -> None:
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream(encoder, rate=_FRAME_RATE)
        stream.width, stream.height = _FRAME_SIZE
        stream.pix_fmt = "yuv420p"
        stream.codec_context.time_base = _TIME_BASE
        stream.codec_context.color_primaries = 1
        stream.codec_context.color_trc = 13
        stream.codec_context.colorspace = 1
        stream.codec_context.color_range = 1
        if x265_params is not None:
            stream.codec_context.options["x265-params"] = x265_params
        for index in range(2):
            pixels = np.empty((_FRAME_SIZE[1], _FRAME_SIZE[0], 3), dtype=np.uint8)
            pixels[:, :, 0] = 32 + index * 160
            pixels[:, :, 1] = 96
            pixels[:, :, 2] = 208 - index * 96
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            frame.pts = index
            frame.time_base = _TIME_BASE
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def _generate_twice(name: str, encoder: str, x265_params: str | None) -> bytes:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_root = Path(temporary_directory)
        first = temporary_root / "first.mp4"
        second = temporary_root / "second.mp4"
        _encode(first, encoder, x265_params)
        _encode(second, encoder, x265_params)
        first_bytes = first.read_bytes()
        if first_bytes != second.read_bytes():
            raise RuntimeError(f"{name} generation was not byte-for-byte deterministic")
        return first_bytes


def _replace_fixture(destination: Path, generated: bytes) -> None:
    temporary_destination: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=_FIXTURE_DIRECTORY, delete=False) as file:
            temporary_destination = Path(file.name)
            file.write(generated)
        temporary_destination.replace(destination)
    finally:
        if temporary_destination is not None:
            temporary_destination.unlink(missing_ok=True)


def main() -> None:
    for name, encoder, x265_params in _FIXTURES:
        generated = _generate_twice(name, encoder, x265_params)
        _replace_fixture(_FIXTURE_DIRECTORY / name, generated)


if __name__ == "__main__":
    main()
