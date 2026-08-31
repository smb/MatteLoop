from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

from matteloop.core.state import (
    AppState,
    PreviewInvalidated,
    PreviewRequested,
    PreviewResult,
    PreviewSucceeded,
    SourceLoaded,
    SourceLoadFailed,
    SourceLoadRequested,
    reduce,
)
from matteloop.ui.main_window import MainWindow
from matteloop.ui.preview_canvas import StatusLabel


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
    loading = reduce(AppState(), SourceLoadRequested("source", "load"))
    return reduce(loading, SourceLoaded("source", "load", "metadata"))


def _current() -> AppState:
    running = reduce(_ready(), PreviewRequested("preview", "preview-request"))
    return reduce(
        running,
        PreviewSucceeded(
            "preview", PreviewResult("source", "preview-request", "result")
        ),
    )


def _source_error() -> AppState:
    loading = reduce(AppState(), SourceLoadRequested("source", "load"))
    return reduce(loading, SourceLoadFailed("source", "load", "bad video"))


@pytest.fixture
def window(qtbot):
    settings = QSettings(
        QSettings.IniFormat, QSettings.UserScope, "matteloop-icon-tests", "ui"
    )
    settings.clear()
    value = MainWindow(Store(AppState()), Services([]), settings)
    qtbot.addWidget(value)
    value.show()
    return value


@pytest.mark.parametrize(
    ("state", "label_name", "icon_name"),
    [
        (_source_error(), "source_error_heading", "error"),
        (_current(), "result_canvas", "preview"),
        (
            reduce(_current(), PreviewInvalidated("Crop & cleanup")),
            "result_canvas",
            "stale",
        ),
    ],
)
def test_presented_status_states_expose_distinct_icons(
    window, state, label_name: str, icon_name: str
) -> None:
    window.render_state(state)
    label = (
        window.source_error_heading
        if label_name == "source_error_heading"
        else window.result_canvas.status_label
    )

    assert label.status_icon_name == icon_name
    assert label.status_icon_pixmap is not None
    assert label.text()


def test_missing_status_icon_keeps_existing_text_without_raising(
    tmp_path, qtbot
) -> None:
    label = StatusLabel(runtime_root=tmp_path)
    qtbot.addWidget(label)
    label.setText("Settings changed — preview again")

    label.set_status_icon("stale")

    assert label.status_icon_name is None
    assert label.status_icon_pixmap is None
    assert label.text() == "Settings changed — preview again"


@pytest.mark.parametrize("requested_size", [1, 16, 24])
def test_status_icons_never_render_below_minimum_size(
    requested_size: int, qtbot
) -> None:
    label = StatusLabel()
    qtbot.addWidget(label)
    label.set_status_icon("preview", requested_size)

    assert label.status_icon_logical_size >= 24
    assert label.status_icon_pixmap is not None
    assert label.status_icon_pixmap.width() >= 24


def test_packaging_includes_every_status_icon_asset() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    spec = Path("packaging/pysidedeploy.spec").read_text(encoding="utf-8")

    for name in (
        "error-24.png",
        "error-32.png",
        "error-48.png",
        "error-64.png",
        "preview-24.png",
        "preview-32.png",
        "preview-48.png",
        "preview-64.png",
        "stale-24.png",
        "stale-32.png",
        "stale-48.png",
        "stale-64.png",
    ):
        assert f"resources/icons/{name}" in pyproject
        assert f"resources/icons/{name}" in spec
