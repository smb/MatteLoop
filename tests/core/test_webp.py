from __future__ import annotations

import errno
import gc
import os
import warnings
import weakref
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageSequence

import rembggui.core.webp as webp_module
from rembggui.core.errors import AppError, ErrorCode, ValidationError
from rembggui.core.rgba import RgbaOwnershipTracker
from rembggui.core.specs import MAX_FINAL_DIMENSION
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


def save_rgba_with_compression(
    path: Path,
    size: tuple[int, int],
    color: tuple[int, ...],
    compress_level: int,
) -> Path:
    with Image.new("RGBA", size, color) as image:
        image.save(path, compress_level=compress_level)
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


def riff_chunks(data: bytes | bytearray) -> list[tuple[int, bytes, int, int]]:
    chunks: list[tuple[int, bytes, int, int]] = []
    position = 12
    while position < len(data):
        size = int.from_bytes(data[position + 4 : position + 8], "little")
        padded_end = position + 8 + size + (size & 1)
        chunks.append(
            (position, bytes(data[position : position + 4]), size, padded_end)
        )
        position = padded_end
    return chunks


def set_riff_size(data: bytearray) -> None:
    data[4:8] = (len(data) - 8).to_bytes(4, "little")


def replace_animation_delays(data: bytearray, delays_ms: tuple[int, ...]) -> bytearray:
    frame_chunks = [chunk for chunk in riff_chunks(data) if chunk[1] == b"ANMF"]
    assert len(frame_chunks) == len(delays_ms)
    for chunk, delay in zip(frame_chunks, delays_ms, strict=True):
        data[chunk[0] + 20 : chunk[0] + 23] = delay.to_bytes(3, "little")
    return data


def mutate_animation_bytes(data: bytearray, mutation: str) -> bytearray:
    chunks = riff_chunks(data)
    vp8x = next(chunk for chunk in chunks if chunk[1] == b"VP8X")
    anim = next(chunk for chunk in chunks if chunk[1] == b"ANIM")
    anmf = next(chunk for chunk in chunks if chunk[1] == b"ANMF")
    if mutation == "vp8x-reserved-flag":
        data[vp8x[0] + 8] |= 0x80
    elif mutation == "vp8x-reserved-bytes":
        data[vp8x[0] + 9] = 1
    elif mutation == "vp8x-missing-animation":
        data[vp8x[0] + 8] &= ~0x02
    elif mutation == "vp8x-duplicate":
        data[anim[0] : anim[0]] = data[vp8x[0] : vp8x[3]]
        set_riff_size(data)
    elif mutation == "anim-before-vp8x":
        first = bytes(data[vp8x[0] : vp8x[3]])
        second = bytes(data[anim[0] : anim[3]])
        data[vp8x[0] : anim[3]] = second + first
    elif mutation == "anim-duplicate":
        data[anmf[0] : anmf[0]] = data[anim[0] : anim[3]]
        set_riff_size(data)
    elif mutation == "anmf-reserved":
        data[anmf[0] + 8 + 15] |= 0x80
    elif mutation == "anmf-zero-duration":
        data[anmf[0] + 8 + 12 : anmf[0] + 8 + 15] = b"\0\0\0"
    elif mutation == "anmf-outside-canvas":
        data[anmf[0] + 8 : anmf[0] + 11] = (1).to_bytes(3, "little")
    elif mutation == "anmf-vp8l-size-mismatch":
        data[anmf[0] + 8 + 6 : anmf[0] + 8 + 9] = (126).to_bytes(3, "little")
    elif mutation == "nested-lossy-vp8":
        nested = anmf[0] + 8 + 16
        data[nested : nested + 4] = b"VP8 "
    elif mutation == "nested-vp8l-duplicate":
        nested = anmf[0] + 8 + 16
        nested_size = int.from_bytes(data[nested + 4 : nested + 8], "little")
        nested_end = nested + 8 + nested_size + (nested_size & 1)
        duplicate = bytes(data[nested:nested_end])
        insert_at = anmf[0] + 8 + anmf[2]
        data[insert_at:insert_at] = duplicate
        data[anmf[0] + 4 : anmf[0] + 8] = (anmf[2] + len(duplicate)).to_bytes(
            4, "little"
        )
        set_riff_size(data)
    elif mutation == "alpha-flag-mismatch":
        data[vp8x[0] + 8] &= ~0x10
    elif mutation == "nonzero-padding":
        for frame_chunk in chunks:
            if frame_chunk[1] != b"ANMF":
                continue
            nested = frame_chunk[0] + 8 + 16
            nested_size = int.from_bytes(data[nested + 4 : nested + 8], "little")
            if nested_size & 1:
                break
        else:
            raise AssertionError("fixture must contain an odd nested VP8L chunk")
        data[nested + 8 + nested_size] = 1
    else:
        raise AssertionError(f"unknown mutation {mutation}")
    return data


