import json
from fractions import Fraction

import av
from PIL import Image

from tests.fixtures.media_factory import make_video


def test_make_video_writes_tiny_deterministic_video(tmp_path):
    path = make_video(
        tmp_path / "sample.mp4",
        [
            Image.new("RGB", (4, 4), "red"),
            Image.new("RGB", (4, 4), "blue"),
        ],
        Fraction(2),
    )

    with av.open(path) as container:
        stream = container.streams.video[0]
        frames = list(container.decode(stream))
        rate = stream.average_rate
        color_contract = (
            int(stream.codec_context.color_primaries),
            int(stream.codec_context.color_trc),
            int(stream.codec_context.colorspace),
            int(stream.codec_context.color_range),
        )

    assert path.exists()
    assert len(frames) == 2
    assert rate == Fraction(2)
    assert color_contract == (1, 13, 1, 1)


def test_make_video_round_trips_rotation_and_fractional_timestamps(tmp_path):
    path = make_video(
        tmp_path / "rotated.mp4",
        [
            Image.new("RGB", (4, 4), "red"),
            Image.new("RGB", (4, 4), "green"),
            Image.new("RGB", (4, 4), "blue"),
        ],
        Fraction(10),
        pts=[Fraction(0), Fraction(1, 3), Fraction(7, 10)],
        rotation=90,
    )

    with av.open(path) as container:
        stream = container.streams.video[0]
        decoded = list(container.decode(stream))
        duration = Fraction(stream.duration) * stream.time_base

    rotation_sidecar = json.loads(
        path.with_suffix(".mp4.rembggui.json").read_text(encoding="utf-8")
    )
    assert rotation_sidecar == {"rotation_ccw": 90, "schema_version": 1}
    assert [Fraction(frame.pts) * frame.time_base for frame in decoded] == [
        Fraction(0),
        Fraction(1, 3),
        Fraction(7, 10),
    ]
    assert [
        max(range(3), key=frame.to_image().getpixel((0, 0)).__getitem__)
        for frame in decoded
    ] == [0, 1, 2]
    assert duration == Fraction(4, 5)
