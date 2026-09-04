from __future__ import annotations

import pytest
from PySide6.QtCore import QSettings, Qt

from matteloop.core.execution_providers import (
    COREML_EXECUTION_PROVIDER,
    CPU_EXECUTION_PROVIDER,
    ProviderOption,
)
from matteloop.ui.aligned_rows import (
    ACCESSIBLE_DESCRIPTION_ROLE,
    ROW_DATA_ROLE,
    STATUS_ROLE,
)
from matteloop.ui.inspector import Inspector


def _settings() -> QSettings:
    settings = QSettings(
        QSettings.IniFormat,
        QSettings.UserScope,
        "matteloop-test",
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


@pytest.mark.parametrize(
    ("key", "title", "expanded"),
    [
        ("segmentation", "Segmentation", True),
        ("time_sampling", "Time & Sampling", True),
        ("crop_cleanup", "Crop & Cleanup", False),
        ("transform", "Transform", False),
        ("output", "Output", True),
        ("workspace", "Workspace", False),
    ],
)
def test_inspector_disclosures_show_visual_and_spoken_state(
    qtbot, key: str, title: str, expanded: bool
) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)
    button, _body = inspector.disclosures[key]

    assert button.isChecked() is expanded
    assert button.arrowType() == (
        Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
    )
    assert button.accessibleDescription() == (
        f"{title}: {'expanded' if expanded else 'collapsed'}"
    )

    button.click()

    assert button.isChecked() is not expanded
    assert button.arrowType() == (
        Qt.ArrowType.RightArrow if expanded else Qt.ArrowType.DownArrow
    )
    assert button.accessibleDescription() == (
        f"{title}: {'collapsed' if expanded else 'expanded'}"
    )


def test_transform_group_is_mounted_inside_its_disclosure_body(qtbot) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)

    _button, body = inspector.disclosures["transform"]

    assert inspector.transform_group.parentWidget() is body


def test_tab_widgets_include_the_transform_disclosure_and_group_in_order(
    qtbot,
) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)

    widgets = inspector.tab_widgets()
    transform_button = inspector.disclosures["transform"][0]
    crop_index = widgets.index(inspector.disclosures["crop_cleanup"][0])
    transform_index = widgets.index(transform_button)
    output_index = widgets.index(inspector.disclosures["output"][0])

    assert crop_index < transform_index < output_index
    group_widgets = inspector.transform_group.tab_widgets()
    assert widgets[transform_index + 1 : transform_index + 1 + len(group_widgets)] == (
        group_widgets
    )


def test_output_directory_middle_elides_while_preserving_full_path_semantics(
    qtbot,
) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)
    path = "/tmp/matteloop/" + "very-long-directory-name/" * 8 + "exports"

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