def test_pillow_pixel_policy_covers_the_legal_final_canvas() -> None:
    assert Image.MAX_IMAGE_PIXELS == MAX_FINAL_DIMENSION**2


@pytest.mark.parametrize("failure", ["warning", "error"])
def test_pillow_decompression_bombs_are_structured_without_allocating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    source = save_rgba(tmp_path / "source.png", (128, 128), (1, 2, 3, 4))
    actual_open = webp_module.Image.open

    def bomb_open(*args: Any, **kwargs: Any) -> Image.Image:
        if failure == "warning":
            warnings.warn("synthetic oversized image", Image.DecompressionBombWarning)
        else:
            raise Image.DecompressionBombError("synthetic oversized image")
        return actual_open(*args, **kwargs)

    monkeypatch.setattr(webp_module.Image, "open", bomb_open)

    with pytest.raises((AppError, ValidationError)) as exc:
        encode_lossless_webp((source,), (100,), tmp_path / "out.webp")

    assert exc.value.code is ErrorCode.INVALID_FINAL_DIMENSIONS
    assert "Pillow" in exc.value.technical_detail


def test_specs_exposes_one_shared_local_path_syntax_policy() -> None:
    import rembggui.core.specs as specs_module

    policy = specs_module.is_local_path_syntax

    assert policy(Path("frames/frame.png"))
    assert policy(Path(r"C:\frames\frame.png"))
    assert not policy(Path("https://example.test/frame.png"))
    assert not policy(Path(r"file:\server\frame.png"))
    assert not policy(Path("//server/share/frame.png"))
    assert not policy(Path(r"\\server\share\frame.png"))


@pytest.mark.parametrize(
    "path",
    [
        Path("https://example.test/frame.png"),
        Path(r"file:\server\frame.png"),
        Path("//server/share/frame.png"),
        Path(r"\\server\share\frame.png"),
    ],
)
def test_webp_rejects_uri_and_network_path_syntax_before_filesystem_access(
    tmp_path: Path, path: Path
) -> None:
    local_source = save_rgba(tmp_path / "source.png", (128, 128), (1, 2, 3, 4))

    calls = (
        lambda: encode_lossless_webp((path,), (100,), tmp_path / "out.webp"),
        lambda: encode_lossless_webp((local_source,), (100,), path),
        lambda: fit_webp_to_size(
            (local_source,), (100,), 100_000, path, tmp_path / "out.webp"
        ),
    )
    for call in calls:
        with pytest.raises(AppError) as exc:
            call()
        assert exc.value.code is ErrorCode.INVALID_OUTPUT
        assert "local path syntax" in exc.value.technical_detail


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


def test_identical_rgba_frames_become_one_held_frame_with_exact_duration(
    tmp_path: Path,
) -> None:
    paths = tuple(
        save_rgba_with_compression(
            tmp_path / f"identical-{index}.png",
            (128, 128),
            (12, 34, 56, 255),
            index % 2 * 9,
        )
        for index in range(6)
    )
    assert paths[0].read_bytes() != paths[1].read_bytes()
    output = tmp_path / "held.webp"

    summary = encode_lossless_webp(paths, (67,) * 6, output)
    info = validate_webp(output, expected_frames=1, expected_duration_ms=402)

    assert summary.frames == 1
    assert summary.duration_ms == 402
    assert info.delays_ms == (402,)
    with Image.open(output) as encoded, Image.open(paths[0]) as source:
        assert encoded.n_frames == 1
        assert encoded.convert("RGBA").tobytes() == source.tobytes()


def test_overlong_identical_run_splits_duration_without_losing_time(
    tmp_path: Path,
) -> None:
    paths = tuple(
        save_rgba(tmp_path / f"overlong-{index}.png", (128, 128), (12, 34, 56, 255))
        for index in range(2)
    )
    delays = (8_388_608, 8_388_608)
    output = tmp_path / "overlong.webp"

    summary = encode_lossless_webp(paths, delays, output)
    info = validate_webp(output, expected_frames=2, expected_duration_ms=16_777_216)

    assert summary.frames == 2
    assert summary.duration_ms == 16_777_216
    assert info.delays_ms == (16_777_215, 1)


