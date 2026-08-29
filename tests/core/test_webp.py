from __future__ import annotations

import gc
import weakref
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageSequence

import rembggui.core.webp as webp_module
from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.webp import (
    EncodeSummary,
    encode_lossless_webp,
    fit_webp_to_size,
    validate_webp,
)


def rgba_fixture_paths(directory: Path, count: int = 3) -> tuple[Path, ...]:
    paths: list[Path] = []
    # WebP animation compositing canonicalizes RGB beneath alpha=0 to zero.
    colors = ((240, 10, 20, 255), (20, 220, 40, 128), (0, 0, 0, 0))
    for index in range(count):
        image = Image.new("RGBA", (128, 128), colors[index % len(colors)])
        image.putpixel((index, index), (index * 20, 40, 60, 80 + index))
        path = directory / f"frame-{index:03d}.png"
        image.save(path)
        image.close()
        paths.append(path)
    return tuple(paths)


def save_rgba(path: Path, size: tuple[int, int], color: tuple[int, ...]) -> Path:
    with Image.new("RGBA", size, color) as image:
        image.save(path)
    return path


def noisy_rgba(path: Path, size: tuple[int, int] = (256, 256)) -> Path:
    width, height = size
    pixels = bytes(
        component
        for y in range(height)
        for x in range(width)
        for component in (
            (x * 73 + y * 151) & 255,
            (x * 193 + y * 17) & 255,
            (x * 29 + y * 107) & 255,
            32 + ((x * 11 + y * 7) % 224),
        )
    )
    with Image.frombytes("RGBA", size, pixels) as image:
        image.save(path)
    return path


def test_animated_webp_is_lossless_alpha_and_has_expected_duration(
    tmp_path: Path,
) -> None:
    paths = rgba_fixture_paths(tmp_path)
    output = tmp_path / "out.webp"

    encode_lossless_webp(paths, (67, 66, 67), output)
    info = validate_webp(output, expected_frames=3, expected_duration_ms=200)

    assert (info.frames, info.loop, info.has_alpha) == (3, 0, True)
    with Image.open(output) as encoded:
        decoded_pixels = tuple(
            frame.convert("RGBA").tobytes() for frame in ImageSequence.Iterator(encoded)
        )
    with (
        Image.open(paths[0]) as first,
        Image.open(paths[1]) as second,
        Image.open(paths[2]) as third,
    ):
        assert decoded_pixels == (
            first.tobytes(),
            second.tobytes(),
            third.tobytes(),
        )


def test_impossible_target_preserves_existing_output(tmp_path: Path) -> None:
    existing = tmp_path / "out.webp"
    existing.write_bytes(b"known-good")

    with pytest.raises(AppError) as exc:
        fit_webp_to_size(
            rgba_fixture_paths(tmp_path, count=1),
            (100,),
            1,
            tmp_path / "work",
            existing,
        )

    assert existing.read_bytes() == b"known-good"
    assert exc.value.code is ErrorCode.IMPOSSIBLE_SIZE


def test_still_webp_preserves_exact_rgba_without_metadata(tmp_path: Path) -> None:
    source = save_rgba(tmp_path / "still.png", (128, 129), (10, 30, 220, 91))
    output = tmp_path / "still.webp"

    summary = encode_lossless_webp((source,), (123,), output)
    info = validate_webp(output, expected_frames=1, expected_duration_ms=0)

    assert (summary.width, summary.height, summary.frames) == (128, 129, 1)
    assert (info.lossless, info.loop, info.duration_ms) == (True, 0, 0)
    with Image.open(source) as expected, Image.open(output) as actual:
        assert actual.convert("RGBA").tobytes() == expected.tobytes()
        assert "icc_profile" not in actual.info
        assert "exif" not in actual.info
        assert "xmp" not in actual.info


def test_animated_webp_stores_each_odd_delay_exactly(tmp_path: Path) -> None:
    paths = rgba_fixture_paths(tmp_path, count=6)
    output = tmp_path / "odd-delays.webp"

    encode_lossless_webp(paths, (17, 16, 17, 17, 16, 17), output)

    assert validate_webp(output, 6, 100).duration_ms == 100


def test_validation_rejects_a_finite_animation_loop(tmp_path: Path) -> None:
    output = tmp_path / "finite-loop.webp"
    encode_lossless_webp(rgba_fixture_paths(tmp_path), (67, 66, 67), output)
    data = bytearray(output.read_bytes())
    animation_chunk = data.index(b"ANIM")
    data[animation_chunk + 12 : animation_chunk + 14] = (1).to_bytes(2, "little")
    output.write_bytes(data)

    with pytest.raises(AppError) as exc:
        validate_webp(output, expected_frames=3, expected_duration_ms=200)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert "infinite" in exc.value.technical_detail


