from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from PySide6.QtCore import QByteArray, QMimeData, QSettings, Qt, QUrl
from PySide6.QtGui import QDropEvent, QFontMetrics
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.state import (
    AppState,
    ArtifactResult,
    CancelRequested,
    EditedCutsChanged,
    EditedCutsScanRequested,
    ModelAvailabilityChanged,
    PreviewFailed,
    PreviewInvalidated,
    PreviewRequested,
    PreviewResult,
    PreviewSucceeded,
    RebuildRequested,
    RenderPreflightRequested,
    RenderRequested,
    RenderSucceeded,
    SourceLoaded,
    SourceLoadFailed,
    SourceLoadRequested,
    reduce,
)
from rembggui.ui.main_window import MainWindow
from rembggui.ui.ports import ChooseVideoRequested
from rembggui.ui.presenter import present
from rembggui.ui.source_strip import SourceDropSurface


class Store:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self.listeners: list[Callable[[AppState], None]] = []

    def dispatch(self, event: object) -> None:
        del event

    def subscribe(self, listener: Callable[[AppState], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)


@dataclass
class Services:
    commands: list[object]

    def dispatch(self, command: object) -> None:
        self.commands.append(command)


def _ready() -> AppState:
    return reduce(
        reduce(AppState(), SourceLoadRequested("source", "load")),
        SourceLoaded("source", "load", "metadata"),
    )


def _current() -> AppState:
    running = reduce(_ready(), PreviewRequested("preview", "preview-request"))
    return reduce(
        running,
        PreviewSucceeded(
            "preview", PreviewResult("source", "preview-request", "result")
        ),
    )


def _rendered() -> AppState:
    running = reduce(_current(), RenderRequested("render", "render-request"))
    return reduce(
        running,
        RenderSucceeded(
            "render", ArtifactResult("source", "render-request", "/tmp/result.webp")
        ),
    )


def _rendered_without_preview() -> AppState:
    running = reduce(_ready(), RenderRequested("render", "render-request"))
    return reduce(
        running,
        RenderSucceeded(
            "render", ArtifactResult("source", "render-request", "/tmp/result.webp")
        ),
    )


def _failed_repreview_unavailable() -> AppState:
    retrying = reduce(_current(), PreviewRequested("preview", "retry-request"))
    failed = reduce(
        retrying,
        PreviewFailed("preview", "source", "retry-request", "preview failed"),
    )
    return reduce(failed, ModelAvailabilityChanged(False))


def _edited_without_preview() -> AppState:
    rendered = _rendered_without_preview()
    scanning = reduce(
        rendered, EditedCutsScanRequested("source", "render-request", "scan")
    )
    return reduce(
        scanning,
        EditedCutsChanged("source", "render-request", "scan", True),
    )


def _edited_error() -> AppState:
    rendered = _rendered()
    scanning = reduce(
        rendered, EditedCutsScanRequested("source", "render-request", "scan")
    )
    return reduce(
        scanning,
        EditedCutsChanged(
            "source", "render-request", "scan", False, "cut frame 4 changed"
        ),
    )


def _edited() -> AppState:
    rendered = _rendered()
    scanning = reduce(
        rendered, EditedCutsScanRequested("source", "render-request", "scan")
    )
    return reduce(
        scanning,
        EditedCutsChanged("source", "render-request", "scan", True),
    )


def _state_rows() -> list[tuple[str, AppState, str | None, str, bool, bool]]:
    loading = reduce(AppState(), SourceLoadRequested("source", "load"))
    source_error = reduce(loading, SourceLoadFailed("source", "load", "bad codec"))
    unavailable = reduce(_ready(), ModelAvailabilityChanged(False))
    preparing = reduce(
        unavailable,
        PreviewRequested("prepare", "prepare-request", requires_model_preparation=True),
    )
    previewing = reduce(_ready(), PreviewRequested("preview", "preview-request"))
    previewing_old = reduce(_current(), PreviewRequested("preview", "retry-request"))
    stale = reduce(_current(), PreviewInvalidated("Crop & cleanup"))
    failed_repreview = reduce(
        previewing_old,
        PreviewFailed("preview", "source", "retry-request", "failed"),
    )
    first_error = reduce(
        previewing,
        PreviewFailed("preview", "source", "preview-request", "failed"),
    )
    preflight = reduce(_ready(), RenderPreflightRequested())
    render = reduce(_ready(), RenderRequested("render", "render-request"))
    rebuild = reduce(_edited(), RebuildRequested("rebuild", "rebuild-request"))
    cancelling = reduce(render, CancelRequested("render"))
    complete_current = _rendered()
    complete_no_preview = reduce(
        reduce(_ready(), RenderRequested("render", "render-request")),
        RenderSucceeded(
            "render", ArtifactResult("source", "render-request", "/tmp/result.webp")
        ),
    )
    return [
        ("empty", AppState(), None, "none", False, True),
        ("loading", loading, None, "none", False, True),
        ("source_error", source_error, None, "error", False, True),
        ("ready", _ready(), "preview", "none", False, False),
        ("unavailable", unavailable, "preview", "none", False, False),
        ("preparing", preparing, None, "running", False, True),
        ("previewing", previewing, None, "running", False, True),
        ("previewing_old", previewing_old, None, "running", True, True),
        ("current", _current(), "render", "current", True, False),
        ("stale", stale, "preview", "stale", True, False),
        ("failed_repreview", failed_repreview, "preview", "stale", True, False),
        ("first_error", first_error, "preview", "error", False, False),
        ("preflight", preflight, "preview", "none", False, False),
        ("render", render, None, "none", False, True),
        ("rebuild", rebuild, None, "stale", True, True),
        ("cancelling", cancelling, None, "none", False, True),
        ("complete_current", complete_current, "render", "current", True, False),
        ("complete_no_preview", complete_no_preview, "preview", "none", False, False),
        ("edited_cuts", _edited(), "preview", "stale", True, False),
        ("edited_cut_error", _edited_error(), "render", "current", True, False),
    ]


@pytest.fixture
def window(qtbot):
    services = Services([])
    settings = QSettings(
        QSettings.IniFormat, QSettings.UserScope, "rembggui-review", "ui"
    )
    settings.clear()
    value = MainWindow(Store(AppState()), services, settings)
    qtbot.addWidget(value)
    value.show()
    return value, services


def test_edit_cut_error_recovery_is_visible_focusable_and_receives_focus(
    window, qtbot
) -> None:
    value, _ = window
    value.render_state(_edited_error())
    qtbot.wait(20)
    assert value.edited_cut_recovery.isVisible()
    assert value.edited_cut_recovery.focusPolicy() & Qt.FocusPolicy.StrongFocus
    assert QApplication.focusWidget() is value.edited_cut_recovery
    assert value.requested_focus_name() == "edited_cut_recovery"


def test_preview_error_wins_over_unavailable_model_copy() -> None:
    preparing = reduce(_ready(), ModelAvailabilityChanged(False))
    running = reduce(
        preparing,
        PreviewRequested("preview", "preview-request", requires_model_preparation=True),
    )
    failed = reduce(
        running,
        PreviewFailed(
            "preview",
            "source",
            "preview-request",
            AppError(
                ErrorCode.MODEL_PREPARATION_INVALID,
                "prepare",
                "model.prepare.failed",
                "checksum detail",
                "retry",
            ),
        ),
    )
    model = present(failed)
    assert model.result_message == "Preview failed — retry Preview Frame"
    assert model.preview_label == "Prepare & Preview"


def test_fixed_shelf_only_contains_reachable_primary_actions_at_minimum(window) -> None:
    value, _ = window
    value.render_state(_ready())
    value.resize(1100, 720)
    value.show()
    assert value.action_shelf.height() == 104
    assert value.rebuild_button.parentWidget() is not value.action_shelf
    assert (
        value.rebuild_button.parentWidget().parentWidget().parentWidget()
        is value.inspector_content
    )
    assert value.success_banner.parentWidget() is not value.action_shelf
    visible_buttons = [
        button
        for button in value.action_shelf.findChildren(QPushButton)
        if button.isVisible()
    ]
    assert {button.objectName() for button in visible_buttons} == {
        "preview_action",
        "render_action",
    }
    assert all(
        button.width() >= button.sizeHint().width() for button in visible_buttons
    )


@pytest.mark.parametrize(
    ("state", "status", "checkerboard", "message"),
    [
        (_ready(), "none", False, "Preview this frame to inspect the cutout"),
        (_current(), "current", True, "Current preview"),
        (
            reduce(_current(), PreviewInvalidated("Crop")),
            "stale",
            True,
            "Settings changed — preview again",
        ),
    ],
)
def test_result_exposes_truthful_status_and_checkerboard(
    window, state, status, checkerboard, message
):
    value, _ = window
    value.render_state(state)
    assert value.result_canvas.property("status") == status
    assert bool(value.result_canvas.property("checkerboard")) is checkerboard
    assert value.result_canvas.text() == message


def test_render_complete_banner_names_artifact_with_full_accessible_path(
    window,
) -> None:
    value, _ = window
    state = _rendered()
    value.render_state(state)
    assert value.success_banner.text() == "Render complete"
    assert value.success_artifact.text() in "/tmp/result.webp"
    assert value.success_artifact.toolTip() == "/tmp/result.webp"
    assert value.success_artifact.accessibleDescription() == "/tmp/result.webp"
    assert value.open_output_button.accessibleName() == "Open output"
    assert value.open_folder_button.accessibleName() == "Open folder"


def test_source_drop_accepts_one_local_video_and_dispatches_path(
    window, tmp_path, qtbot
):
    value, services = window
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"not decoded here")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(source))])
    event = QDropEvent(
        value.source_drop_target.rect().center(),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    value.source_drop_target.dropEvent(event)
    assert event.isAccepted()
    assert len(services.commands) == 1
    assert type(services.commands[0]).__name__ == "VideoDropped"
    assert services.commands[0].path == source


@pytest.mark.parametrize(
    "urls",
    [
        [QUrl("https://example.test/clip.mp4")],
        [QUrl.fromLocalFile("/tmp/a.mp4"), QUrl.fromLocalFile("/tmp/b.mp4")],
        [QUrl.fromLocalFile("/tmp/clip.txt")],
    ],
)
def test_source_drop_rejects_remote_multiple_and_unsupported_urls(qtbot, urls):
    surface = SourceDropSurface()
    qtbot.addWidget(surface)
    mime = QMimeData()
    mime.setUrls(urls)
    event = QDropEvent(
        surface.rect().center(),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    surface.dropEvent(event)
    assert not event.isAccepted()


def test_replace_dispatch_includes_replace_intent(window, qtbot) -> None:
    value, services = window
    value.render_state(_ready())
    qtbot.mouseClick(value.replace_video_button, Qt.MouseButton.LeftButton)
    assert services.commands == [ChooseVideoRequested(replace=True)]


def test_invalid_saved_geometry_falls_back_to_default(window) -> None:
    value, _ = window
    settings = value._settings
    settings.setValue("window/geometry", QByteArray(b"invalid-geometry"))
    value._restore_geometry()
    assert value.size().width() >= 1100
    assert value.size().height() >= 720


def test_accessible_names_and_focus_targets_are_keyboard_controls(window) -> None:
    value, _ = window
    assert value.source_drop_target.accessibleName() == "Video drop area"
    assert value.choose_video_button.accessibleName() == "Choose Video"
    assert value.source_filename.accessibleName() == "Source video"
    assert value.replace_video_button.accessibleName() == "Replace video"
    assert value.original_canvas.accessibleName() == "Original video frame"
    assert value.result_canvas.accessibleName() == "Background-removed result"
    assert value.timeline_placeholder.accessibleName() == "Video timeline"
    assert value.inspector.accessibleName() == "Processing settings"
    assert value.success_banner.accessibleName() == "Render complete"
    for widget in (
        value.original_canvas,
        value.result_canvas,
        value.timeline_placeholder,
        value.success_banner,
        value.edited_cut_recovery,
    ):
        assert widget.focusPolicy() & Qt.FocusPolicy.StrongFocus


@pytest.mark.parametrize(
    ("name", "state", "primary", "status", "checkerboard", "locked"),
    _state_rows(),
)
def test_every_approved_state_row_has_truthful_presentation(
    window, name, state, primary, status, checkerboard, locked
) -> None:
    del name, locked
    value, _ = window
    value.render_state(state)
    assert value.result_canvas.property("status") == status
    assert bool(value.result_canvas.property("checkerboard")) is checkerboard
    assert value.primary_action_name() == primary


def test_stale_category_is_visible_and_preview_accessible_name_is_dynamic(
    window,
) -> None:
    value, _ = window
    stale = reduce(_current(), PreviewInvalidated("Crop & cleanup"))
    value.render_state(stale)
    assert value.result_canvas.status_label.text() == (
        "Settings changed — preview again · Crop & cleanup"
    )
    assert "Crop & cleanup" in value.result_canvas.accessibleDescription()
    unavailable = reduce(_ready(), ModelAvailabilityChanged(False))
    value.render_state(unavailable)
    assert value.preview_button.accessibleName() == "Prepare & Preview"


def test_focus_target_is_actual_widget_after_queued_render(window, qtbot) -> None:
    value, _ = window
    value.render_state(_current())
    qtbot.wait(20)
    assert QApplication.focusWidget() is value.result_canvas
    value.render_state(_rendered())
    qtbot.wait(20)
    assert QApplication.focusWidget() is value.success_banner


def test_wheel_and_native_specs_include_one_canonical_font_source() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    spec = Path("packaging/pysidedeploy.spec").read_text(encoding="utf-8")
    for name in (
        "IBMPlexSans-Regular.ttf",
        "IBMPlexSans-SemiBold.ttf",
        "IBMPlexMono-Regular.ttf",
        "OFL.txt",
    ):
        assert f"resources/fonts/{name}" in pyproject
        assert f"resources/fonts/{name}" in spec


def test_failed_repreview_keeps_old_result_when_model_becomes_unavailable() -> None:
    model = present(_failed_repreview_unavailable())
    assert model.result_message == "Preview failed — preview again"
    assert model.result_status == "stale"
    assert model.result_checkerboard is True
    assert model.result_status_marker == "Preview failed — preview again"
    assert "Preview failed — preview again" in model.result_accessible_description
    assert model.preview_label == "Prepare & Preview"


def test_edited_cuts_without_preview_uses_neutral_copy() -> None:
    state = _edited_without_preview()
    model = present(state)
    assert model.result_message == "Preview this frame to inspect the cutout"
    assert model.result_accessible_description == (
        "Preview this frame to inspect the cutout"
    )


def test_stale_accessibility_and_workspace_flags_are_presenter_owned() -> None:
    stale = present(reduce(_current(), PreviewInvalidated("Crop & cleanup")))
    assert stale.result_accessible_description == (
        "Crop & cleanup: Settings changed — preview again"
    )
    assert stale.workspace_attention is False
    assert stale.workspace_open is False
    edited = present(_edited_error())
    assert edited.workspace_attention is True
    assert edited.workspace_open is True
    assert edited.success_accessible_description == "/tmp/result.webp"


def test_success_banner_fits_two_rows_at_340px_shelf_width(window, qtbot) -> None:
    value, _ = window
    value.render_state(_rendered())
    value.resize(1100, 720)
    value.show()
    qtbot.wait(30)
    assert value.success_container.width() <= 400
    assert value.success_container.minimumSizeHint().width() <= (
        value.success_container.width() + 1
    )
    assert value.success_container.height() >= (
        value.open_output_button.sizeHint().height() * 2
    )
    for button in (value.open_output_button, value.open_folder_button):
        assert button.isVisible()
        assert button.width() >= button.sizeHint().width()
        assert value.success_container.rect().contains(button.geometry())
    metrics = QFontMetrics(value.success_artifact.font())
    assert metrics.horizontalAdvance(value.success_artifact.text()) <= (
        value.success_artifact.width()
    )
    assert value.success_artifact.text() in "/tmp/result.webp"


def test_recovery_is_inside_workspace_body_and_attention_opens_it(
    window, qtbot
) -> None:
    value, _ = window
    workspace_button, workspace_body = value.inspector.disclosures["workspace"]
    assert not workspace_button.isChecked()
    assert value.edited_cut_recovery.parentWidget() is workspace_body
    value.render_state(_edited_error())
    qtbot.wait(30)
    assert workspace_button.isChecked()
    assert value.edited_cut_recovery.isVisible()
    assert QApplication.focusWidget() is value.edited_cut_recovery


def test_actual_tab_order_skips_hidden_widgets_and_reaches_success_actions(
    window, qtbot
) -> None:
    value, _ = window
    value.render_state(_edited_error())
    value.show()
    qtbot.wait(30)
    assert value.inspector_scroll.focusPolicy() == Qt.FocusPolicy.NoFocus
    expected = [
        value.replace_video_button,
        value.original_canvas,
        value.result_canvas,
        value.timeline_placeholder,
        value.inspector.disclosures["segmentation"][0],
        value.manage_models_button,
        value.inspector.disclosures["time_sampling"][0],
        value.inspector.disclosures["crop_cleanup"][0],
        value.inspector.disclosures["output"][0],
        value.inspector.disclosures["workspace"][0],
        value.edited_cut_recovery,
        value.manage_workspaces_button,
        value.success_banner,
        value.open_output_button,
        value.open_folder_button,
        value.preview_button,
        value.render_button,
    ]
    value.replace_video_button.setFocus()
    QApplication.processEvents()
    for expected_widget in expected:
        assert QApplication.focusWidget() is expected_widget
        assert expected_widget.objectName()
        assert expected_widget.accessibleName()
        QTest.keyClick(expected_widget, Qt.Key.Key_Tab)
        QApplication.processEvents()


@pytest.mark.parametrize(
    (
        "name",
        "state",
        "message",
        "primary",
        "status",
        "checkerboard",
        "source_surface",
        "recovery",
        "success",
    ),
    [
        (
            "empty",
            AppState(),
            "Preview this frame to inspect the cutout",
            None,
            "none",
            False,
            True,
            False,
            False,
        ),
        (
            "loading",
            reduce(AppState(), SourceLoadRequested("source", "load")),
            "Reading video…",
            None,
            "none",
            False,
            False,
            False,
            False,
        ),
        (
            "source_error",
            reduce(
                reduce(AppState(), SourceLoadRequested("source", "load")),
                SourceLoadFailed("source", "load", "bad codec"),
            ),
            "This video could not be read. Choose another video.",
            None,
            "error",
            False,
            True,
            False,
            False,
        ),
        (
            "ready",
            _ready(),
            "Preview this frame to inspect the cutout",
            "preview",
            "none",
            False,
            False,
            False,
            False,
        ),
        (
            "unavailable",
            reduce(_ready(), ModelAvailabilityChanged(False)),
            "Download required",
            "preview",
            "none",
            False,
            False,
            False,
            False,
        ),
        (
            "preparing",
            reduce(
                reduce(_ready(), ModelAvailabilityChanged(False)),
                PreviewRequested(
                    "prepare", "prepare-request", requires_model_preparation=True
                ),
            ),
            "Previewing selected frame",
            None,
            "running",
            False,
            False,
            False,
            False,
        ),
        (
            "previewing_old",
            reduce(_current(), PreviewRequested("preview", "retry-request")),
            "Current preview — previewing selected frame",
            None,
            "running",
            True,
            False,
            False,
            False,
        ),
        (
            "current",
            _current(),
            "Current preview",
            "render",
            "current",
            True,
            False,
            False,
            False,
        ),
        (
            "stale",
            reduce(_current(), PreviewInvalidated("Crop & cleanup")),
            "Settings changed — preview again",
            "preview",
            "stale",
            True,
            False,
            False,
            False,
        ),
        (
            "failed_repreview_unavailable",
            _failed_repreview_unavailable(),
            "Preview failed — preview again",
            "preview",
            "stale",
            True,
            False,
            False,
            False,
        ),
        (
            "first_error",
            reduce(
                reduce(_ready(), PreviewRequested("preview", "preview-request")),
                PreviewFailed("preview", "source", "preview-request", "failed"),
            ),
            "Preview failed — retry Preview Frame",
            "preview",
            "error",
            False,
            False,
            False,
            False,
        ),
        (
            "render",
            reduce(_ready(), RenderRequested("render", "render-request")),
            "Preview this frame to inspect the cutout",
            None,
            "none",
            False,
            False,
            False,
            False,
        ),
        (
            "complete_current",
            _rendered(),
            "Current preview",
            "render",
            "current",
            True,
            False,
            False,
            True,
        ),
        (
            "complete_no_preview",
            _rendered_without_preview(),
            "Preview this frame to inspect the cutout",
            "preview",
            "none",
            False,
            False,
            False,
            True,
        ),
        (
            "edited_cuts",
            _edited(),
            "Model preview — rebuild uses edited cut frames",
            "preview",
            "stale",
            True,
            False,
            False,
            True,
        ),
        (
            "edited_cut_error",
            _edited_error(),
            "Current preview",
            "render",
            "current",
            True,
            False,
            True,
            True,
        ),
    ],
)
def test_literal_reducer_valid_state_matrix(
    window,
    name,
    state,
    message,
    primary,
    status,
    checkerboard,
    source_surface,
    recovery,
    success,
) -> None:
    del name
    value, _ = window
    value.render_state(state)
    assert value.result_canvas.text() == message
    assert value.result_canvas.property("status") == status
    assert bool(value.result_canvas.property("checkerboard")) is checkerboard
    assert value.primary_action_name() == primary
    assert value.source_drop_surface.isVisible() is source_surface
    assert value.edited_cut_recovery.isVisible() is recovery
    assert value.success_container.isVisible() is success
