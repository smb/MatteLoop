"""Own the "currently open cut" the reducer state cannot hold (D5).

``ParameterState`` has room for a ``TransformSpec`` (D4) but nothing in
``AppState`` names a cut workspace, and ``core/state.py`` is frozen. This
controller fills that gap: it tracks a ``CutSession`` from the render
worker's raw artifact, recomputes the cut-derived ``CutFacts`` the
``TransformGroup`` readout needs whenever a framing-relevant parameter
changes, and restores a cut's stored transform before "Use this set"
rebuilds it (E17).

The session deliberately does not cache a ``FramingSpec`` (T6): the current
framing always comes from ``store.state.parameters``, read fresh on every
recomputation, so a padding/trim/stretch edit is what re-derives the framed
size a stale crop is clamped against (E14) -- caching it here would let the
two drift.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from itertools import count
from typing import TYPE_CHECKING, Protocol, cast

from PIL import Image
from PySide6.QtCore import QObject, QThread, Signal, Slot

from matteloop.core.crop import clamp_crop
from matteloop.core.geometry import PixelBounds, union_alpha_bounds
from matteloop.core.parameters import ParameterState, TransformChanged
from matteloop.core.specs import FramingSpec
from matteloop.core.state import AppState
from matteloop.core.timebase import webp_delays
from matteloop.jobs.transform_stage import framing_plan
from matteloop.jobs.transform_store import load_transform
from matteloop.jobs.workspace import CutFrame, CutManifest, CutWorkspace
from matteloop.ui.crop_canvas import CropCanvas
from matteloop.ui.ports import StateStore
from matteloop.ui.transform_group import CutFacts, TransformGroup

if TYPE_CHECKING:
    from fractions import Fraction


class FrameReader(Protocol):
    """Read one stored cut frame directly by manifest filename."""

    def read(self, workspace: CutWorkspace, frame: CutFrame) -> Image.Image: ...


class _DirectFrameReader:
    """Open a stored frame straight off disk -- never through
    ``CutWorkspace.read_promoted_cut``, which rescans the whole cut per call.
    """

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


class _CutFactsWorker(QObject):
    """Recompute one ``CutFacts`` off the GUI thread (facts can decode frames)."""

    succeeded = Signal(object, int)
    failed = Signal(int)
    finished = Signal()

    def __init__(
        self,
        session: CutSession,
        framing: FramingSpec,
        frame_reader: FrameReader,
        generation: int,
    ) -> None:
        super().__init__()
        self._session = session
        self._framing = framing
        self._frame_reader = frame_reader
        self._generation = generation

    @Slot()
    def run(self) -> None:
        try:
            facts = _compute_facts(self._session, self._framing, self._frame_reader)
        except Exception:
            self.failed.emit(self._generation)
        else:
            self.succeeded.emit(facts, self._generation)
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
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._frame_reader = frame_reader or _DirectFrameReader()
        self._session: CutSession | None = None
        self._facts: CutFacts | None = None
        self._group: TransformGroup | None = None
        self._canvas: CropCanvas | None = None
        self._crop_edit_enabled = False
        self._aspect_lock: Fraction | None = None
        self._generations = count(1)
        self._current_generation = 0
        self._thread: QThread | None = None
        self._worker: _CutFactsWorker | None = None
        self._framing = _framing_from_parameters(store.state.parameters)
        self._unsubscribe: Callable[[], None] | None = store.subscribe(
            self._state_changed
        )

    def attach(self, group: TransformGroup, canvas: CropCanvas | None) -> None:
        """Wire the group's signals and, once it exists, the result canvas.

        ``main_window.py`` only wires ``command_requested`` for the timeline,
        the original canvas, and the inspector (D5) -- a fourth widget needs
        its own connection, which is what this method is for.
        """
        self._group = group
        self._canvas = canvas
        group.use_playhead_requested.connect(self._use_playhead_requested)
        group.crop_edit_toggled.connect(self._crop_edit_toggled)
        group.aspect_lock_changed.connect(self._aspect_lock_changed)
        if canvas is not None:
            canvas.command_requested.connect(self._store.dispatch)
        group.set_cut(self._facts)

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
        """Dispatch the cut's stored transform before a rebuild request is built.

        Clamps the crop when facts for this exact cut are already known
        (E14); otherwise dispatches the raw stored spec and lets the facts
        recomputation triggered by the rebuild's own ``open_artifact`` clamp
        it -- never blocks this call on decoding the cut's frames.
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
        self._current_generation = next(self._generations)
        self._set_facts(None)

    def shutdown(self) -> None:
        if self._unsubscribe is not None:
            unsubscribe, self._unsubscribe = self._unsubscribe, None
            unsubscribe()
        self.close_session()
        self._join_worker()

    @property
    def session(self) -> CutSession | None:
        return self._session

    @property
    def facts(self) -> CutFacts | None:
        return self._facts

    # -- Stage C seams: state is recorded here, consumed by the player -----

    def _use_playhead_requested(self, _edge: str) -> None:
        """No result player yet (Stage C); nothing to answer with."""

    def _crop_edit_toggled(self, enabled: bool) -> None:
        self._crop_edit_enabled = enabled

    def _aspect_lock_changed(self, ratio: object) -> None:
        self._aspect_lock = cast("Fraction | None", ratio)

    # -- facts recomputation -------------------------------------------

    def _state_changed(self, state: AppState) -> None:
        parameters = state.parameters
        framing = _framing_from_parameters(parameters)
        if framing != self._framing:
            self._framing = framing
            if self._session is not None:
                self._schedule_facts()

    def _schedule_facts(self) -> None:
        session = self._session
        if session is None:
            return
        self._join_worker()
        generation = next(self._generations)
        self._current_generation = generation
        worker = _CutFactsWorker(session, self._framing, self._frame_reader, generation)
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.succeeded.connect(self._facts_ready)
        worker.failed.connect(self._facts_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.started.connect(worker.run)
        self._thread = thread
        self._worker = worker
        thread.start()

    @Slot(object, int)
    def _facts_ready(self, facts: object, generation: int) -> None:
        if generation != self._current_generation or not isinstance(facts, CutFacts):
            return
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

    def _join_worker(self) -> None:
        thread = self._thread
        self._thread = None
        self._worker = None
        if thread is None:
            return
        try:
            thread.quit()
            thread.wait(5000)
        except RuntimeError:
            pass


def _framing_from_parameters(parameters: ParameterState) -> FramingSpec:
    return FramingSpec(
        parameters.trim,
        parameters.alpha_threshold,
        parameters.padding,
        parameters.stretch_x,
    )


def _fps_from_manifest(manifest: CutManifest) -> int:
    sampling = manifest.cache_key_inputs["sampling"]
    if not isinstance(sampling, Mapping):
        raise ValueError("manifest sampling inputs are not a mapping")
    fps = sampling["fps"]
    if type(fps) is not int:
        raise ValueError("manifest fps is not an integer")
    return fps


def _compute_facts(
    session: CutSession, framing: FramingSpec, frame_reader: FrameReader
) -> CutFacts:
    manifest = session.manifest
    source_size = (manifest.width, manifest.height)
    union = _resolve_union(session, framing, frame_reader) if framing.trim else None
    plan = framing_plan(source_size, union, framing)
    delays = webp_delays(manifest.frame_count, session.fps)
    return CutFacts(
        cache_key=manifest.cache_key,
        frame_count=manifest.frame_count,
        framed_size=plan.output_size,
        fps=session.fps,
        delays_ms=delays,
    )


def _resolve_union(
    session: CutSession, framing: FramingSpec, frame_reader: FrameReader
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
        _iter_stored_frames(session, frame_reader), framing.alpha_threshold
    )


def _iter_stored_frames(
    session: CutSession, frame_reader: FrameReader
) -> Iterator[Image.Image]:
    for frame in session.manifest.frames:
        image = frame_reader.read(session.workspace, frame)
        try:
            yield image
        finally:
            image.close()
