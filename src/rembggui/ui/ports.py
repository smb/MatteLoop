"""Small ports joining the reducer store to the Qt shell."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rembggui.core.crop_state import CropEvent
from rembggui.core.parameters import ParameterEvent
from rembggui.core.state import AppState, Event
from rembggui.core.timeline import TimelineEvent


class StateStore(Protocol):
    """Reducer-backed state source consumed by the window."""

    @property
    def state(self) -> AppState: ...

    def dispatch(self, event: Event) -> None: ...

    def subscribe(self, listener: Callable[[AppState], None]) -> Callable[[], None]: ...


@dataclass(frozen=True)
class ChooseVideoRequested:
    replace: bool = False


@dataclass(frozen=True)
class VideoDropped:
    """One validated local video selected through the native drop target."""

    path: Path


@dataclass(frozen=True)
class PreviewFrameRequested:
    pass


@dataclass(frozen=True)
class RenderVideoRequested:
    pass


@dataclass(frozen=True)
class RebuildEditedCutsRequested:
    pass


@dataclass(frozen=True)
class OpenOutputRequested:
    pass


@dataclass(frozen=True)
class OpenOutputFolderRequested:
    pass


@dataclass(frozen=True)
class OutputDirectoryRequested:
    pass


@dataclass(frozen=True)
class ManageModelsRequested:
    pass


@dataclass(frozen=True)
class ManageWorkspacesRequested:
    pass


type WindowCommand = (
    ChooseVideoRequested
    | VideoDropped
    | PreviewFrameRequested
    | RenderVideoRequested
    | RebuildEditedCutsRequested
    | OpenOutputRequested
    | OpenOutputFolderRequested
    | OutputDirectoryRequested
    | ManageModelsRequested
    | ManageWorkspacesRequested
    | CropEvent
    | TimelineEvent
    | ParameterEvent
)


class WindowServices(Protocol):
    """Controller-owned command dispatcher; implemented in Task 15."""

    def dispatch(self, command: WindowCommand) -> None: ...
