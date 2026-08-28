from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from rembggui.core.errors import AppError, ErrorCode, ValidationError
from rembggui.core.fingerprints import (
    complete_source_sha256,
    cut_cache_key,
    preview_fingerprint,
    provisional_source_fingerprint,
    render_fingerprint,
)
from rembggui.core.specs import (
    CollisionPolicy,
    CropSpec,
    EdgeMode,
    FramingSpec,
    OutputSpec,
    RenderRequest,
    SamplingSpec,
    SegmentationSpec,
)

SOURCE_SHA = "01" * 32
MODEL_SHA = "ab" * 32


def request_a(tmp_path: Path) -> RenderRequest:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"0123456789abcdef")
    output_dir = tmp_path / "output"
    output_dir.mkdir(exist_ok=True)
    return RenderRequest(
        source=source,
        sampling=SamplingSpec(Fraction(1, 10), Fraction(9, 10), 30),
        crop=CropSpec(2, 3, 128, 192),
        segmentation=SegmentationSpec("birefnet-portrait", EdgeMode.STANDARD),
        framing=FramingSpec(True, Decimal("2.0"), 8, Decimal("1.25")),
        output=OutputSpec(output_dir, "sprite.webp", 1_000_000),
    )


def test_provisional_fingerprint_changes_with_path_metadata_and_edge_chunks(
    tmp_path: Path,
) -> None:
    source_a = tmp_path / "a.mp4"
    source_b = tmp_path / "b.mp4"
    source_a.write_bytes(b"HEAD-middle-TAIL")
    source_b.write_bytes(b"HEAD-middle-TAIL")

    first = provisional_source_fingerprint(source_a, chunk_size=4)
    duplicate_at_new_path = provisional_source_fingerprint(source_b, chunk_size=4)
    source_a.write_bytes(b"HEAd-middle-TAIl")
    changed_edges = provisional_source_fingerprint(source_a, chunk_size=4)

    assert first != duplicate_at_new_path
    assert first != changed_edges