def test_mixed_identical_runs_keep_run_delays_and_total_duration(
    tmp_path: Path,
) -> None:
    colors = (
        (12, 34, 56, 255),
        (12, 34, 56, 255),
        (78, 90, 12, 255),
        (78, 90, 12, 255),
        (123, 45, 67, 255),
    )
    paths = tuple(
        save_rgba_with_compression(
            tmp_path / f"mixed-{index}.png",
            (128, 128),
            color,
            index % 2 * 9,
        )
        for index, color in enumerate(colors)
    )
    delays = (67, 66, 70, 71, 68)
    output = tmp_path / "mixed.webp"

    summary = encode_lossless_webp(paths, delays, output)
    info = validate_webp(output, expected_frames=3, expected_duration_ms=342)

    assert summary.frames == 3
    assert summary.duration_ms == 342
    assert info.delays_ms == (133, 141, 68)
    with Image.open(output) as encoded:
        decoded_pixels = tuple(
            frame.convert("RGBA").tobytes() for frame in ImageSequence.Iterator(encoded)
        )
    with (
        Image.open(paths[0]) as first,
        Image.open(paths[2]) as second,
        Image.open(paths[4]) as third,
    ):
        assert decoded_pixels == (first.tobytes(), second.tobytes(), third.tobytes())


def test_distinct_rgba_frames_keep_each_emitted_frame_and_delay(
    tmp_path: Path,
) -> None:
    output = tmp_path / "distinct.webp"
    paths = rgba_fixture_paths(tmp_path)

    summary = encode_lossless_webp(paths, (67, 66, 67), output)
    info = validate_webp(output, expected_frames=3, expected_duration_ms=200)

    assert summary.frames == 3
    assert summary.duration_ms == 200
    assert info.delays_ms == (67, 66, 67)


def test_single_rgba_frame_remains_a_valid_still(tmp_path: Path) -> None:
    source = save_rgba(tmp_path / "single.png", (128, 128), (12, 34, 56, 78))
    output = tmp_path / "single.webp"

    summary = encode_lossless_webp((source,), (402,), output)
    info = validate_webp(output, expected_frames=1, expected_duration_ms=0)

    assert summary.frames == 1
    assert summary.duration_ms == 0
    assert info.delays_ms == ()


def test_animated_cutouts_preserve_transparent_canvas_pixels(tmp_path: Path) -> None:
    paths: list[Path] = []
    for index, red in enumerate((0, 45)):
        image = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        for x in range(32, 96):
            for y in range(24, 104):
                image.putpixel((x, y), (red, 60, 90, 255))
        path = tmp_path / f"cut-{index}.png"
        image.save(path)
        image.close()
        paths.append(path)

    output = tmp_path / "cutouts.webp"
    encode_lossless_webp(tuple(paths), (500, 500), output)

    with Image.open(output) as encoded:
        assert tuple(
            frame.convert("RGBA").getpixel((0, 0))
            for frame in ImageSequence.Iterator(encoded)
        ) == ((0, 0, 0, 0), (0, 0, 0, 0))


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


def test_fit_cancels_between_encode_attempts_and_preserves_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = noisy_rgba(tmp_path / "source.png")
    output = tmp_path / "out.webp"
    output.write_bytes(b"known-good")
    actual_encode = webp_module.encode_lossless_webp
    encode_count = 0

    def report_too_large(
        paths: Sequence[Path], delays: Sequence[int], destination: Path
    ) -> EncodeSummary:
        nonlocal encode_count
        encode_count += 1
        summary = actual_encode(paths, delays, destination)
        return replace(summary, file_size=1_000_000)

    monkeypatch.setattr(webp_module, "encode_lossless_webp", report_too_large)

    with pytest.raises(AppError) as exc:
        fit_webp_to_size(
            (source,),
            (100,),
            100_000,
            tmp_path / "work",
            output,
            is_cancelled=lambda: encode_count == 1,
        )

    assert exc.value.code is ErrorCode.JOB_CANCELLED
    assert encode_count == 1
    assert output.read_bytes() == b"known-good"
    assert not tuple((tmp_path / "work").glob("webp-fit-*"))


