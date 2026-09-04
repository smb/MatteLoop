"""Own the "currently open cut" the reducer state cannot hold (D5).

``ParameterState`` has room for a ``TransformSpec`` (D4) but nothing in
``AppState`` names a cut workspace, and ``core/state.py`` is frozen. This
controller tracks a ``CutSession`` from the render worker's raw artifact,
recomputes the cut-derived ``CutFacts`` the ``TransformGroup`` readout needs
whenever a framing-relevant parameter changes, and restores a cut's stored
transform before "Use this set" rebuilds it (E17).

It deliberately does not cache a ``FramingSpec`` (T6): framing always comes
from ``store.state.parameters``, read fresh each recomputation, so an edit
re-derives the framed size a stale crop is clamped against (E14).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from itertools import count
from typing import TYPE_CHECKING, Protocol, cast

from PIL import Image
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from matteloop.core.crop import clamp_crop
from matteloop.core.geometry import FramingPlan, PixelBounds, union_alpha_bounds
from matteloop.core.parameters import ParameterState, TransformChanged
from matteloop.core.specs import CropSpec, FramingSpec, TransformSpec
from matteloop.core.state import AppState
from matteloop.core.timebase import webp_delays
from matteloop.jobs.transform_stage import framing_plan
from matteloop.jobs.transform_store import load_transform
from matteloop.jobs.workspace import CutFrame, CutManifest, CutWorkspace
from matteloop.ui.crop_canvas import CropCanvas
from matteloop.ui.crop_presentation import CropPresentation
from matteloop.ui.ports import StateStore
from matteloop.ui.result_player import (
    PLAYER_CACHE_BUDGET_BYTES,
    FrameLoadWorker,
    PlayerFrames,
    ResultPlayerCanvas,
)
from matteloop.ui.transform_group import CutFacts, TransformGroup

if TYPE_CHECKING:
    from fractions import Fraction


class FrameReader(Protocol):
    """Read one stored cut frame directly by manifest filename."""

    def read(self, workspace: CutWorkspace, frame: CutFrame) -> Image.Image: ...


class _DirectFrameReader:
    """Open a stored frame straight off disk -- never through
    ``read_promoted_cut``, which rescans the whole cut per call."""

    def read(self, workspace: CutWorkspace, frame: CutFrame) -> Image.Image:
        with Image.open(workspace.path / frame.filename) as opened:
            opened.load()
            return opened.convert("RGBA")


@dataclass(frozen=True)
class CutSession:
    """The cut currently open in the Transform group -- no framing cached."""

    workspace: CutWorkspace
    manifest: CutManifest
    fps: int


class _CutFactsCancelled(Exception):
    """Unwinds ``_iter_stored_frames`` once cancelled."""


class _CutFactsWorker(QObject):
    """Recompute one ``CutFacts`` off the GUI thread; also resolves the
    ``FramingPlan`` the player's frame loader needs (C2) to apply per frame."""

    succeeded = Signal(object, object, int)
    failed = Signal(int)
    finished = Signal()

    def __init__(
        self, session: CutSession, framing: FramingSpec, frame_reader: FrameReader,
        generation: int, cancelled: Callable[[], bool] = lambda: False,
    ) -> None:
        super().__init__()
        self._session = session
        self._framing = framing
        self._frame_reader = frame_reader
        self._generation = generation
        self._cancelled = cancelled

    @Slot()
    def run(self) -> None:
        try:
            facts, plan = _compute_facts(
                self._session, self._framing, self._frame_reader, self._cancelled
            )
        except _CutFactsCancelled:
            pass
        except Exception:
            self.failed.emit(self._generation)
        else:
            self.succeeded.emit(facts, plan, self._generation)
        finally:
            self.finished.emit()


