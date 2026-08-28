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

    assert path.exists()
    assert len(frames) == 2
    assert rate == Fraction(2)