def test_fit_cancels_snapshot_after_first_frame_and_releases_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = rgba_fixture_paths(tmp_path, count=3)
    output = tmp_path / "out.webp"
    output.write_bytes(b"known-good")
    tracker = RgbaOwnershipTracker((128, 128))
    actual_copy = webp_module.shutil.copyfileobj
    copied_frames = 0

    def count_snapshot_copy(input_file: Any, output_file: Any, length: int) -> None:
        nonlocal copied_frames
        actual_copy(input_file, output_file, length)
        if Path(output_file.name).parent.name == "source-snapshot":
            copied_frames += 1

    monkeypatch.setattr(webp_module.shutil, "copyfileobj", count_snapshot_copy)

    with pytest.raises(AppError) as exc:
        fit_webp_to_size(
            sources,
            (100, 100, 100),
            1_000_000,
            tmp_path / "work",
            output,
            is_cancelled=lambda: copied_frames >= 1,
            rgba_ownership_tracker=tracker,
        )

    gc.collect()
    assert exc.value.code is ErrorCode.JOB_CANCELLED
    assert copied_frames == 1
    assert output.read_bytes() == b"known-good"
    assert not tuple((tmp_path / "work").glob("webp-fit-*"))
    assert tracker.current == 0


def test_fit_cancels_resize_after_first_frame_and_releases_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = tuple(noisy_rgba(tmp_path / f"source-{index}.png") for index in range(3))
    for index, source_path in enumerate(sources):
        with Image.open(source_path) as image:
            image.load()
            image.putpixel((0, 0), (index * 70, 20, 40, 255))
            image.save(source_path)
    output = tmp_path / "out.webp"
    output.write_bytes(b"known-good")
    tracker = RgbaOwnershipTracker((256, 256))
    actual_encode = webp_module.encode_lossless_webp
    actual_save = Image.Image.save
    resized_frames = 0

    def report_too_large(
        paths: Sequence[Path], delays: Sequence[int], destination: Path, **kwargs: Any
    ) -> EncodeSummary:
        summary = actual_encode(paths, delays, destination, **kwargs)
        return replace(summary, file_size=1_000_000)

    def count_scaled_save(
        image: Image.Image, destination: object, *args: Any, **kwargs: Any
    ) -> None:
        nonlocal resized_frames
        actual_save(image, destination, *args, **kwargs)
        if isinstance(destination, Path) and destination.parent.name == "scaled-01":
            resized_frames += 1

    monkeypatch.setattr(webp_module, "encode_lossless_webp", report_too_large)
    monkeypatch.setattr(Image.Image, "save", count_scaled_save)

    with pytest.raises(AppError) as exc:
        fit_webp_to_size(
            sources,
            (100, 100, 100),
            100_000,
            tmp_path / "work",
            output,
            is_cancelled=lambda: resized_frames >= 1,
            rgba_ownership_tracker=tracker,
        )

    gc.collect()
    assert exc.value.code is ErrorCode.JOB_CANCELLED
    assert resized_frames == 1
    assert output.read_bytes() == b"known-good"
    assert not tuple((tmp_path / "work").glob("webp-fit-*"))
    assert tracker.current == 0


def test_fit_tracks_rgba_ownership_and_releases_all_frames(tmp_path: Path) -> None:
    source = noisy_rgba(tmp_path / "source.png", (256, 256))
    tracker = RgbaOwnershipTracker((256, 256))

    fit_webp_to_size(
        (source,),
        (100,),
        100_000,
        tmp_path / "work",
        tmp_path / "out.webp",
        rgba_ownership_tracker=tracker,
    )
    gc.collect()

    assert tracker.peak <= 3
    assert tracker.current == 0


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


def test_validation_of_an_open_binary_preserves_position_and_caller_ownership(
    tmp_path: Path,
) -> None:
    source = save_rgba(tmp_path / "source.png", (128, 128), (10, 30, 220, 91))
    output = tmp_path / "still.webp"
    encode_lossless_webp((source,), (100,), output)
    expected_tail = output.read_bytes()[7:15]

    with output.open("rb") as held:
        held.seek(7)
        info = validate_webp(held, expected_frames=1, expected_duration_ms=0)

        assert (info.width, info.height, info.frames) == (128, 128, 1)
        assert held.tell() == 7
        assert held.read(8) == expected_tail
        assert not held.closed


