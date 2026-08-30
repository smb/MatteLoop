"""QSettings adapter for primitive inspector preferences."""

from __future__ import annotations

from PySide6.QtCore import QSettings

from rembggui.core.parameters import ParameterState, parameters_from_values

_PREFIX = "parameters/"
_KEYS = (
    "model_id",
    "edge_mode",
    "fps",
    "trim",
    "alpha_threshold",
    "padding",
    "stretch_x",
    "output_directory",
    "output_filename",
    "max_mib",
)


def load_parameters(settings: QSettings) -> ParameterState:
    """Read each primitive independently; malformed settings use field defaults."""
    values = {name: settings.value(f"{_PREFIX}{name}") for name in _KEYS}
    return parameters_from_values(values)


def persist_parameters(settings: QSettings, parameters: ParameterState) -> None:
    """Persist only simple values; source and job state never enter settings."""
    settings.setValue(f"{_PREFIX}model_id", parameters.model_id)
    settings.setValue(f"{_PREFIX}edge_mode", parameters.edge_mode.value)
    settings.setValue(f"{_PREFIX}fps", parameters.fps)
    settings.setValue(f"{_PREFIX}trim", parameters.trim)
    settings.setValue(
        f"{_PREFIX}alpha_threshold", str(parameters.alpha_threshold)
    )
    settings.setValue(f"{_PREFIX}padding", parameters.padding)
    settings.setValue(f"{_PREFIX}stretch_x", str(parameters.stretch_x))
    if parameters.output_directory is None:
        settings.remove(f"{_PREFIX}output_directory")
    else:
        settings.setValue(
            f"{_PREFIX}output_directory", str(parameters.output_directory)
        )
    if parameters.output_filename is None:
        settings.remove(f"{_PREFIX}output_filename")
    else:
        settings.setValue(f"{_PREFIX}output_filename", parameters.output_filename)
    settings.setValue(f"{_PREFIX}max_mib", str(parameters.max_mib))