class TransformStageController(QObject):
    """Track the open cut, its derived facts, and the group/canvas it feeds."""

    facts_changed = Signal(object)

    def __init__(
        self,
        store: StateStore,
        *,
        frame_reader: FrameReader | None = None,
        cache_budget: int = PLAYER_CACHE_BUDGET_BYTES,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._frame_reader = frame_reader or _DirectFrameReader()
        self._session: CutSession | None = None
        self._facts: CutFacts | None = None
        self._plan: FramingPlan | None = None
        self._group: TransformGroup | None = None
        self._canvas: CropCanvas | None = None
        self._player_canvas: ResultPlayerCanvas | None = None
        self._crop_edit_enabled = False
        self._aspect_lock: Fraction | None = None
        self._generations = count(1)
        self._current_generation = 0
        self._thread: QThread | None = None
        self._worker: _CutFactsWorker | None = None
        self._facts_cancel_event: threading.Event | None = None
        # Superseded pairs stay referenced until genuinely finished -- see
        # _join_worker / _cancel_frame_load.
        self._retiring: list[tuple[QThread, QObject]] = []
        self._framing = _framing_from_parameters(store.state.parameters)
        self._last_transform: TransformSpec = store.state.parameters.transform
        self._player_frames: PlayerFrames | None = None
        self._frame_generations = count(1)
        self._frame_generation = 0
        self._frame_thread: QThread | None = None
        self._frame_worker: FrameLoadWorker | None = None
        self._frame_cancel_event: threading.Event | None = None
        self._cache_budget = cache_budget
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(250)
        self._debounce_timer.timeout.connect(self._start_frame_load)
        self._unsubscribe: Callable[[], None] | None = store.subscribe(
            self._state_changed
        )

    def attach(self, group: TransformGroup, canvas: CropCanvas | None) -> None:
        """Wire the group's signals and, once it exists, the result canvas.

        ``main_window.py`` only wires ``command_requested`` for the timeline,
        the original canvas, and the inspector (D5) -- a fourth widget needs
        its own connection, wired here."""
        self._group = group
        self._canvas = canvas
        self._player_canvas = canvas if isinstance(canvas, ResultPlayerCanvas) else None
        group.use_playhead_requested.connect(self._use_playhead_requested)
        group.crop_edit_toggled.connect(self._crop_edit_toggled)
        group.aspect_lock_changed.connect(self._aspect_lock_changed)
        if canvas is not None:
            canvas.command_requested.connect(self._store.dispatch)
        if self._player_canvas is not None:
            self._player_canvas.playhead_changed.connect(group.set_playhead_frame)
        group.set_cut(self._facts)
        self._sync_player_frames(immediate=True)

    def open_artifact(self, artifact: object) -> None:
        """Adopt the cut behind a just-finished render/rebuild, if it has one."""
        workspace = getattr(artifact, "cut_workspace", None)
        manifest = getattr(artifact, "manifest", None)
        if not isinstance(workspace, CutWorkspace) or not isinstance(
            manifest, CutManifest
        ):
            return
        self._session = CutSession(workspace, manifest, _fps_from_manifest(manifest))
        self._schedule_facts()

    def restore_for(self, workspace: CutWorkspace) -> None:
        """Dispatch the cut's stored transform before a rebuild request.

        Clamps the crop when facts for this exact cut are already known
        (E14); otherwise the rebuild's own ``open_artifact`` clamps it --
        this call never blocks on decoding the cut's frames.
        """
        transform = load_transform(workspace)
        facts = self._facts
        if (
            facts is not None
            and facts.cache_key == workspace.cache_key
            and transform.crop is not None
        ):
            clamped = clamp_crop(transform.crop, *facts.framed_size)
            if clamped != transform.crop:
                transform = replace(transform, crop=clamped)
        self._store.dispatch(TransformChanged(transform))

    def close_session(self) -> None:
        self._session = None
        self._plan = None
        self._current_generation = next(self._generations)
        self._set_facts(None)

    def shutdown(self) -> None:
        if self._unsubscribe is not None:
            unsubscribe, self._unsubscribe = self._unsubscribe, None
            unsubscribe()
        # Join the frame loader before close_session() clears _frame_thread
        # via _sync_player_frames's own (non-waiting) cancel.
        self._cancel_frame_load(wait=True)
        self.close_session()
        self._join_worker(wait=True)
        for thread, _worker in self._retiring:
            try:
                thread.wait(5000)  # bounded: a deadlocked retiree can't hang shutdown
            except RuntimeError:
                pass
        self._retiring.clear()

    @property
    def session(self) -> CutSession | None:
        return self._session

    @property
    def facts(self) -> CutFacts | None:
        return self._facts

    # -- Stage C: playhead coupling and crop-edit mode ----------------------

    def _use_playhead_requested(self, edge: str) -> None:
        """Resolve "Use playhead" against the player's current stored frame."""
        canvas = self._player_canvas
        if canvas is None:
            return
        index = canvas.current_frame
        if index is None:
            return
        transform = self._store.state.parameters.transform
        updated = (
            replace(transform, first_frame=index)
            if edge == "first"
            else replace(transform, last_frame=index)
        )
        if updated != transform:
            self._store.dispatch(TransformChanged(updated))

    def _crop_edit_toggled(self, enabled: bool) -> None:
        was_enabled = self._crop_edit_enabled
        self._crop_edit_enabled = enabled
        canvas = self._player_canvas
        if canvas is None:
            return
        transform = self._store.state.parameters.transform
        presentation = self._crop_edit_presentation(transform) if enabled else None
        canvas.set_crop_edit(enabled, presentation, transform)
        if was_enabled and not enabled:
            self._sync_player_frames(immediate=False)

    def _crop_edit_presentation(
        self, transform: TransformSpec
    ) -> CropPresentation | None:
        facts = self._facts
        if facts is None:
            return None
        width, height = facts.framed_size
        crop = transform.crop or CropSpec(0, 0, width, height)
        return CropPresentation(
            source_id=facts.cache_key,
            width=width,
            height=height,
            coded_width=width,
            coded_height=height,
            rotation=0,
            pixel_aspect=1.0,
            crop=crop,
        )

    def _aspect_lock_changed(self, ratio: object) -> None:
        self._aspect_lock = cast("Fraction | None", ratio)
        if self._player_canvas is not None:
            self._player_canvas.set_aspect_lock(self._aspect_lock)

    # -- facts recomputation -------------------------------------------

    def _state_changed(self, state: AppState) -> None:
        parameters = state.parameters
        framing = _framing_from_parameters(parameters)
        framing_changed = framing != self._framing
        if framing_changed:
            self._framing = framing
            if self._session is not None:
                self._schedule_facts()
        transform = parameters.transform
        transform_changed = transform != self._last_transform
        self._last_transform = transform
        facts = self._facts
        if self._player_canvas is not None and facts is not None:
            kept = transform.kept_range(facts.frame_count)
            self._player_canvas.set_kept_range(kept)
            crop_editing = self._crop_edit_enabled
            if transform_changed and not framing_changed:
                if crop_editing:
                    self._refresh_crop_edit_presentation(transform)
                else:
                    self._sync_player_frames(immediate=False)

    def _refresh_crop_edit_presentation(self, transform: TransformSpec) -> None:
        """Re-apply the crop-edit presentation for *transform*, skipping the
        frame reload ``_sync_player_frames`` would trigger (E33): the canvas
        overlay only refreshes inside ``apply_presentation``, so a stale
        presentation stalls a drag and repeats a nudge's first pixel.
        """
        canvas = self._player_canvas
        if canvas is None:
            return
        presentation = self._crop_edit_presentation(transform)
        canvas.set_crop_edit(True, presentation, transform)

    def _schedule_facts(self) -> None:
        session = self._session
        if session is None:
            return
        self._join_worker()
        generation = next(self._generations)
        self._current_generation = generation
        cancel = self._facts_cancel_event = threading.Event()
        worker = _CutFactsWorker(
            session, self._framing, self._frame_reader, generation, cancel.is_set
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.succeeded.connect(self._facts_ready)
        worker.failed.connect(self._facts_failed)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._facts_thread_finished)
        thread.finished.connect(thread.deleteLater)
        thread.started.connect(worker.run)
        self._thread = thread
        self._worker = worker
        thread.start()

    @Slot()
    def _facts_thread_finished(self) -> None:
        """Null the current-thread refs once genuinely finished -- always
        safe by then, unlike a supersede (see ``_retire``).
        """
        if self.sender() is self._thread:
            self._thread = None
            self._worker = None

    @Slot(object, object, int)
    def _facts_ready(self, facts: object, plan: object, generation: int) -> None:
        if (
            generation != self._current_generation
            or not isinstance(facts, CutFacts)
            or not isinstance(plan, FramingPlan)
        ):
            return
        self._plan = plan
        self._clamp_current_crop(facts)
        self._set_facts(facts)

    @Slot(int)
    def _facts_failed(self, generation: int) -> None:
        if generation == self._current_generation:
            self._set_facts(None)

    def _clamp_current_crop(self, facts: CutFacts) -> None:
        transform = self._store.state.parameters.transform
        crop = transform.crop
        if crop is None:
            return
        clamped = clamp_crop(crop, *facts.framed_size)
        if clamped != crop:
            self._store.dispatch(TransformChanged(replace(transform, crop=clamped)))

    def _set_facts(self, facts: CutFacts | None) -> None:
        self._facts = facts
        if self._group is not None:
            self._group.set_cut(facts)
        self.facts_changed.emit(facts)
        self._sync_player_frames(immediate=True)

    def _join_worker(self, *, wait: bool = False) -> None:
        """Detach the current facts worker/thread -- see ``_retire``."""
        if self._facts_cancel_event is not None:
            self._facts_cancel_event.set()
            self._facts_cancel_event = None
        thread, self._thread = self._thread, None
        worker, self._worker = self._worker, None
        self._retire(thread, worker, wait=wait)

    def _retire(
        self, thread: QThread | None, worker: QObject | None, *, wait: bool
    ) -> None:
        """Quit *thread*; wait bounded if *wait*, else retire the pair (a
        drop before it starts/finishes risks the SIGSEGV this guards
        against). Also prunes retirees that have finished."""
        self._retiring = [p for p in self._retiring if not _thread_is_finished(p[0])]
        if thread is None:
            return
        try:
            thread.quit()
            if wait:
                thread.wait(5000)
            elif worker is not None:
                self._retiring.append((thread, worker))
        except RuntimeError:
            pass

    # -- Stage C: the player's frame loader -------------------------------

    def _sync_player_frames(self, *, immediate: bool) -> None:
        canvas = self._player_canvas
        if canvas is None:
            return
        if self._session is None or self._facts is None or self._plan is None:
            self._cancel_frame_load()
            self._player_frames = None
            canvas.set_frames(None)
            return
        if immediate:
            self._start_frame_load()
        else:
            self._debounce_timer.start()

    def _start_frame_load(self) -> None:
        canvas = self._player_canvas
        session = self._session
        facts = self._facts
        plan = self._plan
        if canvas is None or session is None or facts is None or plan is None:
            return
        self._cancel_frame_load()
        generation = next(self._frame_generations)
        self._frame_generation = generation
        cancel_event = threading.Event()
        self._frame_cancel_event = cancel_event
        transform = self._store.state.parameters.transform
        worker = FrameLoadWorker(
            session.workspace,
            session.manifest,
            self._frame_reader,
            plan,
            transform,
            facts.delays_ms,
            generation,
            budget=self._cache_budget,
            cancelled=cancel_event.is_set,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.succeeded.connect(self._frame_load_succeeded)
        worker.failed.connect(self._frame_load_failed)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._frame_thread_finished)
        thread.finished.connect(thread.deleteLater)
        thread.started.connect(worker.run)
        self._frame_thread = thread
        self._frame_worker = worker
        thread.start()

    @Slot()
    def _frame_thread_finished(self) -> None:
        """Mirror ``_facts_thread_finished`` for the frame loader."""
        if self.sender() is self._frame_thread:
            self._frame_thread = None
            self._frame_worker = None

    @Slot(object, int)
    def _frame_load_succeeded(self, frames: object, generation: int) -> None:
        if generation != self._frame_generation or not isinstance(frames, PlayerFrames):
            return
        self._player_frames = frames
        if self._player_canvas is not None:
            self._player_canvas.set_frames(frames)

    @Slot(str, int)
    def _frame_load_failed(self, message: str, generation: int) -> None:
        if generation != self._frame_generation:
            return
        if self._player_canvas is not None:
            self._player_canvas.set_status_marker(message)

    def _cancel_frame_load(self, *, wait: bool = False) -> None:
        """Stop the in-flight frame load, if any, without blocking on it.

        The worker checks ``_frame_cancel_event`` between frames (E23), so
        setting it here lets a superseded load stop itself -- ``quit()``
        alone cannot interrupt its running slot. Bumping the generation
        (E24) means a load that slips past cancellation cannot resurrect
        frames for a session this call is closing. Only ``shutdown()``
        passes ``wait=True``; otherwise the pair is retired -- see ``_retire``.
        """
        self._debounce_timer.stop()
        self._frame_generation = next(self._frame_generations)
        if self._frame_cancel_event is not None:
            self._frame_cancel_event.set()
            self._frame_cancel_event = None
        thread, self._frame_thread = self._frame_thread, None
        worker, self._frame_worker = self._frame_worker, None
        self._retire(thread, worker, wait=wait)


def _framing_from_parameters(parameters: ParameterState) -> FramingSpec:
    return FramingSpec(
        parameters.trim,
        parameters.alpha_threshold,
        parameters.padding,
        parameters.stretch_x,
    )


def _thread_is_finished(thread: QThread) -> bool:
    """A ``RuntimeError`` means the QThread's C++ object was already
    collected -- itself proof the thread is done.
    """
    try:
        return thread.isFinished()
    except RuntimeError:
        return True


def _fps_from_manifest(manifest: CutManifest) -> int:
    sampling = manifest.cache_key_inputs["sampling"]
    if not isinstance(sampling, Mapping):
        raise ValueError("manifest sampling inputs are not a mapping")
    fps = sampling["fps"]
    if type(fps) is not int:
        raise ValueError("manifest fps is not an integer")
    return fps


def _compute_facts(
    session: CutSession, framing: FramingSpec, frame_reader: FrameReader,
    cancelled: Callable[[], bool] = lambda: False,
) -> tuple[CutFacts, FramingPlan]:
    manifest = session.manifest
    source_size = (manifest.width, manifest.height)
    trim = framing.trim
    union = _resolve_union(session, framing, frame_reader, cancelled) if trim else None
    plan = framing_plan(source_size, union, framing)
    delays = webp_delays(manifest.frame_count, session.fps)
    facts = CutFacts(
        cache_key=manifest.cache_key,
        frame_count=manifest.frame_count,
        framed_size=plan.output_size,
        fps=session.fps,
        delays_ms=delays,
    )
    return facts, plan


def _resolve_union(
    session: CutSession, framing: FramingSpec, frame_reader: FrameReader,
    cancelled: Callable[[], bool] = lambda: False,
) -> PixelBounds:
    metadata = session.manifest.union_metadata
    if metadata is not None:
        try:
            matches = Decimal(metadata.alpha_threshold) == framing.alpha_threshold
        except ArithmeticError:
            matches = False
        if matches:
            left, top, right, bottom = metadata.bounds
            return PixelBounds(left, top, right, bottom)
    return union_alpha_bounds(
        _iter_stored_frames(session, frame_reader, cancelled), framing.alpha_threshold
    )


def _iter_stored_frames(
    session: CutSession, frame_reader: FrameReader,
    cancelled: Callable[[], bool] = lambda: False,
) -> Iterator[Image.Image]:
    for frame in session.manifest.frames:
        if cancelled():
            raise _CutFactsCancelled
        image = frame_reader.read(session.workspace, frame)
        try:
            yield image
        finally:
            image.close()
