"""Check the production decode path for libswscale destination overruns."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

import av
from av.video.reformatter import ColorRange, Colorspace, VideoReformatter

_CLIPS = ((440, 444), (448, 448))
_FRAME_COUNT = 3
_FRAME_RATE = 2
_TIME_BASE = Fraction(1, _FRAME_RATE)


def _encode_clip(path: Path, width: int, height: int) -> None:
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("libx264", rate=_FRAME_RATE)
        stream.width, stream.height = width, height
        stream.pix_fmt = "yuv420p"
        stream.codec_context.time_base = _TIME_BASE
        stream.codec_context.color_primaries = 1
        stream.codec_context.color_trc = 1
        stream.codec_context.colorspace = 1
        stream.codec_context.color_range = 1

        for index in range(_FRAME_COUNT):
            frame = av.VideoFrame(width, height, "yuv420p")
            for plane, value in zip(frame.planes, (32 + index * 32, 96, 160)):
                plane.update(bytes([value]) * plane.buffer_size)
            frame.pts = index
            frame.time_base = _TIME_BASE
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def _reformat_and_report(path: Path, width: int, height: int) -> None:
    with av.open(str(path), mode="r") as container:
        frame = next(container.decode(video=0))
        av.logging.set_level(av.logging.DEBUG)
        with av.logging.Capture() as logs:
            rgba = VideoReformatter().reformat(
                frame,
                format="rgba",
                src_colorspace=Colorspace.ITU709,
                dst_colorspace=Colorspace.ITU709,
                src_color_range=ColorRange.MPEG,
                dst_color_range=ColorRange.JPEG,
            )

    plane = rgba.planes[0]
    slack = plane.buffer_size - width * 4 * height
    print(f"dimensions: {width}x{height}")
    print(f"width % 16: {width % 16}")
    print(
        "rgba destination: "
        f"line_size={plane.line_size} buffer_size={plane.buffer_size} slack={slack}"
    )
    print("libswscale logs:")
    if logs:
        for level, name, message in logs:
            print(f"  level={level} logger={name}: {message.rstrip()}")
    else:
        print("  (none captured)")


def _decode_only(path: Path) -> int:
    from matteloop.jobs.source import decode_frame, probe_source

    source_info = probe_source(path)
    decoded = decode_frame(
        path,
        Fraction(0),
        request_id=1,
        expected_revision=source_info.revision,
        validation_proof=source_info.validation_proof,
    )
    decoded.image.close()
    return 0


def _report() -> None:
    temporary_directory = Path(tempfile.mkdtemp(prefix="matteloop-swscale-"))
    print(f"temporary directory: {temporary_directory}")
    paths: list[Path] = []
    for width, height in _CLIPS:
        path = temporary_directory / f"clip-{width}x{height}.mp4"
        _encode_clip(path, width, height)
        paths.append(path)
        _reformat_and_report(path, width, height)
        print(f"clip {width}x{height}: {path}")

    script = Path(__file__).resolve()
    for path in paths:
        result = subprocess.run(
            [sys.executable, str(script), "--mode", "decode-only", str(path)],
            check=False,
        )
        print(f"decode subprocess {path.name}: exit_status={result.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("report", "decode-only"), default="report")
    parser.add_argument("path", nargs="?", type=Path)
    arguments = parser.parse_args()

    if arguments.mode == "decode-only":
        if arguments.path is None:
            parser.error("decode-only requires a clip path")
        return _decode_only(arguments.path)

    try:
        _report()
    except Exception as error:
        print(f"report error: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
