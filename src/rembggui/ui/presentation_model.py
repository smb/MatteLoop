"""Immutable view model for the main window shell."""

from __future__ import annotations

from dataclasses import dataclass

from rembggui.core.state import FocusTarget
from rembggui.ui.crop_presentation import CropPresentation
from rembggui.ui.parameter_presentation import ParameterPresentation
from rembggui.ui.timeline_presentation import TimelinePresentation


@dataclass(frozen=True)
class PresentationModel:
    source_mode: str
    result_message: str
    result_marker: str | None
    show_stage: bool
    show_timeline: bool
    show_success: bool
    show_rebuild: bool
    editor_locked: bool
    choose_enabled: bool
    replace_enabled: bool
    preview_enabled: bool
    preview_label: str
    render_enabled: bool
    rebuild_enabled: bool
    open_output_enabled: bool
    open_folder_enabled: bool
    primary_action: str | None
    focus_target: FocusTarget
    source_error_message: str | None
    source_error_detail: str | None
    stale_category: str | None
    source_surface_visible: bool
    source_strip_visible: bool
    source_error_visible: bool
    source_surface_heading: str
    result_status: str
    result_checkerboard: bool
    result_accessible_name: str
    result_accessible_description: str
    result_status_marker: str | None
    recovery_visible: bool
    recovery_label: str
    artifact_path: str | None
    success_label: str
    success_accessible_description: str
    workspace_attention: bool
    workspace_open: bool
    source_filename: str
    source_dimensions: str
    source_duration: str
    source_frame_rate: str
    source_file_size: str
    source_path: str | None
    source_frame: object | None
    crop: CropPresentation | None
    crop_enabled: bool
    parameters: ParameterPresentation
    timeline: TimelinePresentation | None
    result_frame: object | None
    source_loading: bool
    inspector_enabled: bool
