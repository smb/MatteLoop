"""Rendering helpers kept outside the main-window construction module."""

from __future__ import annotations

from typing import TYPE_CHECKING

from matteloop.ui.copy import main_window_copy, presented_copy, source_error_message
from matteloop.ui.crop_view import render_source_editor
from matteloop.ui.presentation_model import PresentationModel

if TYPE_CHECKING:
    from matteloop.ui.main_window import MainWindow


def render_source_error(window: MainWindow, model: PresentationModel) -> None:
    """Show the source failure with its icon, message and disclosed detail."""
    window.source_error_heading.setVisible(model.source_error_visible)
    window.source_error_copy.setVisible(model.source_error_visible)
    window.source_error_heading.setText(main_window_copy("Couldn’t read this video"))
    window.source_error_heading.set_status_icon(model.source_error_icon)
    window.source_error_heading.setToolTip(model.source_error_detail or "")
    window.source_error_heading.setAccessibleDescription(
        model.source_error_detail or ""
    )
    window.source_error_copy.setText(
        source_error_message(model.source_error_message or "")
    )


def render_window(window: MainWindow, model: PresentationModel) -> None:
    """Render the immutable presentation model into the main-window widgets."""
    _render_source_and_result(window, model)
    _render_actions(window, model)


def _render_source_and_result(window: MainWindow, model: PresentationModel) -> None:
    window.source_drop_surface.setVisible(model.source_surface_visible)
    window.source_strip.setVisible(model.source_strip_visible)
    window.preview_stage.setVisible(model.show_stage)
    window.timeline_widget.setVisible(model.show_timeline)
    window.timeline_widget.apply_presentation(model.timeline, not model.editor_locked)
    render_source_error(window, model)
    window.source_drop_surface.heading.setText(
        presented_copy(model.source_surface_heading)
    )
    window.source_strip.set_presented_metadata(model)
    render_source_editor(window.original_canvas, window.inspector, model)
    window.result_canvas.set_presented_frame(
        model.result_frame, presented_copy(model.result_message)
    )
    window.result_canvas.setAccessibleName(presented_copy(model.result_accessible_name))
    window.result_canvas.setAccessibleDescription(
        presented_copy(model.result_accessible_description)
    )
    window.result_canvas.setProperty("status", model.result_status)
    window.result_canvas.setProperty("checkerboard", model.result_checkerboard)
    status_marker = presented_copy(model.result_status_marker or "")
    window.result_canvas.set_status_marker(
        status_marker or None, model.result_status_icon
    )
    window.result_canvas.style().unpolish(window.result_canvas)
    window.result_canvas.style().polish(window.result_canvas)


def _render_actions(window: MainWindow, model: PresentationModel) -> None:
    window.choose_video_button.setEnabled(model.choose_enabled)
    window.replace_video_button.setEnabled(model.replace_enabled)
    window.preview_button.setEnabled(model.preview_enabled)
    window.preview_button.setText(presented_copy(model.preview_label))
    window.render_button.setEnabled(model.render_enabled)
    window.rebuild_button.setEnabled(model.rebuild_enabled)
    window.rebuild_button.setVisible(model.show_rebuild)
    window.edited_cut_recovery.setVisible(model.recovery_visible)
    window.edited_cut_recovery.setText(presented_copy(model.recovery_label))
    window.inspector.set_workspace_state(
        model.workspace_attention, model.workspace_open
    )
    window.open_output_button.setEnabled(model.open_output_enabled)
    window.open_folder_button.setEnabled(model.open_folder_enabled)
    window.success_container.setVisible(model.show_success)
    window.success_banner.setText(presented_copy(model.success_label))
    artifact_path = model.artifact_path or ""
    window._success_artifact_path = artifact_path
    window._update_success_artifact()
    window.success_artifact.setToolTip(artifact_path)
    window.success_artifact.setAccessibleDescription(artifact_path)
    window.success_banner.setToolTip(artifact_path)
    window.success_banner.setAccessibleDescription(
        presented_copy(model.success_accessible_description)
    )
    window.inspector.setEnabled(model.inspector_enabled)
    for name, button in (
        ("preview", window.preview_button),
        ("render", window.render_button),
    ):
        button.setProperty("primaryAction", model.primary_action == name)
        button.setProperty("primary", model.primary_action == name)
        button.style().unpolish(button)
        button.style().polish(button)
    window.preview_button.setAccessibleName(presented_copy(model.preview_label))
    window._queue_focus(model.focus_target)
