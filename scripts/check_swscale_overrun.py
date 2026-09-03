"""Check the production decode path for libswscale destination overruns.

libswscale writes past the end of the rgba destination PyAV allocates for it,
which FFmpeg trac #9254 reports against the SSSE3 yuv420_rgb32 converter. The
converter is x86, so this only detects anything on an x86-64 host, and only
under Valgrind: on a normal run the write lands in whatever slack the
allocator happened to leave and corrupts silently.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

import av

# 448, 464 and 640 are controls that need no padding under different
# alignments (448 and 640 are multiples of 64, 464 only of 16), so a build
# whose SIMD block is wider than assumed shows up here rather than in a
# user's crash. The small sizes probe near the MIN_SOURCE_DIMENSION floor.
_CLIPS = ((18, 8), (40, 24), (200, 116), (440, 444), (448, 448), (464, 464), (640, 360))
_FRAME_COUNT = 3
_FRAME_RATE = 2
_TIME_BASE = Fraction(1, _FRAME_RATE)
_STANZA = re.compile(r"^==\d+== *$")


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


def _invalid_writes(log: str) -> list[str]:
    """Return every Invalid write stanza in a Valgrind log."""
    stanzas: list[str] = []
    current: list[str] | None = None
    for line in log.splitlines():
        if "Invalid write" in line:
            if current is not None:
                stanzas.append("\n".join(current))
            current = [line]
            continue
        if current is None:
            continue
        if _STANZA.match(line):
            stanzas.append("\n".join(current))
            current = None
            continue
        current.append(line)
    if current is not None:
        stanzas.append("\n".join(current))
    return stanzas


def _check(directory: Path) -> int:
    if shutil.which("valgrind") is None:
        print("valgrind not installed; nothing to check")
        return 0

    script = Path(__file__).resolve()
    failures = 0
    for width, height in _CLIPS:
        clip = directory / f"clip-{width}x{height}.mp4"
        _encode_clip(clip, width, height)
        log_path = directory / f"valgrind-{width}x{height}.log"
        completed = subprocess.run(
            [
                "valgrind",
                "--tool=memcheck",
                "--num-callers=30",
                f"--log-file={log_path}",
                sys.executable,
                str(script),
                "--mode",
                "decode-only",
                str(clip),
            ],
            check=False,
            env={**_child_env()},
        )
        log = log_path.read_text(encoding="utf-8", errors="replace")
        stanzas = _invalid_writes(log)
        swscale = [stanza for stanza in stanzas if "libswscale" in stanza]
        if swscale:
            verdict = "OVERRUN"
        else:
            verdict = "other invalid write" if stanzas else "clean"
        print(
            f"{width}x{height}: width%16={width % 16:>2} "
            f"exit={completed.returncode} {verdict}"
        )
        # Print every stanza, not only the libswscale ones: a write from
        # somewhere else would mean the padding itself is wrong.
        for stanza in stanzas:
            print(stanza)
        failures += len(swscale)
    return 1 if failures else 0


def _child_env() -> dict[str, str]:
    import os

    environment = dict(os.environ)
    # pymalloc's arena reuse hides the write from memcheck.
    environment["PYTHONMALLOC"] = "malloc"
    return environment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("check", "decode-only"), default="check")
    parser.add_argument("path", nargs="?", type=Path)
    arguments = parser.parse_args()

    if arguments.mode == "decode-only":
        if arguments.path is None:
            parser.error("decode-only requires a clip path")
        return _decode_only(arguments.path)

    with tempfile.TemporaryDirectory(prefix="matteloop-swscale-") as directory:
        return _check(Path(directory))


if __name__ == "__main__":
    raise SystemExit(main())
