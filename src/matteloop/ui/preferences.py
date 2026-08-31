"""QSettings adapter for primitive inspector preferences."""

from __future__ import annotations

from PySide6.QtCore import QSettings

from matteloop.core.execution_providers import ProviderOption, select_provider
from matteloop.core.parameters import ParameterState, parameters_from_values

_PREFIX = "parameters/"
_KEYS = (
    "model_id",
    "edge_mode",
    "execution_provider",
    "fps",
    "trim",
    "alpha_threshold",
    "padding",
    "stretch_x",
    "output_directory",
    "output_filename",
    "max_mib",
)


def load_parameters(
    settings: QSettings, provider_options: tuple[ProviderOption, ...] | None = None
) -> ParameterState:
    """Read each primitive independently; malformed settings use field defaults."""
    values = {name: settings.value(f"{_PREFIX}{name}") for name in _KEYS}
    parameters = parameters_from_values(values)
    if provider_options is None:
        return parameters
    return parameters_from_values(
        {**values, "execution_provider": select_provider(
            values.get("execution_provider"), provider_options
        )}
    )


def persist_parameters(settings: QSettings, parameters: ParameterState) -> None:
    """Persist only simple values; source and job state never enter settings."""
    settings.setValue(f"{_PREFIX}model_id", parameters.model_id)
    settings.setValue(f"{_PREFIX}edge_mode", parameters.edge_mode.value)
    settings.setValue(
        f"{_PREFIX}execution_provider", parameters.execution_provider
    )
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
