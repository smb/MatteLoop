"""Canonical, targeted content identities for preview, cuts, and rendering."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from decimal import Decimal
from pathlib import Path
from typing import BinaryIO, Protocol

from rembggui.core.errors import AppError, ErrorCode, ValidationError
from rembggui.core.specs import CropSpec, FramingSpec, RenderRequest, SamplingSpec

FINGERPRINT_SCHEMA = "rembggui-fingerprint"
FINGERPRINT_SCHEMA_VERSION = 1
PIPELINE_SCHEMA_VERSION = "pipeline-v1"
ORIENTATION_COLOR_VERSION = "orientation-color-v1"
REMBG_VERSION = "2.0.72"
PROVISIONAL_CHUNK_SIZE = 64 * 1024
COMPLETE_HASH_CHUNK_SIZE = 1024 * 1024


class _Digest(Protocol):
    def update(self, data: bytes, /) -> object: ...


def provisional_source_fingerprint(
    source: Path, *, chunk_size: int = PROVISIONAL_CHUNK_SIZE
) -> str:
    """Return a fast source identity suitable only for UI data and thumbnails."""
    source = _validated_source_path(source)
    chunk_size = _validated_chunk_size(chunk_size)
    try:
        source_stat = source.stat()
        if not stat.S_ISREG(source_stat.st_mode):
            raise OSError("source is not a regular file")
        with source.open("rb") as source_file:
            head = source_file.read(chunk_size)
            tail_offset = max(0, source_stat.st_size - chunk_size)
            source_file.seek(tail_offset)
            tail = source_file.read(chunk_size)
        canonical_path = str(source.resolve(strict=True))
    except OSError as error:
        raise _invalid_source("source must be a readable regular file") from error

    return _canonical_hash(
        {
            **_schema("provisional-source"),
            "canonical_path": canonical_path,
            "size": source_stat.st_size,
            "mtime_ns": source_stat.st_mtime_ns,
            "head_sha256": hashlib.sha256(head).hexdigest(),
            "tail_sha256": hashlib.sha256(tail).hexdigest(),
        }
    )


def complete_source_sha256(
    source: Path, *, chunk_size: int = COMPLETE_HASH_CHUNK_SIZE
) -> str:
    """Stream a complete source digest, rejecting any concurrent source change."""
    source = _validated_source_path(source)
    chunk_size = _validated_chunk_size(chunk_size)
    try:
        path_before = source.stat()
        if not stat.S_ISREG(path_before.st_mode):
            raise OSError("source is not a regular file")
    except OSError as error:
        raise _invalid_source("source must be a readable regular file") from error

    digest = hashlib.sha256()
    bytes_read = 0
    try:
        with source.open("rb") as source_file:
            opened_before = os.fstat(source_file.fileno())
            _require_unchanged(path_before, opened_before)
            bytes_read = _update_digest(source_file, digest, chunk_size)
            opened_after = os.fstat(source_file.fileno())
            path_after = source.stat()
    except AppError:
        raise
    except OSError as error:
        raise _source_changed("source became unavailable while hashing") from error

    identities = (opened_before, opened_after, path_after)
    if any(_stat_identity(item) != _stat_identity(path_before) for item in identities):
        raise _source_changed("source changed while hashing")
    if bytes_read != path_before.st_size:
        raise _source_changed("source size changed while hashing")
    return digest.hexdigest()


def preview_fingerprint(
    request: RenderRequest,
    *,
    source_fingerprint: str | None = None,
    orientation_color_version: str = ORIENTATION_COLOR_VERSION,
) -> str:
    """Identify only inputs consumed by preview generation."""
    request = _validated_request(request)
    if source_fingerprint is None:
        source_fingerprint = provisional_source_fingerprint(request.source)
    source_fingerprint = _validated_sha256(source_fingerprint, "source_fingerprint")
    orientation_color_version = _validated_version(
        orientation_color_version, "orientation_color_version"
    )
    return _canonical_hash(
        {
            **_schema("preview"),
            "source_fingerprint": source_fingerprint,
            "sampling": _sampling(request.sampling),
            "crop": _crop(request.crop),
            "segmentation": {
                "model_id": request.segmentation.model_id,
                "edge_mode": request.segmentation.edge_mode.value,
            },
            "framing": _framing(request.framing),
            "orientation_color_version": orientation_color_version,
        }
    )


def cut_cache_key(
    request: RenderRequest,
    *,
    source_sha256: str,
    model_weight_sha256: str,
    pipeline_schema_version: str = PIPELINE_SCHEMA_VERSION,
    orientation_color_version: str = ORIENTATION_COLOR_VERSION,
    rembg_version: str = REMBG_VERSION,
) -> str:
    """Return the authoritative persistent identity for segmented cut reuse."""
    request = _validated_request(request)
    source_sha256 = _validated_sha256(source_sha256, "source_sha256")
    model_weight_sha256 = _validated_sha256(
        model_weight_sha256, "model_weight_sha256"
    )
    pipeline_schema_version = _validated_version(
        pipeline_schema_version, "pipeline_schema_version"
    )
    orientation_color_version = _validated_version(
        orientation_color_version, "orientation_color_version"
    )
    rembg_version = _validated_version(rembg_version, "rembg_version")
    return _canonical_hash(
        {
            **_schema("cut-cache-key"),
            "source_sha256": source_sha256,
            "sampling": _sampling(request.sampling),
            "crop": _crop(request.crop),
            "model": {
                "id": request.segmentation.model_id,
                "weight_sha256": model_weight_sha256,
            },
            "rembg_version": rembg_version,
            "pipeline_schema_version": pipeline_schema_version,
            "orientation_color_version": orientation_color_version,
            "edge_settings": {"mode": request.segmentation.edge_mode.value},
        }
    )


def render_fingerprint(request: RenderRequest, *, cut_key: str) -> str:
    """Identify cut content plus the framing and size settings used by encoding."""
    request = _validated_request(request)
    cut_key = _validated_sha256(cut_key, "cut_key")
    return _canonical_hash(
        {
            **_schema("render"),
            "cut_key": cut_key,
            "framing": _framing(request.framing),
            "max_bytes": request.output.max_bytes,
        }
    )


def _update_digest(
    source_file: BinaryIO,
    digest: _Digest,
    chunk_size: int,
) -> int:
    bytes_read = 0
    while chunk := source_file.read(chunk_size):
        digest.update(chunk)
        bytes_read += len(chunk)
    return bytes_read


def _require_unchanged(before: os.stat_result, after: os.stat_result) -> None:
    if _stat_identity(before) != _stat_identity(after):
        raise _source_changed("source changed before hashing began")


def _stat_identity(source_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_size,
        source_stat.st_mtime_ns,
        source_stat.st_ctime_ns,
    )


def _schema(kind: str) -> dict[str, object]:
    return {
        "fingerprint_schema": FINGERPRINT_SCHEMA,
        "fingerprint_schema_version": FINGERPRINT_SCHEMA_VERSION,
        "kind": kind,
    }


def _sampling(sampling: SamplingSpec) -> dict[str, object]:
    return {
        "start": {
            "numerator": sampling.start.numerator,
            "denominator": sampling.start.denominator,
        },
        "end": {
            "numerator": sampling.end.numerator,
            "denominator": sampling.end.denominator,
        },
        "fps": sampling.fps,
    }


def _crop(crop: CropSpec) -> dict[str, int]:
    return {"x": crop.x, "y": crop.y, "width": crop.width, "height": crop.height}


def _framing(framing: FramingSpec) -> dict[str, object]:
    return {
        "trim": framing.trim,
        "alpha_threshold": _canonical_decimal(framing.alpha_threshold),
        "padding": framing.padding,
        "stretch_x": _canonical_decimal(framing.stretch_x),
    }


def _canonical_decimal(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    fixed_point = format(value, "f")
    if "." in fixed_point:
        fixed_point = fixed_point.rstrip("0").rstrip(".")
    return fixed_point


def _canonical_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validated_request(request: RenderRequest) -> RenderRequest:
    if not isinstance(request, RenderRequest):
        raise ValidationError(
            ErrorCode.INVALID_RENDER_REQUEST,
            "fingerprint",
            "request must be a RenderRequest",
        )
    request.validate()
    return request


def _validated_source_path(source: Path) -> Path:
    if not isinstance(source, Path):
        raise _invalid_source("source must be a Path")
    return source


def _validated_chunk_size(chunk_size: int) -> int:
    if (
        not isinstance(chunk_size, int)
        or isinstance(chunk_size, bool)
        or chunk_size < 1
    ):
        raise _invalid_source("chunk_size must be a positive integer")
    return chunk_size


def _validated_sha256(value: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in value)
    ):
        raise ValidationError(
            ErrorCode.INVALID_RENDER_REQUEST,
            "fingerprint",
            f"{field} must be a 64-character hexadecimal SHA-256",
        )
    return value.lower()


def _validated_version(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            ErrorCode.INVALID_RENDER_REQUEST,
            "fingerprint",
            f"{field} must be a non-empty string",
        )
    return value


def _invalid_source(detail: str) -> ValidationError:
    return ValidationError(ErrorCode.INVALID_RENDER_REQUEST, "source-hash", detail)


def _source_changed(detail: str) -> AppError:
    return AppError(
        ErrorCode.SOURCE_CHANGED,
        "source-hash",
        "error.source.changed",
        detail,
        "reload-source",
    )
