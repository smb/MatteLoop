from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QFileDialog

from matteloop.core.parameters import ParameterState
from matteloop.core.state import (
    AppState,
    RenderRequested,
    SourceLoaded,
    SourceLoadRequested,
    reduce,
)
from matteloop.ui.controller import SourceController
from matteloop.ui.inspector import Inspector
from matteloop.ui.main_window import MainWindow
from matteloop.ui.parameter_presentation import present_parameters
from matteloop.ui.settings_dialog import SettingsDialog
from matteloop.ui.store import ReducerStore


@dataclass(frozen=True)
class Metadata:
    path: Path
    width: int = 640
    height: int = 360
    duration: Fraction = Fraction(4)


class Services:
    def __init__(self) -> None:
        self.commands: list[object] = []

    def dispatch(self, command: object) -> None:
        self.commands.append(command)


def _settings(name: str) -> QSettings:
    settings = QSettings(
        QSettings.IniFormat,
        QSettings.UserScope,
        "matteloop-test",
        name,
    )
    settings.clear()
    return settings


def _ready_store(
    source: Path, output_directory: Path | None = None
) -> ReducerStore:
    state = AppState(
        parameters=ParameterState(output_directory=output_directory)
    )
    loading = reduce(state, SourceLoadRequested("source", "load"))
    ready = reduce(loading, SourceLoaded("source", "load", Metadata(source)))
    return ReducerStore(ready)


def test_preferences_button_opens_dialog_and_keeps_shortcut(qtbot) -> None:
    store = ReducerStore()
    window = MainWindow(store, Services(), _settings("button"))
    qtbot.addWidget(window)
    window.show()

    button = window.action_shelf.preferences_button
    assert button.objectName() == "preferences_action"
    assert button.accessibleName() == "Preferences"
    assert button.toolTip() == "Preferences"
    assert button.width() < window.render_button.width()
    assert button.isEnabled()
    assert not window.render_button.isEnabled()
    assert button.shortcut() == QKeySequence(QKeySequence.StandardKey.Preferences)

    button.click()

    qtbot.waitUntil(window.action_shelf.preferences_dialog.isVisible)
    assert window.action_shelf.preferences_dialog.windowTitle() == "Preferences"


def test_preferences_directory_matches_inspector_and_can_fall_back_to_source(
    monkeypatch: pytest.MonkeyPatch, qtbot, tmp_path: Path
) -> None:
    source = tmp_path / "source" / "clip.mp4"
    source.parent.mkdir()
    chosen = tmp_path / "exports"
    chosen.mkdir()
    settings = _settings("shared-directory")
    store = _ready_store(source)
    controller = SourceController(store, settings=settings)
    preferences = SettingsDialog(store, controller)
    inspector = Inspector(settings)
    qtbot.addWidget(preferences)
    qtbot.addWidget(inspector)
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *_args: str(chosen)
    )

    preferences.choose_output_directory_button.click()

    assert store.state.parameters.output_directory == chosen
    inspector.apply_parameters(present_parameters(store.state), editable=True)
    assert inspector.output_directory_edit.toolTip() == str(chosen)
    assert settings.value("parameters/output_directory") == str(chosen)

    preferences.clear_output_directory_button.click()

    assert store.state.parameters.output_directory is None
    inspector.apply_parameters(present_parameters(store.state), editable=True)
    assert inspector.output_directory_edit.toolTip() == str(source.parent)
    assert settings.value("parameters/output_directory") is None
    controller.shutdown()


def test_preferences_clear_works_without_a_loaded_source(qtbot, tmp_path: Path) -> None:
    override = tmp_path / "exports"
    settings = _settings("clear-without-source")
    settings.setValue("parameters/output_directory", str(override))
    store = ReducerStore(
        AppState(parameters=ParameterState(output_directory=override))
    )
    controller = SourceController(store, settings=settings)
    dialog = SettingsDialog(store, controller)
    qtbot.addWidget(dialog)

    dialog.clear_output_directory_button.click()

    assert store.state.parameters.output_directory is None
    assert settings.value("parameters/output_directory") is None
    controller.shutdown()


def test_preferences_disable_output_directory_controls_while_a_job_runs(
    qtbot, tmp_path: Path
) -> None:
    source = tmp_path / "source" / "clip.mp4"
    source.parent.mkdir()
    store = _ready_store(source, tmp_path / "exports")
    store.dispatch(RenderRequested("job", "request"))
    dialog = SettingsDialog(store, Services())
    qtbot.addWidget(dialog)

    dialog.load()

    assert not dialog.output_directory_edit.isEnabled()
    assert not dialog.choose_output_directory_button.isEnabled()
    assert not dialog.clear_output_directory_button.isEnabled()
    assert (
        dialog.description_label.text()
        == "Output directory controls are disabled while a job is running."
    )