def test_validation_captures_an_open_binary_descriptor_exactly_once(
    tmp_path: Path,
) -> None:
    source = save_rgba(tmp_path / "source.png", (128, 128), (10, 30, 220, 91))
    valid = tmp_path / "valid.webp"
    encode_lossless_webp((source,), (100,), valid)
    invalid = tmp_path / "invalid.webp"
    invalid.write_bytes(b"X" + valid.read_bytes()[1:])

    class SwitchingBinary:
        def __init__(self, primary, substitute) -> None:
            self.primary = primary
            self.substitute = substitute
            self.fileno_calls = 0

        def fileno(self):
            self.fileno_calls += 1
            if self.fileno_calls == 1:
                return self.primary.fileno()
            return self.substitute.fileno()

        def read(self, size=-1):
            return self.primary.read(size)

        def seek(self, offset, whence=0):
            return self.primary.seek(offset, whence)

        def tell(self):
            return self.primary.tell()

    with invalid.open("rb") as primary, valid.open("rb") as substitute:
        primary.seek(11)
        switching = SwitchingBinary(primary, substitute)

        with pytest.raises(AppError) as exc:
            validate_webp(switching, expected_frames=1, expected_duration_ms=0)

        assert exc.value.code is ErrorCode.INVALID_OUTPUT
        assert switching.fileno_calls == 1
        assert primary.tell() == 11
        assert not primary.closed
        assert not substitute.closed


@pytest.mark.parametrize("fileno_result", [True, "3", -1])
def test_validation_rejects_non_exact_or_negative_binary_descriptors(
    fileno_result: object,
) -> None:
    class MalformedBinary:
        fileno_calls = 0

        def fileno(self):
            self.fileno_calls += 1
            return fileno_result

        def read(self, size=-1):
            del size
            return b""

        def seek(self, offset, whence=0):
            del whence
            return offset

        def tell(self):
            return 0

    source = MalformedBinary()

    with pytest.raises(AppError) as exc:
        validate_webp(source, expected_frames=1, expected_duration_ms=0)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert source.fileno_calls == 1


def test_validation_closes_the_duplicated_descriptor_when_tell_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = save_rgba(tmp_path / "source.png", (128, 128), (10, 30, 220, 91))
    valid = tmp_path / "valid.webp"
    encode_lossless_webp((source,), (100,), valid)
    duplicated: list[int] = []
    actual_dup = os.dup

    class ExplodingTell:
        def __init__(self, held) -> None:
            self.held = held

        def fileno(self):
            return self.held.fileno()

        def read(self, size=-1):
            return self.held.read(size)

        def seek(self, offset, whence=0):
            return self.held.seek(offset, whence)

        def tell(self):
            raise RuntimeError("synthetic tell failure")

    def observe_dup(descriptor: int) -> int:
        duplicate = actual_dup(descriptor)
        duplicated.append(duplicate)
        return duplicate

    monkeypatch.setattr(webp_module.os, "dup", observe_dup)

    with valid.open("rb") as held:
        with pytest.raises(RuntimeError, match="synthetic tell failure"):
            validate_webp(ExplodingTell(held), 1, 0)

        assert not held.closed

    assert len(duplicated) == 1
    with pytest.raises(OSError):
        os.fstat(duplicated[0])


def test_animated_webp_stores_each_odd_delay_exactly(tmp_path: Path) -> None:
    paths = rgba_fixture_paths(tmp_path, count=6)
    output = tmp_path / "odd-delays.webp"

    encode_lossless_webp(paths, (17, 16, 17, 17, 16, 17), output)

    info = validate_webp(output, 6, 100)

    assert info.duration_ms == 100
    assert info.delays_ms == (17, 16, 17, 17, 16, 17)


def test_direct_encode_rejects_same_total_delay_redistribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out.webp"
    output.write_bytes(b"existing")
    actual_rewrite = webp_module._rewrite_animation_durations

    def redistribute(path: Path, delays_ms: tuple[int, ...]) -> None:
        actual_rewrite(path, delays_ms)
        offsets = webp_module._animation_duration_offsets(path)
        with path.open("r+b") as encoded:
            for offset, delay in zip(offsets, (66, 66, 68), strict=True):
                encoded.seek(offset)
                encoded.write(delay.to_bytes(3, "little"))

    monkeypatch.setattr(webp_module, "_rewrite_animation_durations", redistribute)

    with pytest.raises(AppError) as exc:
        encode_lossless_webp(rgba_fixture_paths(tmp_path), (67, 66, 67), output)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert output.read_bytes() == b"existing"


