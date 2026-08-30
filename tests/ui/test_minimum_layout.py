from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSettings

from rembggui.core.state import AppState, SourceLoaded, SourceLoadRequested, reduce
from rembggui.ui.main_window import MainWindow


class Store:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self._listeners: list[Callable[[AppState], None]] = []

    def dispatch(self, event: object) -> None:
        del event

    def subscribe(self, listener: Callable[[AppState], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)


class Services:
    def dispatch(self, command: object) -> None:
        del command


def test_minimum_layout_keeps_side_by_side_stage_timeline_and_shelf(qtbot) -> None:
    state = reduce(
        reduce(AppState(), SourceLoadRequested("source", "load")),
        SourceLoaded("source", "load", "metadata"),
    )
    settings = QSettings(
        QSettings.IniFormat, QSettings.UserScope, "rembggui-test", "layout"
    )
    settings.clear()
    window = MainWindow(Store(state), Services(), settings)
    qtbot.addWidget(window)
    window.resize(1100, 720)
    window.show()
    qtbot.waitUntil(lambda: window.width() == 1100)
    assert window.minimumWidth() == 1100
    assert window.minimumHeight() == 720
    assert window.inspector.width() == 340
    assert window.timeline_placeholder.height() >= 176
    assert (
        window.original_canvas.geometry().right()
        < window.result_canvas.geometry().left()
    )
    assert window.action_shelf.height() == 104
    assert (
        window.inspector_scroll.horizontalScrollBarPolicy().name == "ScrollBarAlwaysOff"
    )
    window.resize(1160, 720)
    assert window.inspector.width() == 400


def test_empty_has_only_restrained_source_surface(qtbot) -> None:
    settings = QSettings(
        QSettings.IniFormat, QSettings.UserScope, "rembggui-test", "empty"
    )
    settings.clear()
    window = MainWindow(Store(AppState()), Services(), settings)
    qtbot.addWidget(window)
    window.show()
    assert window.source_drop_surface.isVisible()
    assert not window.preview_stage.isVisible()


def test_long_source_filename_is_middle_elided_with_full_path_accessibility_description(
    qtbot,
) -> None:
    class Metadata:
        path = (
            "/very/long/path/" + "segment/" * 30 + "source-video-" + "x" * 50 + ".mov"
        )
        width = 1920
        height = 1080

    state = reduce(
        reduce(AppState(), SourceLoadRequested("source", "load")),
        SourceLoaded("source", "load", Metadata()),
    )
    settings = QSettings(
        QSettings.IniFormat, QSettings.UserScope, "rembggui-test", "path"
    )
    settings.clear()
    window = MainWindow(Store(state), Services(), settings)
    qtbot.addWidget(window)
    window.show()
    assert window.source_filename.toolTip() == Metadata.path
    assert window.source_filename.accessibleDescription() == Metadata.path
    assert window.source_filename.text().endswith(".mov")
    assert "…" in window.source_filename.text()
    assert "/" not in window.source_filename.text()
