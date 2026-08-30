"""Small reducer-backed store used by the Qt shell and controller."""

from __future__ import annotations

from collections.abc import Callable

from rembggui.core.state import AppState, Event, reduce


class ReducerStore:
    """Keep application state behind the pure reducer and notify the view."""

    def __init__(self, initial_state: AppState | None = None) -> None:
        self._state = initial_state if initial_state is not None else AppState()
        self._listeners: list[Callable[[AppState], None]] = []

    @property
    def state(self) -> AppState:
        return self._state

    def dispatch(self, event: Event) -> None:
        self._state = reduce(self._state, event)
        for listener in tuple(self._listeners):
            listener(self._state)

    def subscribe(self, listener: Callable[[AppState], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            self._listeners.remove(listener)

        return unsubscribe