@pytest.mark.parametrize(
    ("mutation", "detail"),
    [
        ("vp8x-reserved-flag", "reserved"),
        ("vp8x-reserved-bytes", "reserved"),
        ("vp8x-missing-animation", "animation flag"),
        ("vp8x-duplicate", "VP8X"),
        ("anim-before-vp8x", "VP8X"),
        ("anim-duplicate", "ANIM"),
        ("anmf-reserved", "reserved"),
        ("anmf-zero-duration", "positive"),
        ("anmf-outside-canvas", "canvas"),
        ("anmf-vp8l-size-mismatch", "dimensions"),
        ("nested-lossy-vp8", "lossless"),
        ("nested-vp8l-duplicate", "exactly one"),
        ("alpha-flag-mismatch", "alpha"),
        ("nonzero-padding", "padding"),
    ],
)
def test_strict_riff_state_machine_rejects_real_byte_mutations(
    tmp_path: Path, mutation: str, detail: str
) -> None:
    output = tmp_path / "animation.webp"
    encode_lossless_webp(rgba_fixture_paths(tmp_path), (67, 66, 67), output)
    output.write_bytes(mutate_animation_bytes(bytearray(output.read_bytes()), mutation))

    with pytest.raises(AppError) as exc:
        validate_webp(output, expected_frames=3, expected_duration_ms=200)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert detail in exc.value.technical_detail


def test_strict_still_shape_rejects_a_duplicate_vp8l_chunk(tmp_path: Path) -> None:
    source = save_rgba(tmp_path / "still.png", (128, 128), (1, 2, 3, 4))
    output = tmp_path / "still.webp"
    encode_lossless_webp((source,), (100,), output)
    data = bytearray(output.read_bytes())
    chunk = riff_chunks(data)[0]
    data.extend(data[chunk[0] : chunk[3]])
    set_riff_size(data)
    output.write_bytes(data)

    with pytest.raises(AppError) as exc:
        validate_webp(output, expected_frames=1, expected_duration_ms=0)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert "single VP8L" in exc.value.technical_detail


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


def test_encode_rejects_symlink_frame_inputs(tmp_path: Path) -> None:
    source = save_rgba(tmp_path / "source.png", (128, 128), (1, 2, 3, 4))
    linked = tmp_path / "linked.png"
    try:
        linked.symlink_to(source)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    with pytest.raises(AppError) as exc:
        encode_lossless_webp((linked,), (100,), tmp_path / "out.webp")

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert "symlink" in exc.value.technical_detail


def test_direct_encode_rejects_identical_path_replacement_during_encode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = save_rgba(tmp_path / "source.png", (128, 128), (1, 2, 3, 4))
    replacement = save_rgba(tmp_path / "replacement.png", (128, 128), (1, 2, 3, 4))
    output = tmp_path / "out.webp"
    output.write_bytes(b"existing")
    actual_encode = webp_module._encode_still

    def replace_after_encode(*args: Any) -> None:
        actual_encode(*args)
        os.replace(replacement, source)

    monkeypatch.setattr(webp_module, "_encode_still", replace_after_encode)

    with pytest.raises(AppError) as exc:
        encode_lossless_webp((source,), (100,), output)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert "changed" in exc.value.technical_detail
    assert output.read_bytes() == b"existing"


def test_fit_encodes_only_from_a_private_source_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = save_rgba(tmp_path / "source.png", (128, 128), (1, 2, 3, 4))
    actual_encode = webp_module.encode_lossless_webp
    observed: list[tuple[Path, ...]] = []

    def observe_sources(
        paths: Sequence[Path], delays: Sequence[int], destination: Path
    ) -> EncodeSummary:
        observed.append(tuple(paths))
        return actual_encode(paths, delays, destination)

    monkeypatch.setattr(webp_module, "encode_lossless_webp", observe_sources)

    fit_webp_to_size(
        (source,), (100,), 1_000_000, tmp_path / "work", tmp_path / "out.webp"
    )

    assert observed
    assert all(paths[0] != source for paths in observed)
    assert all(paths[0].parent.name == "source-snapshot" for paths in observed)


def test_fit_rejects_concurrent_source_replacement_during_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = save_rgba(tmp_path / "source.png", (128, 128), (1, 2, 3, 4))
    replacement = save_rgba(tmp_path / "replacement.png", (128, 128), (1, 2, 3, 4))
    output = tmp_path / "out.webp"
    output.write_bytes(b"existing")
    actual_copy = webp_module.shutil.copyfileobj

    def replace_during_copy(input_file: Any, output_file: Any, length: int) -> None:
        actual_copy(input_file, output_file, length)
        if Path(input_file.name) == source:
            os.replace(replacement, source)

    monkeypatch.setattr(webp_module.shutil, "copyfileobj", replace_during_copy)

    with pytest.raises(AppError) as exc:
        fit_webp_to_size((source,), (100,), 1_000_000, tmp_path / "work", output)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert "changed" in exc.value.technical_detail
    assert output.read_bytes() == b"existing"


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

    def cancel_encode(_source: Path, _identity: object, temporary: Path) -> None:
        temporary.write_bytes(b"partial")
        raise Cancelled

    monkeypatch.setattr(webp_module, "_encode_still", cancel_encode)

    with pytest.raises(Cancelled):
        encode_lossless_webp((source,), (100,), output)

    assert output.read_bytes() == b"existing"
    assert not tuple(tmp_path.glob(".out.webp.*.tmp.webp"))


