from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QSettings

from rembggui.core.parameters import ParameterState
from rembggui.core.specs import EdgeMode
from rembggui.ui.preferences import load_parameters, persist_parameters


def _settings() -> QSettings:
    settings = QSettings(
        QSettings.IniFormat,
        QSettings.UserScope,
        "rembggui-test",
        "parameter-persistence",
    )
    settings.clear()
    return settings


def test_parameter_preferences_round_trip_as_qsettings_primitives() -> None:
    settings = _settings()
    expected = ParameterState(
        model_id="isnet-general-use",
        edge_mode=EdgeMode.DECONTAMINATE_COLORS,
        fps=90,
        trim=True,
        alpha_threshold=Decimal("3.5"),
        padding=9,
        stretch_x=Decimal("1.2"),
        output_directory=Path("/exports"),
        output_filename="last.webp",
        max_mib=Decimal("2.5"),
    )

    persist_parameters(settings, expected)
    actual = load_parameters(settings)

    assert actual == expected
