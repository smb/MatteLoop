"""Immutable V1 inspector parameters and their reducer events."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

from matteloop.core.errors import ValidationError
from matteloop.core.execution_providers import (
    CPU_EXECUTION_PROVIDER,
    is_allowed_provider,
)
from matteloop.core.specs import (
    EdgeMode,
    FramingSpec,
    OutputSpec,
    SamplingSpec,
    SegmentationSpec,
    TransformSpec,
    is_local_path_syntax,
)

if TYPE_CHECKING:
    from matteloop.core.state import AppState

V1_MODEL_IDS = (
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
)


@dataclass(frozen=True, slots=True)
class ParameterState:
    """Current editable values, independent of source/preview/job lifetime."""

    model_id: str = "birefnet-portrait"
    edge_mode: EdgeMode = EdgeMode.STANDARD
    fps: int = 15
    trim: bool = False
    alpha_threshold: Decimal = Decimal("2.0")
    padding: int = 0
    stretch_x: Decimal = Decimal("1.0")
    output_directory: Path | None = None
    output_filename: str | None = None
    max_mib: Decimal = Decimal("0")
    execution_provider: str = CPU_EXECUTION_PROVIDER
    transform: TransformSpec = field(default_factory=TransformSpec)

    def __post_init__(self) -> None:
        if self.model_id not in V1_MODEL_IDS:
            raise ValueError("model is outside the V1 catalog")
        SamplingSpec(Fraction(0), Fraction(1), self.fps)
        SegmentationSpec(self.model_id, self.edge_mode)
        if not is_allowed_provider(self.execution_provider):
            raise ValueError("execution provider is not allowlisted")
        FramingSpec(
            self.trim,
            self.alpha_threshold,
            self.padding,
            self.stretch_x,
        )
        if self.output_directory is not None and (
            not isinstance(self.output_directory, Path)
            or not is_local_path_syntax(self.output_directory)
        ):
            raise ValueError("output directory must be a local Path")
        if self.output_filename is not None:
            OutputSpec(Path("."), self.output_filename)
        OutputSpec.from_mib(
            self.output_directory or Path("."),
            self.output_filename or "output.webp",
            self.max_mib,
        )


@dataclass(frozen=True, slots=True)
class ModelChanged:
    model_id: str


@dataclass(frozen=True, slots=True)
class EdgeModeChanged:
    edge_mode: EdgeMode


@dataclass(frozen=True, slots=True)
class ExecutionProviderChanged:
    execution_provider: str


@dataclass(frozen=True, slots=True)
class OutputFpsChanged:
    fps: int


@dataclass(frozen=True, slots=True)
class GlobalTrimChanged:
    enabled: bool


@dataclass(frozen=True, slots=True)
class AlphaThresholdChanged:
    value: Decimal


@dataclass(frozen=True, slots=True)
class PaddingChanged:
    value: int


@dataclass(frozen=True, slots=True)
class StretchChanged:
    value: Decimal


@dataclass(frozen=True, slots=True)
class OutputDirectoryChanged:
    directory: Path


@dataclass(frozen=True, slots=True)
class OutputFilenameChanged:
    filename: str


@dataclass(frozen=True, slots=True)
class OutputMaxSizeChanged:
    value: Decimal


@dataclass(frozen=True, slots=True)
class TransformChanged:
    transform: TransformSpec


ParameterEvent = (
    ModelChanged
    | EdgeModeChanged
    | ExecutionProviderChanged
    | OutputFpsChanged
    | GlobalTrimChanged
    | AlphaThresholdChanged
    | PaddingChanged
    | StretchChanged
    | OutputDirectoryChanged
    | OutputFilenameChanged
    | OutputMaxSizeChanged
    | TransformChanged
)


def reduce_parameters(state: AppState, event: ParameterEvent) -> AppState:
    """Apply an inspector event and use the central stale-preview reducer."""
    from matteloop.core.state import capabilities

    if not capabilities(state).can_edit:
        return state
    if isinstance(event, ModelChanged):
        return _reduce_model(state, event)
    if isinstance(event, EdgeModeChanged):
        return _reduce_edge_mode(state, event)
    if isinstance(event, ExecutionProviderChanged):
        return _reduce_execution_provider(state, event)
    if isinstance(event, OutputFpsChanged):
        return _reduce_fps(state, event)
    if isinstance(event, GlobalTrimChanged):
        return _reduce_trim(state, event)
    if isinstance(event, AlphaThresholdChanged):
        return _reduce_cleanup_value(state, event)
    if isinstance(event, PaddingChanged):
        return _reduce_cleanup_value(state, event)
    if isinstance(event, StretchChanged):
        return _reduce_cleanup_value(state, event)
    if isinstance(event, OutputDirectoryChanged):
        return _reduce_output_directory(state, event)
    if isinstance(event, OutputFilenameChanged):
        return _reduce_output_filename(state, event)
    if isinstance(event, OutputMaxSizeChanged):
        return _reduce_output_max_size(state, event)
    if isinstance(event, TransformChanged):
        return _reduce_transform(state, event)
    return state


def _reduce_model(state: AppState, event: ModelChanged) -> AppState:
    from matteloop.core.state import PreviewInvalidated, reduce

    parameters = state.parameters
    if event.model_id not in V1_MODEL_IDS or event.model_id == parameters.model_id:
        return state
    updated = replace(parameters, model_id=event.model_id)
    return reduce(
        replace(state, parameters=updated, model_available=False),
        PreviewInvalidated("Segmentation"),
    )


def _reduce_edge_mode(state: AppState, event: EdgeModeChanged) -> AppState:
    parameters = state.parameters
    if not isinstance(event.edge_mode, EdgeMode) or event.edge_mode not in {
        EdgeMode.STANDARD,
        EdgeMode.DECONTAMINATE_COLORS,
    } or event.edge_mode is parameters.edge_mode:
        return state
    return _invalidate(
        state,
        replace(parameters, edge_mode=event.edge_mode),
        "Segmentation",
    )


def _reduce_execution_provider(
    state: AppState, event: ExecutionProviderChanged
) -> AppState:
    if (
        not is_allowed_provider(event.execution_provider)
        or event.execution_provider == state.parameters.execution_provider
    ):
        return state
    return _invalidate(
        state,
        replace(state.parameters, execution_provider=event.execution_provider),
        "Rechenbeschleunigung",
    )


def _reduce_fps(state: AppState, event: OutputFpsChanged) -> AppState:
    from matteloop.core.state import PreviewInvalidated, reduce

    try:
        SamplingSpec(Fraction(0), Fraction(1), event.fps)
    except Exception:
        return state
    timeline = state.timeline
    if timeline is not None and timeline.end - timeline.start < Fraction(1, event.fps):
        return state
    if event.fps == state.parameters.fps and (
        timeline is None or timeline.fps == event.fps
    ):
        return state
    updated = replace(state.parameters, fps=event.fps)
    updated_timeline = None if timeline is None else replace(timeline, fps=event.fps)
    return reduce(
        replace(state, parameters=updated, timeline=updated_timeline),
        PreviewInvalidated("Sampling"),
    )


def _reduce_trim(state: AppState, event: GlobalTrimChanged) -> AppState:
    if type(event.enabled) is not bool or event.enabled == state.parameters.trim:
        return state
    return _invalidate(
        state,
        replace(state.parameters, trim=event.enabled),
        "Crop & cleanup",
    )


def _reduce_cleanup_value(
    state: AppState, event: AlphaThresholdChanged | PaddingChanged | StretchChanged
) -> AppState:
    try:
        if isinstance(event, AlphaThresholdChanged):
            updated = replace(state.parameters, alpha_threshold=event.value)
        elif isinstance(event, PaddingChanged):
            updated = replace(state.parameters, padding=event.value)
        else:
            updated = replace(state.parameters, stretch_x=event.value)
    except (InvalidOperation, TypeError, ValueError, ValidationError):
        return state
    if updated == state.parameters:
        return state
    return _invalidate(state, updated, "Crop & cleanup")


def _reduce_output_directory(
    state: AppState, event: OutputDirectoryChanged
) -> AppState:
    if (
        not isinstance(event.directory, Path)
        or not is_local_path_syntax(event.directory)
        or event.directory == state.parameters.output_directory
    ):
        return state
    return replace(
        state,
        parameters=replace(state.parameters, output_directory=event.directory),
    )


def _reduce_output_filename(
    state: AppState, event: OutputFilenameChanged
) -> AppState:
    if not is_valid_output_filename(event.filename):
        return state
    if event.filename == state.parameters.output_filename:
        return state
    return replace(
        state,
        parameters=replace(state.parameters, output_filename=event.filename),
    )


def _reduce_output_max_size(
    state: AppState, event: OutputMaxSizeChanged
) -> AppState:
    try:
        updated = replace(state.parameters, max_mib=event.value)
    except (InvalidOperation, TypeError, ValueError, ValidationError):
        return state
    if updated == state.parameters:
        return state
    return replace(state, parameters=updated)


def _reduce_transform(state: AppState, event: TransformChanged) -> AppState:
    """Replace the transform in place; a transform never stales the preview."""
    if (
        not isinstance(event.transform, TransformSpec)
        or event.transform == state.parameters.transform
    ):
        return state
    return replace(
        state, parameters=replace(state.parameters, transform=event.transform)
    )


def _invalidate(
    state: AppState, parameters: ParameterState, category: str
) -> AppState:
    from matteloop.core.state import PreviewInvalidated, reduce

    return reduce(replace(state, parameters=parameters), PreviewInvalidated(category))


def parameters_from_values(values: Mapping[str, object]) -> ParameterState:
    """Parse QSettings-like primitive values with an independent fallback per key."""
    defaults = ParameterState()
    return replace(
        defaults,
        model_id=_model_value(values.get("model_id"), defaults.model_id),
        edge_mode=_edge_value(values.get("edge_mode"), defaults.edge_mode),
        execution_provider=_provider_value(
            values.get("execution_provider"), defaults.execution_provider
        ),
        fps=_int_value(values.get("fps"), defaults.fps, 1, 240),
        trim=_bool_value(values.get("trim"), defaults.trim),
        alpha_threshold=_decimal_value(
            values.get("alpha_threshold"), defaults.alpha_threshold,
            lambda value: Decimal(0) <= value <= Decimal(100),
        ),
        padding=_int_value(values.get("padding"), defaults.padding, 0, None),
        stretch_x=_decimal_value(
            values.get("stretch_x"), defaults.stretch_x, lambda value: value > 0
        ),
        max_mib=_decimal_value(
            values.get("max_mib"), defaults.max_mib, lambda value: value >= 0
        ),
        output_directory=_directory_value(values.get("output_directory")),
        output_filename=_filename_value(values.get("output_filename")),
    )


def output_filename_for_source(parameters: ParameterState, source: Path) -> str:
    """Return the selected name, or the documented source-stem default."""
    return parameters.output_filename or f"{source.stem}.webp"


def output_directory_for_source(parameters: ParameterState, source: Path) -> Path:
    return parameters.output_directory or source.parent


def is_valid_output_filename(value: object) -> bool:
    """Use the frozen output-spec rules for inspector validation."""
    if not isinstance(value, str):
        return False
    try:
        OutputSpec(Path("."), value)
    except Exception:
        return False
    return True


def _model_value(value: object, default: str) -> str:
    return value if isinstance(value, str) and value in V1_MODEL_IDS else default


def _edge_value(value: object, default: EdgeMode) -> EdgeMode:
    if not isinstance(value, (str, EdgeMode)):
        return default
    try:
        edge = EdgeMode(value)
    except (TypeError, ValueError):
        return default
    return (
        edge
        if edge in {EdgeMode.STANDARD, EdgeMode.DECONTAMINATE_COLORS}
        else default
    )


def _provider_value(value: object, default: str) -> str:
    return value if isinstance(value, str) and is_allowed_provider(value) else default


def _int_value(value: object, default: int, minimum: int, maximum: int | None) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value) if isinstance(value, (int, str)) else default
    except (TypeError, ValueError):
        return default
    if isinstance(value, str) and value.strip() != str(parsed):
        return default
    return (
        parsed
        if parsed >= minimum and (maximum is None or parsed <= maximum)
        else default
    )


def _decimal_value(
    value: object, default: Decimal, predicate: Callable[[Decimal], bool]
) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
        if not parsed.is_finite() or not predicate(parsed):
            return default
        return parsed
    except (InvalidOperation, TypeError, ValueError):
        return default


def _bool_value(value: object, default: bool) -> bool:
    if type(value) is bool:
        return value
    if isinstance(value, str):
        if value.casefold() == "true":
            return True
        if value.casefold() == "false":
            return False
    return default


def _directory_value(value: object) -> Path | None:
    if not isinstance(value, (str, Path)) or not str(value):
        return None
    path = value if isinstance(value, Path) else Path(value)
    return path if is_local_path_syntax(path) else None


def _filename_value(value: object) -> str | None:
    if isinstance(value, str) and is_valid_output_filename(value):
        return value
    return None
