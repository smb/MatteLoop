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


def test_inspector_exposes_the_thirteen_enabled_models_with_default_selected(
    qtbot,
) -> None:
    inspector = Inspector(
        _settings(), model_options=(("birefnet-portrait", True),)
    )
    qtbot.addWidget(inspector)

    assert inspector.model_picker.count() == 13
    assert [inspector.model_picker.itemData(i) for i in range(13)] == [
        "u2net",
        "u2netp",
        "u2net_human_seg",
        "silueta",
        "isnet-general-use",
        "isnet-anime",
        "birefnet-general",
        "birefnet-general-lite",
        "birefnet-portrait",
        "birefnet-dis",
        "birefnet-hrsod",
        "birefnet-cod",
        "birefnet-massive",
    ]
    assert inspector.model_picker.currentData() == "birefnet-portrait"
    portrait_index = inspector.model_picker.findData("birefnet-portrait")
    assert inspector.model_picker.itemText(portrait_index).endswith("cached locally")
    assert inspector.edge_picker.count() == 2


def test_inspector_shows_manifest_download_size_and_cache_status_for_each_model(
    qtbot,
) -> None:
    inspector = Inspector(
        _settings(), model_options=(("u2netp", True),)
    )
    qtbot.addWidget(inspector)

    expected_sizes = {
        "birefnet-portrait": "927.6 MiB",
        "u2net": "167.8 MiB",
        "u2netp": "4.4 MiB",
        "u2net_human_seg": "167.8 MiB",
        "silueta": "42.1 MiB",
        "isnet-general-use": "170.4 MiB",
        "isnet-anime": "167.9 MiB",
        "birefnet-general": "927.6 MiB",
        "birefnet-general-lite": "213.6 MiB",
        "birefnet-dis": "927.6 MiB",
        "birefnet-hrsod": "927.6 MiB",
        "birefnet-cod": "927.6 MiB",
        "birefnet-massive": "927.6 MiB",
    }

    for index in range(inspector.model_picker.count()):
        model_id = inspector.model_picker.itemData(index)
        text = inspector.model_picker.itemText(index)
        assert expected_sizes[model_id] in text
        expected_status = (
            "cached locally" if model_id == "u2netp" else "not cached locally"
        )
        assert text.endswith(expected_status)


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
