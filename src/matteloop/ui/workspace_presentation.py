"""Qt-free presentation and request assembly for durable cut sets."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from fractions import Fraction
from pathlib import Path

from matteloop.core.specs import (
    AlphaMattingSpec,
    CropSpec,
    EdgeMode,
    RenderRequest,
    SamplingSpec,
    SegmentationSpec,
)
from matteloop.core.timeline import format_timecode
from matteloop.jobs.models.catalog import ModelCatalog
from matteloop.jobs.workspace import CutManifest, WorkspaceSummary
from matteloop.ui.aligned_rows import AlignedColumn, AlignedRow, RowStatus
from matteloop.ui.source_presentation import (
    format_source_dimensions,
    format_source_file_size,
    format_source_filename,
)


def present_workspace(summary: WorkspaceSummary, catalog: ModelCatalog) -> AlignedRow:
    """Map one validated manifest to aligned columns and spoken detail."""
    manifest = summary.manifest
    sampling = _mapping(manifest.cache_key_inputs["sampling"])
    start = _fraction(sampling["start"])
    end = _fraction(sampling["end"])
    fps = _integer(sampling["fps"])
    model_id = manifest.model_id
    model = catalog.get(model_id)
    flags = ", ".join(
        label
        for label, enabled in (("edited", manifest.edited), ("pinned", manifest.pinned))
        if enabled
    ) or "unchanged"
    status_words = f"{flags}; {format_source_file_size(summary.size_bytes)}"
    detail = (
        f"{format_source_filename(manifest.source_path)}; "
        f"{manifest.frame_count} frames; {model.display_name}; "
        f"{format_timecode(start)}–{format_timecode(end)} at {fps} fps; "
        f"{format_source_dimensions(manifest.width, manifest.height)}; "
        f"created {_created_at(manifest.created_at_ns)}; "
        f"purpose: {model.purpose}; licence: {model.license_note}; "
        f"{status_words}"
    )
    glyph = "✎" if manifest.edited else ("◆" if manifest.pinned else "✓")
    status_token = (
        RowStatus.EDITED
        if manifest.edited
        else RowStatus.PINNED
        if manifest.pinned
        else RowStatus.READY
    )
    return AlignedRow(
        glyph,
        status_token,
        (
            AlignedColumn(format_source_filename(manifest.source_path)),
            AlignedColumn(f"{manifest.frame_count} frames", True),
            AlignedColumn(model.display_name),
            AlignedColumn(
                f"{format_timecode(start)}–{format_timecode(end)} · {fps} fps"
            ),
            AlignedColumn(
                format_source_dimensions(manifest.width, manifest.height)
            ),
            AlignedColumn(_created_at(manifest.created_at_ns)),
            AlignedColumn(flags),
        ),
        detail,
    )


def request_for_workspace(
    manifest: CutManifest, base: RenderRequest
) -> RenderRequest:
    """Use the manifest's exact cut inputs with the current output/framing."""
    inputs = manifest.cache_key_inputs
    sampling = _mapping(inputs["sampling"])
    crop = _mapping(inputs["crop"])
    model = _mapping(inputs["model"])
    edge_settings = _mapping(inputs["edge_settings"])
    matting = _mapping(edge_settings["alpha_matting"])
    mode = {
        "standard": EdgeMode.STANDARD,
        "decontaminate": EdgeMode.DECONTAMINATE_COLORS,
    }[_string(edge_settings["mode"])]
    return RenderRequest(
        source=Path(manifest.source_path),
        sampling=SamplingSpec(
            _fraction(sampling["start"]),
            _fraction(sampling["end"]),
            _integer(sampling["fps"]),
        ),
        crop=CropSpec(
            _integer(crop["x"]),
            _integer(crop["y"]),
            _integer(crop["width"]),
            _integer(crop["height"]),
        ),
        segmentation=SegmentationSpec(
            _string(model["id"]),
            mode,
            AlphaMattingSpec(
                _integer(matting["foreground_threshold"]),
                _integer(matting["background_threshold"]),
                _integer(matting["erode_size"]),
            ),
            base.segmentation.execution_provider,
        ),
        framing=base.framing,
        output=base.output,
        rebuild=True,
        transform=base.transform,
    )


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("manifest cache inputs are not a mapping")
    return value


def _fraction(value: object) -> Fraction:
    payload = _mapping(value)
    return Fraction(_integer(payload["numerator"]), _integer(payload["denominator"]))


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("manifest cache input is not an integer")
    return value


def _string(value: object) -> str:
    if type(value) is not str:
        raise ValueError("manifest cache input is not a string")
    return value


def _created_at(timestamp_ns: int) -> str:
    try:
        return datetime.fromtimestamp(timestamp_ns / 1_000_000_000).strftime(
            "%Y-%m-%d %H:%M"
        )
    except (OverflowError, OSError, ValueError):
        return "unknown time"