@pytest.mark.parametrize(
    ("error_number", "retry_action"),
    [
        (errno.ENOSPC, "free-disk-space"),
        (getattr(errno, "EDQUOT", errno.ENOSPC), "free-disk-space"),
        (errno.EACCES, "choose-writable-output"),
        (getattr(errno, "EROFS", errno.EACCES), "choose-writable-output"),
    ],
)
def test_encode_maps_actual_filesystem_failures_and_preserves_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
    retry_action: str,
) -> None:
    source = save_rgba(tmp_path / "source.png", (128, 128), (1, 2, 3, 4))
    output = tmp_path / "out.webp"
    output.write_bytes(b"existing")

    def fail_encode(*_args: Any) -> None:
        raise OSError(error_number, "synthetic encoder filesystem failure")

    monkeypatch.setattr(webp_module, "_encode_still", fail_encode)

    with pytest.raises(AppError) as exc:
        encode_lossless_webp((source,), (100,), output)

    assert exc.value.retry_action == retry_action
    assert output.read_bytes() == b"existing"


def test_primary_exception_keeps_cleanup_failure_as_an_observable_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Cancelled(BaseException):
        pass

    source = save_rgba(tmp_path / "source.png", (128, 128), (1, 2, 3, 4))
    output = tmp_path / "out.webp"
    output.write_bytes(b"existing")
    actual_unlink = Path.unlink

    def cancel_encode(*args: Any) -> None:
        temporary = args[-1]
        temporary.write_bytes(b"partial")
        raise Cancelled("cancelled")

    def fail_partial_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.name.startswith(".out.webp."):
            raise OSError("injected unlink failure")
        actual_unlink(path, *args, **kwargs)

    monkeypatch.setattr(webp_module, "_encode_still", cancel_encode)
    monkeypatch.setattr(Path, "unlink", fail_partial_unlink)

    with pytest.raises(Cancelled) as exc:
        encode_lossless_webp((source,), (100,), output)

    assert any("cleanup" in note for note in exc.value.__notes__)
    assert output.read_bytes() == b"existing"
    assert tuple(tmp_path.glob(".out.webp.*.tmp.webp"))


def test_successful_fit_cleanup_failure_prevents_destination_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = save_rgba(tmp_path / "source.png", (128, 128), (1, 2, 3, 4))
    output = tmp_path / "out.webp"
    output.write_bytes(b"existing")
    actual_rmtree = webp_module.shutil.rmtree

    def fail_scratch(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path).name.startswith("webp-fit-"):
            raise OSError("injected rmtree failure")
        actual_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(webp_module.shutil, "rmtree", fail_scratch)

    with pytest.raises(AppError) as exc:
        fit_webp_to_size((source,), (100,), 1_000_000, tmp_path / "work", output)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert "cleanup" in exc.value.technical_detail
    assert output.read_bytes() == b"existing"


def test_primary_fit_error_survives_rmtree_failure_with_cleanup_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = save_rgba(tmp_path / "source.png", (128, 128), (1, 2, 3, 4))
    output = tmp_path / "out.webp"
    output.write_bytes(b"existing")
    actual_rmtree = webp_module.shutil.rmtree

    def fail_scratch(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path).name.startswith("webp-fit-"):
            raise OSError("injected rmtree failure")
        actual_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(webp_module.shutil, "rmtree", fail_scratch)

    with pytest.raises(AppError) as exc:
        fit_webp_to_size((source,), (100,), 1, tmp_path / "work", output)

    assert exc.value.code is ErrorCode.IMPOSSIBLE_SIZE
    assert any("cleanup" in note for note in exc.value.__notes__)
    assert output.read_bytes() == b"existing"