@pytest.mark.parametrize("payload", [b"", b"abc", b"abcdefgh"])
def test_provisional_fingerprint_handles_empty_and_overlapping_chunks(
    tmp_path: Path, payload: bytes
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(payload)
    source_stat = source.stat()
    canonical_json = json.dumps(
        {
            "canonical_path": str(source.resolve(strict=True)),
            "fingerprint_schema": "rembggui-fingerprint",
            "fingerprint_schema_version": 1,
            "head_sha256": hashlib.sha256(payload).hexdigest(),
            "kind": "provisional-source",
            "mtime_ns": source_stat.st_mtime_ns,
            "size": len(payload),
            "tail_sha256": hashlib.sha256(payload).hexdigest(),
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    assert provisional_source_fingerprint(source, chunk_size=8) == hashlib.sha256(
        canonical_json.encode("utf-8")
    ).hexdigest()


def test_complete_hash_promotes_file_content_independently_of_path(
    tmp_path: Path,
) -> None:
    source_a = tmp_path / "a.mp4"
    source_b = tmp_path / "b.mp4"
    payload = b"streamed-video-payload" * 10_000
    source_a.write_bytes(payload)
    source_b.write_bytes(payload)

    assert complete_source_sha256(source_a, chunk_size=997) == hashlib.sha256(
        payload
    ).hexdigest()
    assert complete_source_sha256(source_a, chunk_size=997) == complete_source_sha256(
        source_b, chunk_size=4096
    )


def test_complete_hash_supports_an_empty_source(tmp_path: Path) -> None:
    source = tmp_path / "empty.mp4"
    source.write_bytes(b"")

    assert complete_source_sha256(source) == hashlib.sha256(b"").hexdigest()


def test_complete_hash_rejects_a_source_that_changes_during_streaming(
    tmp_path: Path,
) -> None:
    source = tmp_path / "changing.mp4"
    source.write_bytes(b"x" * 1_000_000)
    stop = threading.Event()

    def mutate_metadata() -> None:
        time.sleep(0.005)
        while not stop.is_set():
            os.utime(source, None)

    writer = threading.Thread(target=mutate_metadata)
    writer.start()
    try:
        with pytest.raises(AppError) as exc:
            complete_source_sha256(source, chunk_size=1)
    finally:
        stop.set()
        writer.join()

    assert exc.value.code is ErrorCode.SOURCE_CHANGED
    assert exc.value.retry_action == "reload-source"


def test_output_path_does_not_stale_segmentation(tmp_path: Path) -> None:
    request = request_a(tmp_path)
    new_output_dir = tmp_path / "elsewhere"
    new_output_dir.mkdir()
    changed_output = replace(
        request,
        output=OutputSpec(
            new_output_dir,
            "renamed.webp",
            request.output.max_bytes,
            CollisionPolicy.REPLACE,
        ),
    )

    assert preview_fingerprint(request) == preview_fingerprint(changed_output)
    assert cut_cache_key(
        request, source_sha256=SOURCE_SHA, model_weight_sha256=MODEL_SHA
    ) == cut_cache_key(
        changed_output, source_sha256=SOURCE_SHA, model_weight_sha256=MODEL_SHA
    )


@pytest.mark.parametrize(
    "changed_request",
    [
        lambda request: replace(
            request,
            sampling=SamplingSpec(Fraction(1, 10), Fraction(4, 5), 30),
        ),
        lambda request: replace(request, crop=CropSpec(3, 3, 128, 192)),
        lambda request: replace(
            request,
            segmentation=SegmentationSpec("isnet-general-use", EdgeMode.STANDARD),
        ),
        lambda request: replace(
            request,
            segmentation=SegmentationSpec(
                "birefnet-portrait", EdgeMode.ALPHA_MATTING
            ),
        ),
    ],
)
def test_cut_key_invalidates_only_for_authoritative_cut_inputs(
    tmp_path: Path, changed_request: object
) -> None:
    request = request_a(tmp_path)
    changed = changed_request(request)  # type: ignore[operator]

    assert cut_cache_key(
        request, source_sha256=SOURCE_SHA, model_weight_sha256=MODEL_SHA
    ) != cut_cache_key(
        changed, source_sha256=SOURCE_SHA, model_weight_sha256=MODEL_SHA
    )


def test_cut_key_tracks_source_model_pipeline_and_color_identities(
    tmp_path: Path,
) -> None:
    request = request_a(tmp_path)
    baseline = cut_cache_key(
        request, source_sha256=SOURCE_SHA, model_weight_sha256=MODEL_SHA
    )

    variants = (
        cut_cache_key(request, source_sha256="02" * 32, model_weight_sha256=MODEL_SHA),
        cut_cache_key(request, source_sha256=SOURCE_SHA, model_weight_sha256="ac" * 32),
        cut_cache_key(
            request,
            source_sha256=SOURCE_SHA,
            model_weight_sha256=MODEL_SHA,
            pipeline_schema_version="pipeline-v2",
        ),
        cut_cache_key(
            request,
            source_sha256=SOURCE_SHA,
            model_weight_sha256=MODEL_SHA,
            orientation_color_version="orientation-color-v2",
        ),
        cut_cache_key(
            request,
            source_sha256=SOURCE_SHA,
            model_weight_sha256=MODEL_SHA,
            rembg_version="2.0.73",
        ),
    )

    assert all(variant != baseline for variant in variants)


def test_framing_and_output_limits_do_not_invalidate_cut_reuse(tmp_path: Path) -> None:
    request = request_a(tmp_path)
    changed = replace(
        request,
        framing=FramingSpec(False, Decimal("75"), 22, Decimal("0.75")),
        output=replace(request.output, max_bytes=250_000),
    )

    assert cut_cache_key(
        request, source_sha256=SOURCE_SHA, model_weight_sha256=MODEL_SHA
    ) == cut_cache_key(
        changed, source_sha256=SOURCE_SHA, model_weight_sha256=MODEL_SHA
    )
    assert preview_fingerprint(
        request, source_fingerprint=SOURCE_SHA
    ) != preview_fingerprint(changed, source_fingerprint=SOURCE_SHA)


def test_render_size_limit_does_not_invalidate_preview(tmp_path: Path) -> None:
    request = request_a(tmp_path)
    changed = replace(request, output=replace(request.output, max_bytes=250_000))

    assert preview_fingerprint(
        request, source_fingerprint=SOURCE_SHA
    ) == preview_fingerprint(changed, source_fingerprint=SOURCE_SHA)


def test_preview_uses_supplied_source_identity_without_reading_source(
    tmp_path: Path,
) -> None:
    request = request_a(tmp_path)
    supplied = provisional_source_fingerprint(request.source)
    request.source.unlink()

    assert preview_fingerprint(request, source_fingerprint=supplied) == (
        preview_fingerprint(request, source_fingerprint=supplied)
    )


def test_preview_tracks_all_content_layers_it_consumes(tmp_path: Path) -> None:
    request = request_a(tmp_path)
    source_identity = "cd" * 32
    baseline = preview_fingerprint(request, source_fingerprint=source_identity)

    variants = (
        preview_fingerprint(
            replace(request, sampling=SamplingSpec(Fraction(0), Fraction(1), 24)),
            source_fingerprint=source_identity,
        ),
        preview_fingerprint(
            replace(request, crop=CropSpec(1, 3, 128, 192)),
            source_fingerprint=source_identity,
        ),
        preview_fingerprint(
            replace(
                request,
                segmentation=SegmentationSpec(
                    "birefnet-portrait", EdgeMode.DECONTAMINATE_COLORS
                ),
            ),
            source_fingerprint=source_identity,
        ),
        preview_fingerprint(
            replace(request, framing=replace(request.framing, padding=9)),
            source_fingerprint=source_identity,
        ),
        preview_fingerprint(
            request,
            source_fingerprint=source_identity,
            orientation_color_version="orientation-color-v2",
        ),
    )

    assert all(variant != baseline for variant in variants)


def test_render_tracks_framing_and_size_but_not_destination_or_job_mode(
    tmp_path: Path,
) -> None:
    request = request_a(tmp_path)
    cut_key = "ef" * 32
    baseline = render_fingerprint(request, cut_key=cut_key)
    new_output_dir = tmp_path / "new-output"
    new_output_dir.mkdir()

    destination_only = replace(
        request,
        output=OutputSpec(
            new_output_dir,
            "new-name.webp",
            request.output.max_bytes,
            CollisionPolicy.REPLACE,
        ),
        rebuild=True,
    )
    new_framing = replace(request, framing=replace(request.framing, trim=False))
    new_limit = replace(request, output=replace(request.output, max_bytes=999_999))

    assert render_fingerprint(destination_only, cut_key=cut_key) == baseline
    assert render_fingerprint(new_framing, cut_key=cut_key) != baseline
    assert render_fingerprint(new_limit, cut_key=cut_key) != baseline
    assert render_fingerprint(request, cut_key="ee" * 32) != baseline


@pytest.mark.parametrize(
    "function_call",
    [
        lambda request: cut_cache_key(
            request, source_sha256="bad", model_weight_sha256=MODEL_SHA
        ),
        lambda request: cut_cache_key(
            request, source_sha256=SOURCE_SHA, model_weight_sha256="xyz" * 21 + "x"
        ),
        lambda request: cut_cache_key(
            request,
            source_sha256=SOURCE_SHA,
            model_weight_sha256=MODEL_SHA,
            pipeline_schema_version="",
        ),
        lambda request: preview_fingerprint(request, source_fingerprint="bad"),
        lambda request: render_fingerprint(request, cut_key="bad"),
    ],
)
def test_fingerprints_reject_malformed_canonical_identities(
    tmp_path: Path, function_call: object
) -> None:
    with pytest.raises(ValidationError) as exc:
        function_call(request_a(tmp_path))  # type: ignore[operator]

    assert exc.value.code is ErrorCode.INVALID_RENDER_REQUEST


def test_cut_key_uses_a_canonical_versioned_json_schema(tmp_path: Path) -> None:
    request = request_a(tmp_path)
    canonical_json = json.dumps(
        {
            "crop": {"height": 192, "width": 128, "x": 2, "y": 3},
            "edge_settings": {"mode": "standard"},
            "fingerprint_schema": "rembggui-fingerprint",
            "fingerprint_schema_version": 1,
            "kind": "cut-cache-key",
            "model": {"id": "birefnet-portrait", "weight_sha256": MODEL_SHA},
            "orientation_color_version": "orientation-color-v1",
            "pipeline_schema_version": "pipeline-v1",
            "rembg_version": "2.0.72",
            "sampling": {
                "end": {"denominator": 10, "numerator": 9},
                "fps": 30,
                "start": {"denominator": 10, "numerator": 1},
            },
            "source_sha256": SOURCE_SHA,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    assert cut_cache_key(
        request, source_sha256=SOURCE_SHA, model_weight_sha256=MODEL_SHA
    ) == hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
