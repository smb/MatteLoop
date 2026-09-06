from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest
from PySide6.QtCore import QSettings, Qt

from matteloop.core.state import (
    AppState,
    ArtifactResult,
    EditedCutsChanged,
    EditedCutsScanRequested,
    ModelAvailabilityChanged,
    PreviewFailed,
    PreviewInvalidated,
    PreviewInvalidationReason,
    PreviewRequested,
    PreviewResult,
    PreviewSucceeded,
    RenderPreflightRequested,
    RenderRequested,
    RenderSucceeded,
    SourceLoaded,
    SourceLoadFailed,
    SourceLoadRequested,
    reduce,
)
from matteloop.ui.main_window import MainWindow
from matteloop.ui.ports import (
    ChooseVideoRequested,
    OpenOutputFolderRequested,
    OpenOutputRequested,
    PreviewFrameRequested,
    RebuildEditedCutsRequested,
    RenderVideoRequested,
)


class Store:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self.events: list[object] = []
        self.listeners: list[Callable[[AppState], None]] = []

    def dispatch(self, event: object) -> None:
        self.events.append(event)

    def subscribe(self, listener: Callable[[AppState], None]) -> Callable[[], None]:
        self.listeners.append(listener)

        def unsubscribe() -> None:
            self.listeners.remove(listener)

        return unsubscribe


@dataclass
class Services:
    commands: list[object]

    def dispatch(self, command: object) -> None:
        self.commands.append(command)


def _ready() -> AppState:
    loading = reduce(AppState(), SourceLoadRequested("source", "load"))
    return reduce(loading, SourceLoaded("source", "load", "metadata"))


def _current() -> AppState:
    running = reduce(_ready(), PreviewRequested("preview", "request"))
    return reduce(
        running,
        PreviewSucceeded("preview", PreviewResult("source", "request", "cutout")),
    )


def _rendered(state: AppState | None = None) -> AppState:
    running = reduce(state or _ready(), RenderRequested("render", "render-request"))
    return reduce(
        running,
        RenderSucceeded(
            "render", ArtifactResult("source", "render-request", "/tmp/cutout.webp")
        ),
    )


def _edited() -> AppState:
    rendered = _rendered(_current())
    scanning = reduce(
        rendered, EditedCutsScanRequested("source", "render-request", "scan")
    )
    return reduce(scanning, EditedCutsChanged("source", "render-request", "scan", True))


@pytest.fixture
def window(qtbot):
    store = Store(AppState())
    services = Services([])
    settings = QSettings(
        QSettings.IniFormat, QSettings.UserScope, "matteloop-test", "ui"
    )
    settings.clear()
    value = MainWindow(store, services, settings)
    qtbot.addWidget(value)
    value.show()
    return value, store, services


@pytest.mark.parametrize(
    ("state", "primary", "focus"),
    [
        (AppState(), None, "choose_video"),
        (_ready(), "preview", "preview_action"),
        (_current(), "render", "result_canvas"),
        (
            reduce(
                _current(),
                PreviewInvalidated(PreviewInvalidationReason.CROP_CLEANUP),
            ),
            "preview",
            "preview_action",
        ),
        (_rendered(), "preview", "job_dialog"),
        (
            reduce(_ready(), ModelAvailabilityChanged(False)),
            "preview",
            "preview_action",
        ),
        (reduce(_ready(), RenderPreflightRequested()), "preview", "preflight_dialog"),
        (_edited(), "preview", "rebuild_action"),
    ],
)
def test_state_matrix_drives_primary_action_and_focus(window, state, primary, focus):
    value, _, _ = window
    value.render_state(state)
    assert value.primary_action_name() == primary
    assert value.requested_focus_name() == focus


def test_failed_repreview_keeps_old_result_stale_and_retries_preview(window) -> None:
    value, _, _ = window
    state = reduce(_current(), PreviewRequested("retry", "retry-request"))
    state = reduce(state, PreviewFailed("retry", "source", "retry-request", "failed"))
    value.render_state(state)
    assert value.primary_action_name() == "preview"
    assert value.result_canvas.text() == "Preview failed — preview again"


def test_widget_actions_translate_to_frozen_commands(window, qtbot) -> None:
    value, _, services = window
    qtbot.mouseClick(value.choose_video_button, Qt.LeftButton)
    value.render_state(_edited())
    for button in (
        value.preview_button,
        value.render_button,
        value.rebuild_button,
        value.open_output_button,
        value.open_folder_button,
    ):
        qtbot.mouseClick(button, Qt.LeftButton)
    assert [type(command) for command in services.commands] == [
        ChooseVideoRequested,
        PreviewFrameRequested,
        RenderVideoRequested,
        RebuildEditedCutsRequested,
        OpenOutputRequested,
        OpenOutputFolderRequested,
    ]


def test_store_subscription_updates_and_unsubscribes_once(window) -> None:
    value, store, _ = window
    assert len(store.listeners) == 1
    value.close()
    assert store.listeners == []


def test_error_copy_and_editor_lock(window) -> None:
    value, _, _ = window
    loading = reduce(AppState(), SourceLoadRequested("source", "load"))
    state = reduce(loading, SourceLoadFailed("source", "load", "Unsupported codec"))
    value.render_state(state)
    assert value.source_error_heading.text() == "Couldn’t read this video"
    assert not value.preview_button.isEnabled()
    assert value.choose_video_button.isEnabled()


def test_unavailable_model_truthfully_changes_preview_action_copy(window) -> None:
    value, _, _ = window
    value.render_state(reduce(_ready(), ModelAvailabilityChanged(False)))
    assert value.preview_button.text() == "Prepare & Preview"