def test_mkstemp_descriptor_close_failure_cleans_created_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = save_rgba(tmp_path / "source.png", (128, 128), (1, 2, 3, 4))
    output = tmp_path / "out.webp"
    output.write_bytes(b"existing")
    actual_close = webp_module.os.close

    def close_then_fail(descriptor: int) -> None:
        actual_close(descriptor)
        raise OSError("injected descriptor close failure")

    monkeypatch.setattr(webp_module.os, "close", close_then_fail)

    with pytest.raises(AppError) as exc:
        encode_lossless_webp((source,), (100,), output)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
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
        paths: tuple[Path, ...],
        size: tuple[int, int],
        destination: Path,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[Path, ...]:
        resize_sources.append(paths)
        prior_scaled_directory_counts.append(
            len(tuple(destination.parent.glob("scaled-*")))
        )
        return actual_resize(
            paths,
            size,
            destination,
            is_cancelled=is_cancelled,
        )

    monkeypatch.setattr(webp_module, "encode_lossless_webp", always_too_large)
    monkeypatch.setattr(webp_module, "_resize_from_sources", observe_resize)

    with pytest.raises(AppError) as exc:
        fit_webp_to_size(sources, (100,), 999, tmp_path / "work", output)

    assert exc.value.code is ErrorCode.IMPOSSIBLE_SIZE
    assert encode_count == 12
    assert len(resize_sources) == 11
    assert all(paths == resize_sources[0] for paths in resize_sources)
    assert resize_sources[0] != sources
    assert resize_sources[0][0].parent.name == "source-snapshot"
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


def test_fit_rejects_wrong_but_valid_final_copy_before_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = save_rgba(tmp_path / "source.png", (128, 128), (1, 2, 3, 4))
    wrong_source = save_rgba(tmp_path / "wrong.png", (129, 128), (20, 30, 40, 50))
    wrong_webp = tmp_path / "wrong.webp"
    encode_lossless_webp((wrong_source,), (100,), wrong_webp)
    output = tmp_path / "out.webp"
    output.write_bytes(b"existing")
    actual_copy = webp_module.shutil.copyfileobj

    def substitute_valid_webp(source_file: Any, destination: Any, length: int) -> None:
        if Path(source_file.name).suffix == ".webp":
            with wrong_webp.open("rb") as replacement:
                actual_copy(replacement, destination, length)
        else:
            actual_copy(source_file, destination, length)

    monkeypatch.setattr(webp_module.shutil, "copyfileobj", substitute_valid_webp)

    with pytest.raises(AppError) as exc:
        fit_webp_to_size((source,), (100,), 1_000_000, tmp_path / "work", output)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert "dimensions" in exc.value.technical_detail
    assert output.read_bytes() == b"existing"


def test_fit_rejects_same_total_delay_redistribution_during_final_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = rgba_fixture_paths(tmp_path)
    output = tmp_path / "out.webp"
    output.write_bytes(b"existing")
    actual_copy = webp_module.shutil.copyfileobj

    def redistribute_final_copy(
        source_file: Any, destination: Any, length: int
    ) -> None:
        if Path(source_file.name).suffix == ".webp":
            mutated = replace_animation_delays(
                bytearray(source_file.read()), (66, 66, 68)
            )
            destination.write(mutated)
        else:
            actual_copy(source_file, destination, length)

    monkeypatch.setattr(webp_module.shutil, "copyfileobj", redistribute_final_copy)

    with pytest.raises(AppError) as exc:
        fit_webp_to_size(
            sources,
            (67, 66, 67),
            1_000_000,
            tmp_path / "work",
            output,
        )

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert "delays" in exc.value.technical_detail
    assert output.read_bytes() == b"existing"


def test_fit_rejects_wrong_pixels_with_valid_dimensions_during_final_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = save_rgba(tmp_path / "source.png", (128, 128), (1, 2, 3, 4))
    wrong_source = save_rgba(tmp_path / "wrong.png", (128, 128), (20, 30, 40, 50))
    wrong_webp = tmp_path / "wrong.webp"
    encode_lossless_webp((wrong_source,), (100,), wrong_webp)
    output = tmp_path / "out.webp"
    output.write_bytes(b"existing")
    actual_copy = webp_module.shutil.copyfileobj

    def substitute_pixels(source_file: Any, destination: Any, length: int) -> None:
        if Path(source_file.name).suffix == ".webp":
            with wrong_webp.open("rb") as replacement:
                actual_copy(replacement, destination, length)
        else:
            actual_copy(source_file, destination, length)

    monkeypatch.setattr(webp_module.shutil, "copyfileobj", substitute_pixels)

    with pytest.raises(AppError) as exc:
        fit_webp_to_size((source,), (100,), 1_000_000, tmp_path / "work", output)

    assert exc.value.code is ErrorCode.INVALID_OUTPUT
    assert "RGBA source" in exc.value.technical_detail
    assert output.read_bytes() == b"existing"
