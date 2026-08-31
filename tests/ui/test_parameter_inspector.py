from __future__ import annotations

from PySide6.QtCore import QSettings, Qt

from rembggui.core.execution_providers import (
    COREML_EXECUTION_PROVIDER,
    CPU_EXECUTION_PROVIDER,
    ProviderOption,
)
from rembggui.ui.aligned_rows import (
    ACCESSIBLE_DESCRIPTION_ROLE,
    ROW_DATA_ROLE,
    STATUS_ROLE,
)
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
    assert inspector.model_picker.itemText(portrait_index) == "BiRefNet Portrait"
    assert inspector.model_picker.itemData(portrait_index, STATUS_ROLE) == "cached"
    assert "cached locally" in inspector.model_picker.itemData(
        portrait_index, ACCESSIBLE_DESCRIPTION_ROLE
    )
    assert not inspector.model_picker.itemIcon(portrait_index).isNull()
    assert inspector.edge_picker.count() == 2


def test_inspector_disclosure_titles_preserve_literal_ampersands(qtbot) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)

    assert inspector.disclosures["time_sampling"][0].text() == "Time && Sampling"
    assert inspector.disclosures["time_sampling"][0].accessibleName() == (
        "Time & Sampling"
    )
    assert inspector.disclosures["crop_cleanup"][0].text() == "Crop && Cleanup"
    assert inspector.disclosures["crop_cleanup"][0].accessibleName() == (
        "Crop & Cleanup"
    )


def test_output_directory_middle_elides_while_preserving_full_path_semantics(
    qtbot,
) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)
    path = "/Users/sb/" + "very-long-directory-name/" * 8 + "exports"

    inspector.output_directory_edit.resize(140, 32)
    inspector.output_directory_edit.setText(path)

    assert inspector.output_directory_edit.text() != path
    assert "…" in inspector.output_directory_edit.text()
    assert inspector.output_directory_edit.toolTip() == path
    assert inspector.output_directory_edit.accessibleDescription() == path


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
        row = inspector.model_picker.itemData(index, ROW_DATA_ROLE)
        assert row.columns[1].text == expected_sizes[model_id]
        expected_status = (
            "cached locally" if model_id == "u2netp" else "not cached yet"
        )
        detail = inspector.model_picker.itemData(
            index, Qt.ItemDataRole.AccessibleTextRole
        )
        assert expected_status in detail


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


def test_inspector_exposes_and_emits_the_selected_execution_provider(qtbot) -> None:
    inspector = Inspector(
        _settings(),
        provider_options=(
            ProviderOption(CPU_EXECUTION_PROVIDER, "CPU – empfohlen", True),
            ProviderOption(
                COREML_EXECUTION_PROVIDER,
                "Apple CoreML – experimentell",
            ),
        ),
    )
    qtbot.addWidget(inspector)
    commands: list[object] = []
    inspector.command_requested.connect(commands.append)

    assert inspector.provider_picker.currentData() == CPU_EXECUTION_PROVIDER
    assert inspector.provider_picker.itemText(1) == "Apple CoreML – experimentell"
    inspector.provider_picker.setCurrentIndex(1)

    assert [type(command).__name__ for command in commands] == [
        "ExecutionProviderChanged"
    ]
    assert commands[0].execution_provider == COREML_EXECUTION_PROVIDER


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
