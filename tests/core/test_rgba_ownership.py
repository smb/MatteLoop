from __future__ import annotations

import gc
import importlib
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from rembggui.core.webp import encode_lossless_webp


def tracker_type() -> Any:
    try:
        module = importlib.import_module("rembggui.core.rgba")
    except ModuleNotFoundError:
        pytest.fail("RGBA ownership tracker module is missing")
    return module.RgbaOwnershipTracker


def rgba_frames(directory: Path, count: int) -> tuple[Path, ...]:
    paths: list[Path] = []
    for index in range(count):
        path = directory / f"frame-{index:02d}.png"
        with Image.new(
            "RGBA",
            (128, 128),
            (index * 17 % 255, 40, 90, 80 + index),
        ) as image:
            image.save(path, format="PNG")
        paths.append(path)
    return tuple(paths)


@pytest.mark.parametrize("frame_count", [6, 12])
def test_production_webp_rgba_liveness_is_constant_across_frame_counts(
    tmp_path: Path, frame_count: int
) -> None:
    tracker = tracker_type()((128, 128))

    encode_lossless_webp(
        rgba_frames(tmp_path, frame_count),
        (20,) * frame_count,
        tmp_path / "out.webp",
        rgba_ownership_tracker=tracker,
    )
    gc.collect()

    assert tracker.peak == 3
    assert tracker.current == 0


def test_tracker_detects_cross_frame_retention_and_does_not_retain_owners(
    tmp_path: Path,
) -> None:
    class RetainingTracker(tracker_type()):
        def __init__(self) -> None:
            super().__init__((128, 128))
            self.retained: list[object] = []

        def register(
            self,
            owner: object,
            *,
            known_full_resolution_rgba: bool = False,
        ) -> object:
            self.retained.append(owner)
            return super().register(
                owner,
                known_full_resolution_rgba=known_full_resolution_rgba,
            )

    tracker = RetainingTracker()
    encode_lossless_webp(
        rgba_frames(tmp_path, 6),
        (20,) * 6,
        tmp_path / "out.webp",
        rgba_ownership_tracker=tracker,
    )

    assert tracker.peak > 3
    assert tracker.current > 3
    tracker.retained.clear()
    gc.collect()
    assert tracker.current == 0


def test_tracker_deduplicates_identity_and_uses_weak_liveness() -> None:
    tracker = tracker_type()((128, 128))
    image = Image.new("RGBA", (128, 128))

    tracker.register(image)
    tracker.register(image)

    assert tracker.current == 1
    assert tracker.peak == 1
    image.close()
    del image
    gc.collect()
    assert tracker.current == 0


def test_tracker_can_include_framed_and_auto_fit_full_resolution_sizes() -> None:
    tracker = tracker_type()((128, 128))
    tracker.include_size((160, 192))
    cut = Image.new("RGBA", (128, 128))
    framed = Image.new("RGBA", (160, 192))

    tracker.register(cut)
    tracker.register(framed)

    assert tracker.current == 2
    assert tracker.peak == 2
    cut.close()
    framed.close()
    del cut, framed
    gc.collect()
    assert tracker.current == 0
