from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QSettings

from rembggui.core.execution_providers import (
    CPU_EXECUTION_PROVIDER,
    CUDA_EXECUTION_PROVIDER,
    ProviderOption,
)
from rembggui.core.parameters import OutputFilenameChanged, ParameterState
from rembggui.core.specs import EdgeMode
from rembggui.core.state import AppState, SourceState
from rembggui.ui.controller import SourceController
from rembggui.ui.preferences import load_parameters, persist_parameters
from rembggui.ui.store import ReducerStore


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
        execution_provider=CUDA_EXECUTION_PROVIDER,
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
    actual = load_parameters(
        settings,
        (
            ProviderOption(CPU_EXECUTION_PROVIDER, "CPU"),
            ProviderOption(CUDA_EXECUTION_PROVIDER, "NVIDIA CUDA"),
        ),
    )

    assert actual == expected


def test_unavailable_saved_provider_falls_back_to_the_available_cpu_choice() -> None:
    settings = _settings()
    persist_parameters(
        settings,
        ParameterState(execution_provider="CUDAExecutionProvider"),
    )

    actual = load_parameters(
        settings,
        (ProviderOption(CPU_EXECUTION_PROVIDER, "CPU"),),
    )

    assert actual.execution_provider == CPU_EXECUTION_PROVIDER


def test_failed_provider_is_not_reintroduced_by_a_later_parameter_save() -> None:
    settings = _settings()
    store = ReducerStore(
        AppState(
            source=SourceState.READY,
            source_id="source",
            source_value=object(),
            parameters=ParameterState(execution_provider=CUDA_EXECUTION_PROVIDER),
        )
    )
    controller = SourceController.__new__(SourceController)
    controller._store = store
    controller._settings = settings
    controller._working_provider = CPU_EXECUTION_PROVIDER
    controller._failed_provider = CUDA_EXECUTION_PROVIDER

    controller._dispatch_parameter(OutputFilenameChanged("next.webp"))

    assert settings.value("parameters/execution_provider") == CPU_EXECUTION_PROVIDER
