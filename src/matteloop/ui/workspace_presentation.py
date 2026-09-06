"""Qt-free presentation and request assembly for durable cut sets."""

from __future__ import annotations

from collections.abc import Mapping
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QDateTime, QLocale, QObject

from matteloop.core.specs import (
    AlphaMattingSpec,
    CropSpec,
    EdgeMode,
    RenderRequest,
    SamplingSpec,
    SegmentationSpec,
)
from matteloop.jobs.models.catalog import ModelCatalog
from matteloop.jobs.workspace import CutManifest, WorkspaceSummary
from matteloop.ui.aligned_rows import AlignedColumn, AlignedRow, RowStatus
from matteloop.ui.copy import model_display_name, model_license, model_purpose
from matteloop.ui.i18n import display_locale
from matteloop.ui.source_presentation import (
    format_localized_timecode,
    format_source_dimensions,
    format_source_file_size,
    format_source_filename,
)


class WorkspacePresentation(QObject):
    """Translation context used by the Qt-free workspace presentation helpers."""

    def format_frames(self, count: int) -> str:
        return self.tr("%n frames", "", count)


_WORKSPACE_TRANSLATOR = WorkspacePresentation()


def present_workspace(summary: WorkspaceSummary, catalog: ModelCatalog) -> AlignedRow:
    """Map one validated manifest to aligned columns and spoken detail."""
    values = _workspace_values(summary, catalog)
    flags = values["flags"]
    status_token = (
        RowStatus.EDITED
        if summary.manifest.edited
        else RowStatus.PINNED
        if summary.manifest.pinned
        else RowStatus.READY
    )
    return AlignedRow(
        "✎" if summary.manifest.edited else ("◆" if summary.manifest.pinned else "✓"),
        status_token,
        (
            AlignedColumn(values["filename"]),
            AlignedColumn(values["frame_text"], True),
            AlignedColumn(values["display_name"]),
            AlignedColumn(values["range_text"]),
            AlignedColumn(values["dimensions"]),
            AlignedColumn(values["created_at"]),
            AlignedColumn(flags),
        ),
        _workspace_detail(values),
    )


def _workspace_values(
    summary: WorkspaceSummary, catalog: ModelCatalog
) -> dict[str, str]:
    manifest = summary.manifest
    sampling = _mapping(manifest.cache_key_inputs["sampling"])
    start = _fraction(sampling["start"])
    end = _fraction(sampling["end"])
    fps = _integer(sampling["fps"])
    model_id = manifest.model_id
    model = catalog.get(model_id)
    flags = ", ".join(
        label
        for label, enabled in (
            (
                QCoreApplication.translate("WorkspacePresentation", "edited"),
                manifest.edited,
            ),
            (
                QCoreApplication.translate("WorkspacePresentation", "pinned"),
                manifest.pinned,
            ),
        )
        if enabled
    ) or QCoreApplication.translate("WorkspacePresentation", "unchanged")
    display_name = model_display_name(model_id, model.display_name)
    purpose = model_purpose(model_id, model.purpose)
    license_note = model_license(model_id, model.license_note)
    frame_count = manifest.frame_count
    frame_text = _WORKSPACE_TRANSLATOR.format_frames(frame_count)
    fps_text = QCoreApplication.translate("WorkspacePresentation", "%s fps") % fps
    return {
        "filename": format_source_filename(manifest.source_path),
        "frame_text": frame_text,
        "display_name": display_name,
        "range_text": QCoreApplication.translate("WorkspacePresentation", "%1–%2 · %3")
        .replace("%1", format_localized_timecode(start))
        .replace("%2", format_localized_timecode(end))
        .replace("%3", fps_text),
        "dimensions": format_source_dimensions(manifest.width, manifest.height),
        "created_at": _created_at(manifest.created_at_ns),
        "purpose": purpose,
        "license_note": license_note,
        "flags": flags,
        "size": format_source_file_size(summary.size_bytes),
        "range": f"{format_localized_timecode(start)}–{format_localized_timecode(end)}",
        "fps": fps_text,
    }


def _workspace_detail(values: dict[str, str]) -> str:
    status_words = (
        QCoreApplication.translate("WorkspacePresentation", "%1; %2")
        .replace("%1", values["flags"])
        .replace("%2", values["size"])
    )
    return (
        QCoreApplication.translate(
            "WorkspacePresentation",
            "%1; %2; %3; %4 at %5; %6; created %7; purpose: %8; licence: %9; %10",
        )
        .replace("%10", status_words)
        .replace("%1", values["filename"])
        .replace("%2", values["frame_text"])
        .replace("%3", values["display_name"])
        .replace("%4", values["range"])
        .replace("%5", values["fps"])
        .replace("%6", values["dimensions"])
        .replace("%7", values["created_at"])
        .replace("%8", values["purpose"])
        .replace("%9", values["license_note"])
    )


def request_for_workspace(manifest: CutManifest, base: RenderRequest) -> RenderRequest:
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
        timestamp = QDateTime.fromMSecsSinceEpoch(timestamp_ns // 1_000_000)
        return display_locale().toString(
            timestamp, QLocale.FormatType.ShortFormat
        )
    except (OverflowError, OSError, ValueError):
        return QCoreApplication.translate("WorkspacePresentation", "unknown time")
