"""Offline qualification of the committed H.264 and H.265 decoder fixtures."""

from __future__ import annotations

import hashlib
from fractions import Fraction
from pathlib import Path

import pytest

from matteloop.jobs.source import decode_frame, probe_source

_FIXTURE_DIRECTORY = Path(__file__).resolve().parents[1] / "fixtures" / "codecs"


@pytest.mark.parametrize(
    ("fixture_name", "expected_hash", "expected_codec"),
    (
        (
            "h264-sdr.mp4",
            "0b96a9e0aaf2ebb470bac23a746d98d5b19f5e59f9592f6e1a9e2659eee83064",
            "h264",
        ),
        (
            "h265-sdr.mp4",
            "98261fe0d518cd1de414b7b50a6c44b677d59eb99fd7046455a24d7f9c619785",
            "hevc",
        ),
    ),
)
def test_decoder_fixture_has_verified_codec_and_decodes_first_frame(
    fixture_name: str,
    expected_hash: str,
    expected_codec: str,
) -> None:
    """Production source code accepts each committed, tagged SDR decoder sample."""
    fixture_path = _FIXTURE_DIRECTORY / fixture_name

    assert hashlib.sha256(fixture_path.read_bytes()).hexdigest() == expected_hash

    source = probe_source(fixture_path)
    assert source.validation_proof.codec_name == expected_codec
    assert source.width == 64
    assert source.height == 48
    assert source.frame_count == 2
    assert source.duration == Fraction(1)
    assert source.validation_proof.color_matrix == 1
    assert source.validation_proof.color_primaries == 1
    assert source.validation_proof.color_transfer == 13
    assert source.validation_proof.color_range == 1

    decoded = decode_frame(
        fixture_path,
        Fraction(0),
        1,
        expected_revision=source.revision,
        validation_proof=source.validation_proof,
    )
    try:
        assert decoded.image.mode == "RGBA"
        assert decoded.image.size == (64, 48)
        assert decoded.actual_pts == Fraction(0)
    finally:
        decoded.image.close()
