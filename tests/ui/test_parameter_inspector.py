from __future__ import annotations

from PySide6.QtCore import QSettings

from rembggui.ui.inspector import Inspector


def _settings() -> QSettings:
    settings = QSettings(
        QSettings.IniFormat,
        QSettings.UserScope,
        "rembggui-test",
        "parameter-inspector",
    )
    settings.clear()
    return settings


def test_inspector_exposes_only_the_four_v1_models(qtbot) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)

    assert inspector.model_picker.count() == 4
    assert [inspector.model_picker.itemData(i) for i in range(4)] == [
        "birefnet-portrait",
        "birefnet-general-lite",
        "u2net",
        "isnet-general-use",
    ]
    assert inspector.model_picker.currentData() == "birefnet-portrait"
    assert inspector.edge_picker.count() == 2


def test_inspector_emits_parameter_commands_from_standard_controls(qtbot) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)
    commands: list[object] = []
    inspector.command_requested.connect(commands.append)

    inspector.fps_spinbox.setValue(60)
    inspector.trim_checkbox.setChecked(True)
    inspector.padding_spinbox.setValue(4)

    assert [type(command).__name__ for command in commands] == [
        "OutputFpsChanged",
        "GlobalTrimChanged",
        "PaddingChanged",
    ]


def test_inspector_emits_edge_mode_from_the_standard_combo(qtbot) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)
    commands: list[object] = []
    inspector.command_requested.connect(commands.append)

    inspector.edge_picker.setCurrentIndex(1)

    assert [type(command).__name__ for command in commands] == ["EdgeModeChanged"]


def test_inspector_explains_invalid_output_filename_without_emitting_a_command(
    qtbot,
) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)
    commands: list[object] = []
    inspector.command_requested.connect(commands.append)

    inspector.output_filename_edit.setText("not-a-webp.txt")
    inspector.output_filename_edit.editingFinished.emit()

    assert commands == []
    assert inspector.output_filename_edit.property("invalid") is True
    assert "single" in inspector.output_filename_edit.toolTip()
