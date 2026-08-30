from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import replace
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO

import pytest

import rembggui.core.fingerprints as fingerprints_module
from rembggui.core.errors import AppError, ErrorCode, ValidationError
from rembggui.core.fingerprints import (
    complete_source_sha256,
    cut_cache_key,
    cut_cache_key_inputs,
    provisional_source_fingerprint,
    render_fingerprint,
    union_fingerprint,
)
from rembggui.core.fingerprints import (
    preview_fingerprint as _preview_fingerprint,
)
from rembggui.core.specs import (
    AlphaMattingSpec,
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


def preview_fingerprint(
    request: RenderRequest,
    playhead: Fraction,
    **identities: object,
) -> str:
    """Call the public preview identity with one explicit prepared binding."""
    identities.setdefault("model_weight_sha256", MODEL_SHA)
    return _preview_fingerprint(
        request,
        playhead,
        **identities,  # type: ignore[arg-type]
    )


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

    assert (
        provisional_source_fingerprint(source, chunk_size=8)
        == hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    )


def test_complete_hash_promotes_file_content_independently_of_path(
    tmp_path: Path,
) -> None:
    source_a = tmp_path / "a.mp4"
    source_b = tmp_path / "b.mp4"
    payload = b"streamed-video-payload" * 10_000
    source_a.write_bytes(payload)
    source_b.write_bytes(payload)

    assert (
        complete_source_sha256(source_a, chunk_size=997)
        == hashlib.sha256(payload).hexdigest()
    )
    assert complete_source_sha256(source_a, chunk_size=997) == complete_source_sha256(
        source_b, chunk_size=4096
    )


def test_complete_hash_supports_an_empty_source(tmp_path: Path) -> None:
    source = tmp_path / "empty.mp4"
    source.write_bytes(b"")

    assert complete_source_sha256(source) == hashlib.sha256(b"").hexdigest()


def test_complete_hash_rejects_a_source_that_changes_during_streaming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "changing.mp4"
    source.write_bytes(b"x" * 1_000_000)
    initial_stat = source.stat()
    read_boundary_entered = threading.Event()
    mutation_completed = threading.Event()
    mutation_errors: list[BaseException] = []
    original_update_digest = fingerprints_module._update_digest

    def mutate_metadata() -> None:
        if not read_boundary_entered.wait(timeout=1):
            mutation_errors.append(
                AssertionError("hashing never reached the read boundary")
            )
            return
        os.utime(
            source,
            ns=(initial_stat.st_atime_ns, initial_stat.st_mtime_ns + 1_000_000_000),
        )
        mutation_completed.set()

    def gated_update_digest(
        source_file: BinaryIO, digest: object, chunk_size: int
    ) -> int:
        class ReadBoundary:
            def read(self, size: int) -> bytes:
                read_boundary_entered.set()
                if not mutation_completed.wait(timeout=1):
                    raise AssertionError("metadata mutation did not complete")
                return source_file.read(size)

        return original_update_digest(ReadBoundary(), digest, chunk_size)

    monkeypatch.setattr(fingerprints_module, "_update_digest", gated_update_digest)

    writer = threading.Thread(target=mutate_metadata)
    writer.start()
    try:
        with pytest.raises(AppError) as exc:
            complete_source_sha256(source, chunk_size=1_000_000)
    finally:
        writer.join(timeout=1)

    assert not writer.is_alive()
    assert not mutation_errors
    assert read_boundary_entered.is_set()
    assert mutation_completed.is_set()

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

    assert preview_fingerprint(request, Fraction(1, 5)) == preview_fingerprint(
        changed_output, Fraction(1, 5)
    )
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
            segmentation=SegmentationSpec("birefnet-portrait", EdgeMode.ALPHA_MATTING),
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
    ) != cut_cache_key(changed, source_sha256=SOURCE_SHA, model_weight_sha256=MODEL_SHA)


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
    ) == cut_cache_key(changed, source_sha256=SOURCE_SHA, model_weight_sha256=MODEL_SHA)
    assert preview_fingerprint(
        request, Fraction(1, 5), source_fingerprint=SOURCE_SHA
    ) != preview_fingerprint(changed, Fraction(1, 5), source_fingerprint=SOURCE_SHA)


def test_render_size_limit_does_not_invalidate_preview(tmp_path: Path) -> None:
    request = request_a(tmp_path)
    changed = replace(request, output=replace(request.output, max_bytes=250_000))

    assert preview_fingerprint(
        request, Fraction(1, 5), source_fingerprint=SOURCE_SHA
    ) == preview_fingerprint(changed, Fraction(1, 5), source_fingerprint=SOURCE_SHA)