@pytest.mark.parametrize(
    ("expected_frames", "expected_duration_ms"),
    [(2, 200), (3, 199)],
)
def test_validation_rejects_wrong_frame_or_duration_expectation(
    tmp_path: Path, expected_frames: int, expected_duration_ms: int
) -> None:
    output = tmp_path / "animation.webp"
    encode_lossless_webp(rgba_fixture_paths(tmp_path), (67, 66, 67), output)

    with pytest.raises(AppError) as exc:
        validate_webp(output, expected_frames, expected_duration_ms)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT


def test_validation_rejects_corrupt_truncated_and_lossy_webp(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.webp"
    corrupt.write_bytes(b"RIFF\x10\x00\x00\x00WEBPVP8L")
    lossy = tmp_path / "lossy.webp"
    with Image.new("RGB", (128, 128), (1, 2, 3)) as image:
        image.save(lossy, format="WEBP", lossless=False, quality=50)

    for path in (corrupt, lossy):
        with pytest.raises(AppError) as exc:
            validate_webp(path, expected_frames=1, expected_duration_ms=0)
        assert exc.value.code is ErrorCode.INVALID_OUTPUT


def test_validation_rejects_out_of_guard_dimensions(tmp_path: Path) -> None:
    too_small = tmp_path / "small.webp"
    with Image.new("RGBA", (127, 128), (0, 0, 0, 0)) as image:
        image.save(too_small, format="WEBP", lossless=True)

    with pytest.raises(AppError) as exc:
        validate_webp(too_small, expected_frames=1, expected_duration_ms=0)

    assert exc.value.code is ErrorCode.INVALID_FINAL_DIMENSIONS


def test_validation_rejects_riff_at_four_gib_before_decoding(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.webp"
    with oversized.open("wb") as output:
        output.seek((1 << 32) - 1)
        output.write(b"\0")

    with pytest.raises(AppError) as exc:
        validate_webp(oversized, expected_frames=1, expected_duration_ms=0)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert "4 GiB" in exc.value.technical_detail


@pytest.mark.parametrize(
    "delays",
    [(), (100, 100), (0,), (True,), (1 << 24,)],
)
def test_encode_rejects_invalid_delay_contracts_without_touching_output(
    tmp_path: Path, delays: tuple[int, ...]
) -> None:
    source = save_rgba(tmp_path / "source.png", (128, 128), (1, 2, 3, 4))
    output = tmp_path / "output.webp"
    output.write_bytes(b"existing")

    with pytest.raises(AppError) as exc:
        encode_lossless_webp((source,), delays, output)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert output.read_bytes() == b"existing"
    assert not tuple(tmp_path.glob(".output.webp.*.tmp.webp"))


def test_encode_rejects_corrupt_mismatched_and_non_rgba_inputs_atomically(
    tmp_path: Path,
) -> None:
    good = save_rgba(tmp_path / "good.png", (128, 128), (1, 2, 3, 4))
    mismatched = save_rgba(tmp_path / "mismatch.png", (129, 128), (1, 2, 3, 4))
    rgb = tmp_path / "rgb.png"
    with Image.new("RGB", (128, 128), (1, 2, 3)) as image:
        image.save(rgb)
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"not a png")
    output = tmp_path / "output.webp"

    for paths in ((good, mismatched), (rgb,), (corrupt,), (tmp_path,)):
        output.write_bytes(b"existing")
        with pytest.raises(AppError) as exc:
            encode_lossless_webp(paths, (100,) * len(paths), output)
        assert exc.value.code is ErrorCode.INVALID_OUTPUT
        assert output.read_bytes() == b"existing"


def test_frame_count_guard_runs_before_iterating_or_opening_paths(
    tmp_path: Path,
) -> None:
    class TooManyPaths(Sequence[Path]):
        def __len__(self) -> int:
            return 100_001

        def __getitem__(self, index: int) -> Path:
            raise AssertionError("oversized path sequence must not be indexed")

    class TooManyDelays(Sequence[int]):
        def __len__(self) -> int:
            return 100_001

        def __getitem__(self, index: int) -> int:
            raise AssertionError("oversized delay sequence must not be indexed")

    with pytest.raises(AppError) as exc:
        encode_lossless_webp(TooManyPaths(), TooManyDelays(), tmp_path / "out.webp")

    assert exc.value.code is ErrorCode.INVALID_OUTPUT


def test_encoder_opens_frames_lazily_and_closes_every_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = rgba_fixture_paths(tmp_path, count=9)
    actual_open = webp_module.Image.open
    references: list[weakref.ReferenceType[Image.Image]] = []
    file_handles: list[Any] = []
    maximum_live = 0

    def tracked_open(*args: Any, **kwargs: Any) -> Image.Image:
        nonlocal maximum_live
        gc.collect()
        image = actual_open(*args, **kwargs)
        references.append(weakref.ref(image))
        file_handle = getattr(image, "fp", None)
        if file_handle is not None:
            file_handles.append(file_handle)
        maximum_live = max(
            maximum_live,
            sum(reference() is not None for reference in references),
        )
        return image

    monkeypatch.setattr(webp_module.Image, "open", tracked_open)

    encode_lossless_webp(paths, (20,) * 9, tmp_path / "out.webp")
    gc.collect()

    assert maximum_live <= 3
    assert all(reference() is None for reference in references)
    assert all(handle.closed for handle in file_handles)


def test_validation_failure_closes_decoder_and_preserves_existing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = save_rgba(tmp_path / "source.png", (128, 128), (1, 2, 3, 4))
    output = tmp_path / "out.webp"
    output.write_bytes(b"existing")
    actual_validate = webp_module.validate_webp

    def reject_temporary(path: Path, expected_frames: int, expected_duration_ms: int):
        info = actual_validate(path, expected_frames, expected_duration_ms)
        raise AppError(
            ErrorCode.INVALID_OUTPUT,
            "webp",
            "error.test",
            f"rejected validated {info.frames}-frame output",
            "retry",
        )

    monkeypatch.setattr(webp_module, "validate_webp", reject_temporary)

    with pytest.raises(AppError):
        encode_lossless_webp((source,), (100,), output)

    assert output.read_bytes() == b"existing"
    assert not tuple(tmp_path.glob(".out.webp.*.tmp.webp"))


def test_encode_cancellation_cleans_partial_sibling_and_preserves_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Cancelled(BaseException):
        pass

    source = save_rgba(tmp_path / "source.png", (128, 128), (1, 2, 3, 4))
    output = tmp_path / "out.webp"
    output.write_bytes(b"existing")

    def cancel_encode(_source: Path, temporary: Path) -> None:
        temporary.write_bytes(b"partial")
        raise Cancelled

    monkeypatch.setattr(webp_module, "_encode_still", cancel_encode)

    with pytest.raises(Cancelled):
        encode_lossless_webp((source,), (100,), output)

    assert output.read_bytes() == b"existing"
    assert not tuple(tmp_path.glob(".out.webp.*.tmp.webp"))


def test_fit_resizes_from_immutable_sources_and_stops_after_twelve_encodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = (noisy_rgba(tmp_path / "source.png"),)
    output = tmp_path / "out.webp"
    output.write_bytes(b"existing")
    actual_encode = webp_module.encode_lossless_webp
    actual_resize = webp_module._resize_from_sources
    encode_count = 0
    resize_sources: list[tuple[Path, ...]] = []
    prior_scaled_directory_counts: list[int] = []

    def always_too_large(
        paths: Sequence[Path], delays: Sequence[int], destination: Path
    ) -> EncodeSummary:
        nonlocal encode_count
        encode_count += 1
        summary = actual_encode(paths, delays, destination)
        return replace(summary, file_size=1000)

    def observe_resize(
        paths: tuple[Path, ...], size: tuple[int, int], destination: Path
    ) -> tuple[Path, ...]:
        resize_sources.append(paths)
        prior_scaled_directory_counts.append(
            len(tuple(destination.parent.glob("scaled-*")))
        )
        return actual_resize(paths, size, destination)

    monkeypatch.setattr(webp_module, "encode_lossless_webp", always_too_large)
    monkeypatch.setattr(webp_module, "_resize_from_sources", observe_resize)

    with pytest.raises(AppError) as exc:
        fit_webp_to_size(sources, (100,), 999, tmp_path / "work", output)

    assert exc.value.code is ErrorCode.IMPOSSIBLE_SIZE
    assert encode_count == 12
    assert resize_sources == [sources] * 11
    assert prior_scaled_directory_counts == [0] * 11
    assert output.read_bytes() == b"existing"
    assert not tuple((tmp_path / "work").glob("webp-fit-*"))


def test_fit_produces_valid_bounded_output_and_cleans_workspace(tmp_path: Path) -> None:
    source = noisy_rgba(tmp_path / "source.png")
    output = tmp_path / "out.webp"

    result = fit_webp_to_size((source,), (100,), 100_000, tmp_path / "work", output)
    info = validate_webp(result, expected_frames=1, expected_duration_ms=0)

    assert result == output
    assert output.stat().st_size <= 100_000
    assert 128 <= info.width <= 256
    assert 128 <= info.height <= 256
    assert not tuple((tmp_path / "work").glob("webp-fit-*"))


def test_fit_revalidates_final_bytes_before_replacing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = noisy_rgba(tmp_path / "source.png", (128, 128))
    output = tmp_path / "out.webp"
    output.write_bytes(b"existing")
    actual_encode = webp_module.encode_lossless_webp

    def misreported_size(
        paths: Sequence[Path], delays: Sequence[int], destination: Path
    ) -> EncodeSummary:
        summary = actual_encode(paths, delays, destination)
        return replace(summary, file_size=1)

    monkeypatch.setattr(webp_module, "encode_lossless_webp", misreported_size)

    with pytest.raises(AppError) as exc:
        fit_webp_to_size((source,), (100,), 1, tmp_path / "work", output)

    assert exc.value.code is ErrorCode.IMPOSSIBLE_SIZE
    assert output.read_bytes() == b"existing"
    assert not tuple((tmp_path / "work").glob("webp-fit-*"))