def test_preview_uses_supplied_source_identity_without_reading_source(
    tmp_path: Path,
) -> None:
    request = request_a(tmp_path)
    supplied = provisional_source_fingerprint(request.source)
    request.source.unlink()

    assert preview_fingerprint(
        request, Fraction(1, 5), source_fingerprint=supplied
    ) == preview_fingerprint(request, Fraction(1, 5), source_fingerprint=supplied)


def test_preview_tracks_all_content_layers_it_consumes(tmp_path: Path) -> None:
    request = request_a(tmp_path)
    source_identity = "cd" * 32
    baseline = preview_fingerprint(
        request, Fraction(1, 5), source_fingerprint=source_identity
    )

    variants = (
        preview_fingerprint(
            replace(request, sampling=SamplingSpec(Fraction(0), Fraction(1), 24)),
            Fraction(1, 5),
            source_fingerprint=source_identity,
        ),
        preview_fingerprint(
            replace(request, crop=CropSpec(1, 3, 128, 192)),
            Fraction(1, 5),
            source_fingerprint=source_identity,
        ),
        preview_fingerprint(
            replace(
                request,
                segmentation=SegmentationSpec(
                    "birefnet-portrait", EdgeMode.DECONTAMINATE_COLORS
                ),
            ),
            Fraction(1, 5),
            source_fingerprint=source_identity,
        ),
        preview_fingerprint(
            replace(request, framing=replace(request.framing, padding=9)),
            Fraction(1, 5),
            source_fingerprint=source_identity,
        ),
        preview_fingerprint(
            request,
            Fraction(1, 5),
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


def test_union_identity_tracks_only_cut_content_and_alpha_threshold(
    tmp_path: Path,
) -> None:
    request = request_a(tmp_path)
    cut_key = "ef" * 32
    baseline = union_fingerprint(request, cut_key=cut_key)
    new_output = replace(request, output=replace(request.output, max_bytes=17))
    new_padding = replace(request, framing=replace(request.framing, padding=99))
    new_threshold = replace(
        request,
        framing=replace(request.framing, alpha_threshold=Decimal("3")),
    )

    assert union_fingerprint(new_output, cut_key=cut_key) == baseline
    assert union_fingerprint(new_padding, cut_key=cut_key) == baseline
    assert union_fingerprint(new_threshold, cut_key=cut_key) != baseline


def test_render_fingerprint_distinguishes_long_exact_decimal_values(
    tmp_path: Path,
) -> None:
    request = request_a(tmp_path)
    first = replace(
        request,
        framing=replace(
            request.framing,
            stretch_x=Decimal("1.123456789012345678901234567890123456789"),
        ),
    )
    second = replace(
        request,
        framing=replace(
            request.framing,
            stretch_x=Decimal("1.123456789012345678901234567890123456788"),
        ),
    )

    assert render_fingerprint(first, cut_key="ef" * 32) != render_fingerprint(
        second, cut_key="ef" * 32
    )


def test_render_fingerprint_canonicalizes_equivalent_decimal_forms(
    tmp_path: Path,
) -> None:
    request = request_a(tmp_path)
    trailing_zero = replace(
        request,
        framing=replace(
            request.framing,
            alpha_threshold=Decimal("1.2300"),
            stretch_x=Decimal("1.2500"),
        ),
    )
    exponent_form = replace(
        request,
        framing=replace(
            request.framing,
            alpha_threshold=Decimal("123e-2"),
            stretch_x=Decimal("125e-2"),
        ),
    )

    assert render_fingerprint(trailing_zero, cut_key="ef" * 32) == render_fingerprint(
        exponent_form, cut_key="ef" * 32
    )


def test_render_fingerprint_canonicalizes_signed_zero(tmp_path: Path) -> None:
    request = request_a(tmp_path)
    negative_zero = replace(
        request,
        framing=replace(request.framing, alpha_threshold=Decimal("-0.000")),
    )
    positive_zero = replace(
        request,
        framing=replace(request.framing, alpha_threshold=Decimal("0")),
    )

    assert render_fingerprint(negative_zero, cut_key="ef" * 32) == render_fingerprint(
        positive_zero, cut_key="ef" * 32
    )


def test_render_fingerprint_is_independent_of_decimal_context_precision(
    tmp_path: Path,
) -> None:
    request = replace(
        request_a(tmp_path),
        framing=FramingSpec(
            True,
            Decimal("2.123456789012345678901234567890123456789"),
            8,
            Decimal("1.987654321098765432109876543210987654321"),
        ),
    )

    with localcontext() as context:
        context.prec = 6
        low_precision = render_fingerprint(request, cut_key="ef" * 32)
    with localcontext() as context:
        context.prec = 60
        high_precision = render_fingerprint(request, cut_key="ef" * 32)

    assert low_precision == high_precision


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
        lambda request: preview_fingerprint(
            request, Fraction(1, 5), source_fingerprint="bad"
        ),
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
            "edge_settings": {
                "alpha_matting": {
                    "background_threshold": 10,
                    "erode_size": 10,
                    "foreground_threshold": 240,
                },
                "mode": "standard",
            },
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

    assert (
        cut_cache_key(request, source_sha256=SOURCE_SHA, model_weight_sha256=MODEL_SHA)
        == hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    )


def test_preview_identity_tracks_playhead_and_matting_values(tmp_path: Path) -> None:
    request = request_a(tmp_path)
    source_identity = "cd" * 32
    baseline = preview_fingerprint(
        request, Fraction(1, 5), source_fingerprint=source_identity
    )
    changed_matting = replace(
        request,
        segmentation=replace(
            request.segmentation,
            alpha_matting=AlphaMattingSpec(230, 12, 7),
        ),
    )

    assert baseline != preview_fingerprint(
        request, Fraction(1, 4), source_fingerprint=source_identity
    )
    assert baseline != preview_fingerprint(
        changed_matting, Fraction(1, 5), source_fingerprint=source_identity
    )


def test_preview_identity_tracks_weight_runtime_and_pipeline_versions(
    tmp_path: Path,
) -> None:
    request = request_a(tmp_path)
    baseline = preview_fingerprint(
        request,
        Fraction(1, 5),
        source_fingerprint=SOURCE_SHA,
        model_weight_sha256=MODEL_SHA,
        rembg_version="2.0.72",
        pipeline_schema_version="pipeline-v1",
    )

    assert baseline == preview_fingerprint(
        request,
        Fraction(1, 5),
        source_fingerprint=SOURCE_SHA,
        model_weight_sha256=MODEL_SHA,
        rembg_version="2.0.72",
        pipeline_schema_version="pipeline-v1",
    )
    assert baseline != preview_fingerprint(
        request,
        Fraction(1, 5),
        source_fingerprint=SOURCE_SHA,
        model_weight_sha256="ac" * 32,
        rembg_version="2.0.72",
        pipeline_schema_version="pipeline-v1",
    )
    assert baseline != preview_fingerprint(
        request,
        Fraction(1, 5),
        source_fingerprint=SOURCE_SHA,
        model_weight_sha256=MODEL_SHA,
        rembg_version="2.0.73",
        pipeline_schema_version="pipeline-v1",
    )
    assert baseline != preview_fingerprint(
        request,
        Fraction(1, 5),
        source_fingerprint=SOURCE_SHA,
        model_weight_sha256=MODEL_SHA,
        rembg_version="2.0.72",
        pipeline_schema_version="pipeline-v2",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_weight_sha256", "not-a-sha"),
        ("rembg_version", ""),
        ("pipeline_schema_version", 1),
    ],
)
def test_preview_identity_rejects_malformed_prepared_identities(
    tmp_path: Path, field: str, value: object
) -> None:
    identities: dict[str, object] = {
        "model_weight_sha256": MODEL_SHA,
        "rembg_version": "2.0.72",
        "pipeline_schema_version": "pipeline-v1",
    }
    identities[field] = value

    with pytest.raises(ValidationError) as exc:
        preview_fingerprint(
            request_a(tmp_path),
            Fraction(1, 5),
            source_fingerprint=SOURCE_SHA,
            **identities,  # type: ignore[arg-type]
        )

    assert exc.value.code is ErrorCode.INVALID_RENDER_REQUEST


def test_cut_key_is_built_from_the_public_frozen_inputs(tmp_path: Path) -> None:
    request = request_a(tmp_path)
    inputs = cut_cache_key_inputs(
        request, source_sha256=SOURCE_SHA, model_weight_sha256=MODEL_SHA
    )

    assert cut_cache_key(
        request, source_sha256=SOURCE_SHA, model_weight_sha256=MODEL_SHA
    ) == fingerprints_module.cut_cache_key_from_inputs(inputs)
    with pytest.raises(TypeError):
        inputs["source_sha256"] = "ff" * 32  # type: ignore[index]


def test_complete_hash_checks_cancellation_between_chunks(tmp_path: Path) -> None:
    source = tmp_path / "cancelled.mp4"
    source.write_bytes(b"0123456789")
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(AppError) as exc:
        complete_source_sha256(source, chunk_size=2, is_cancelled=cancelled)

    assert exc.value.code is ErrorCode.JOB_CANCELLED
    assert checks == 3
