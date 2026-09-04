"""The controller owning the currently open cut and its derived facts (D5)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from PIL import Image
from PySide6.QtCore import Qt

from matteloop.core.crop_state import CropChanged
from matteloop.core.parameters import (
    AlphaThresholdChanged,
    GlobalTrimChanged,
    PaddingChanged,
    TransformChanged,
)
from matteloop.core.specs import CropSpec, FramingSpec, TransformSpec
from matteloop.core.state import (
    AppState,
    JobKind,
    SourceLoaded,
    SourceLoadRequested,
    reduce,
)
from matteloop.core.timebase import webp_delays
from matteloop.jobs.render import FilesystemWorkspacePort
from matteloop.jobs.transform_stage import framing_plan
from matteloop.jobs.transform_store import store_transform
from matteloop.jobs.workspace import CutFrame, CutWorkspace
from matteloop.ui.crop_canvas import CropCanvas
from matteloop.ui.result_player import PlayerFrames, ResultPlayerCanvas
from matteloop.ui.store import ReducerStore
from matteloop.ui.transform_group import CutFacts, TransformGroup
from matteloop.ui.transform_stage import TransformStageController, _DirectFrameReader
from tests.jobs.render_support import job, render_service, request


@dataclass(frozen=True)
class _Metadata:
    path: Path
    width: int = 128
    height: int = 128
    duration: Fraction = Fraction(2)
    average_rate: Fraction = Fraction(30)


def _ready_state(path: Path) -> AppState:
    """A minimal READY state: ``TransformChanged`` is gated on ``can_edit``,
    which requires a loaded source (``core/state.py::capabilities``)."""
    loading = reduce(AppState(), SourceLoadRequested("source", "load"))
    return reduce(loading, SourceLoaded("source", "load", _Metadata(path)))


class _FakeStore:
    """A ``StateStore`` double that records dispatches without reducing them."""

    def __init__(self, state: AppState) -> None:
        self._state = state
        self.dispatched: list[object] = []
        self._listeners: list[Callable[[AppState], None]] = []

    @property
    def state(self) -> AppState:
        return self._state

    def dispatch(self, event: object) -> None:
        self.dispatched.append(event)

    def subscribe(self, listener: Callable[[AppState], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)


def _seed_cut(tmp_path: Path, job_id: str):
    return render_service(workspace=FilesystemWorkspacePort()).render(
        request(tmp_path), job(tmp_path, job_id, JobKind.RENDER)
    )


def test_open_artifact_without_a_cut_workspace_is_ignored(qtbot) -> None:
    store = _FakeStore(AppState())
    controller = TransformStageController(store)
    events: list[object] = []
    controller.facts_changed.connect(events.append)

    controller.open_artifact(object())

    assert controller.session is None
    assert events == []
    controller.shutdown()


def test_open_artifact_computes_facts_matching_the_shared_framing_stage(
    tmp_path, qtbot
) -> None:
    artifact = _seed_cut(tmp_path, "seed-facts")
    manifest = artifact.manifest
    store = _FakeStore(AppState())
    controller = TransformStageController(store)

    controller.open_artifact(artifact)

    qtbot.waitUntil(lambda: controller.facts is not None, timeout=5000)
    facts = controller.facts
    assert facts is not None
    sampling = manifest.cache_key_inputs["sampling"]
    fps = sampling["fps"]
    assert facts.cache_key == manifest.cache_key
    assert facts.frame_count == manifest.frame_count
    assert facts.fps == fps
    assert facts.delays_ms == webp_delays(manifest.frame_count, fps)
    plan = framing_plan((manifest.width, manifest.height), None, FramingSpec())
    assert facts.framed_size == plan.output_size
    controller.shutdown()


def test_restore_for_dispatches_identity_then_the_stored_transform(
    tmp_path, qtbot
) -> None:
    workspace = _seed_cut(tmp_path, "seed-restore").cut_workspace
    store = ReducerStore(_ready_state(tmp_path / "source.mp4"))
    controller = TransformStageController(store)

    controller.restore_for(workspace)
    assert store.state.parameters.transform == TransformSpec()

    spec = TransformSpec(first_frame=1)
    store_transform(workspace, spec, [])

    controller.restore_for(workspace)
    assert store.state.parameters.transform == spec
    controller.shutdown()


def test_padding_change_recomputes_facts_from_the_shared_framing_stage(
    tmp_path, qtbot
) -> None:
    artifact = _seed_cut(tmp_path, "seed-padding")
    manifest = artifact.manifest
    store = ReducerStore(_ready_state(tmp_path / "source.mp4"))
    controller = TransformStageController(store)
    controller.open_artifact(artifact)
    qtbot.waitUntil(lambda: controller.facts is not None, timeout=5000)
    original_size = controller.facts.framed_size

    store.dispatch(PaddingChanged(10))

    qtbot.waitUntil(
        lambda: (
            controller.facts is not None
            and controller.facts.framed_size != original_size
        ),
        timeout=5000,
    )
    parameters = store.state.parameters
    expected = framing_plan(
        (manifest.width, manifest.height),
        None,
        FramingSpec(False, parameters.alpha_threshold, 10, parameters.stretch_x),
    )
    assert controller.facts.framed_size == expected.output_size
    controller.shutdown()


def test_facts_ready_clamps_a_crop_left_over_from_a_larger_framed_size(
    tmp_path, qtbot
) -> None:
    store = ReducerStore(_ready_state(tmp_path / "source.mp4"))
    controller = TransformStageController(store)
    oversized_crop = CropSpec(0, 0, 200, 200)
    store.dispatch(TransformChanged(TransformSpec(crop=oversized_crop)))
    assert store.state.parameters.transform.crop == oversized_crop

    smaller_facts = CutFacts(
        cache_key="a" * 64,
        frame_count=4,
        framed_size=(128, 128),
        fps=15,
        delays_ms=(67, 67, 67, 67),
    )
    plan = framing_plan((128, 128), None, FramingSpec())
    controller._facts_ready(  # noqa: SLF001
        smaller_facts, plan, controller._current_generation
    )

    clamped = store.state.parameters.transform.crop
    assert clamped is not None
    assert clamped.width <= 128
    assert clamped.height <= 128
    assert clamped != oversized_crop
    assert controller.facts == smaller_facts
    controller.shutdown()


def test_shutdown_joins_the_facts_worker_and_clears_the_session(
    tmp_path, qtbot
) -> None:
    artifact = _seed_cut(tmp_path, "seed-shutdown")
    store = _FakeStore(AppState())
    controller = TransformStageController(store)
    controller.open_artifact(artifact)

    controller.shutdown()

    assert controller.session is None
    assert controller.facts is None
    assert controller._thread is None  # noqa: SLF001


class _StallFirstReadReader:
    """Read the first stored frame normally but hold it open until told to
    proceed -- this is what proves the facts worker is genuinely mid-decode,
    not merely fast, by the time ``shutdown()`` lands on it.
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._inner = _DirectFrameReader()

    def read(self, workspace: CutWorkspace, frame: CutFrame) -> Image.Image:
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            self.release.wait(5)
        return self._inner.read(workspace, frame)


def test_shutdown_cancels_a_live_facts_computation_instead_of_waiting_for_it(
    tmp_path, qtbot
) -> None:
    """Regression pin for CI run 33856558458 (job "test (ui)", segfault, exit
    139, reproduced 3/3 on macOS): ``open_artifact`` sets ``self._session``
    synchronously and only schedules the facts worker afterwards, so a
    ``qtbot.waitUntil(lambda: controller.session is not None)`` in the caller
    can return while the worker is still decoding frames for the trim union.
    ``QThread.quit()`` cannot interrupt a running slot, so before this fix
    ``shutdown()`` waited out the *whole* decode; on Linux/offscreen CI the
    worker thread segfaulted mid-decode instead of finishing it.

    This can only pin the *behaviour* -- that a cancelled worker stops
    between frames and delivers no facts -- not the segfault itself: it does
    not reproduce on Windows, so this test cannot prove or disprove it. CI is
    the only real verifier of the crash fix.

    Deterministic rather than timing-dependent: the reader blocks the first
    read on a real ``threading.Event`` and only releases it once the
    controller's cancel token is observed set, so the assertions depend on
    that ordering, not on wall-clock guesses.
    """
    artifact = _seed_cut(tmp_path, "seed-facts-cancel")
    assert artifact.manifest.frame_count >= 2, "need a frame that must never decode"

    store = ReducerStore(_ready_state(tmp_path / "source.mp4"))
    reader = _StallFirstReadReader()
    controller = TransformStageController(store, frame_reader=reader)
    store.dispatch(GlobalTrimChanged(True))
    # A mismatched threshold: the render's cached union metadata must be
    # skipped so _resolve_union actually decodes the stored frames.
    store.dispatch(AlphaThresholdChanged(Decimal("50")))

    controller.open_artifact(artifact)
    assert reader.started.wait(5), "the facts worker never reached the first frame"
    cancel_event = controller._facts_cancel_event  # noqa: SLF001
    assert cancel_event is not None
    worker = controller._worker  # noqa: SLF001
    assert worker is not None
    outcomes: list[str] = []
    worker.succeeded.connect(lambda *_a: outcomes.append("succeeded"))
    worker.failed.connect(lambda *_a: outcomes.append("failed"))

    def _release_once_cancelled() -> None:
        cancel_event.wait(5)
        reader.release.set()

    releaser = threading.Thread(target=_release_once_cancelled)
    releaser.start()

    controller.shutdown()
    releaser.join(timeout=5)

    assert not releaser.is_alive(), "the releaser thread must not hang"
    assert reader.calls == 1, "must not decode the second frame once cancelled"
    qtbot.wait(50)  # pump the event loop so a queued signal would land here
    assert outcomes == [], "a cancelled worker must not report succeeded or failed"
    assert controller.facts is None


class _StallEveryReadReader:
    """Read every stored frame, but block on every call until released --
    this is what lets a supersede be provoked while the superseded worker
    is genuinely mid-decode, deterministically rather than by timing.
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self._inner = _DirectFrameReader()

    def read(self, workspace: CutWorkspace, frame: CutFrame) -> Image.Image:
        self.started.set()
        self.release.wait(5)
        return self._inner.read(workspace, frame)


def test_superseding_the_facts_worker_retires_it_until_its_thread_finishes(
    tmp_path, qtbot
) -> None:
    """Pins the SIGSEGV fix (CI run 33856558458; devbox measurement 9-10/20
    failures with a core dump inside ``QThread::started`` emission): a
    superseded worker/thread pair must stay referenced by the controller
    until the thread genuinely finishes, never dropped at supersede time --
    dropping it before the thread has started (or finished) is what let Qt
    activate ``started`` on an already-collected receiver.

    Deterministic: the reader blocks every read on a real
    ``threading.Event``, so the assertions depend on that ordering, not on
    wall-clock guesses.
    """
    artifact = _seed_cut(tmp_path, "seed-retire")
    store = ReducerStore(_ready_state(tmp_path / "source.mp4"))
    reader = _StallEveryReadReader()
    controller = TransformStageController(store, frame_reader=reader)
    # A mismatched threshold: the render's cached union metadata must be
    # skipped so _resolve_union actually decodes the stored frames.
    store.dispatch(GlobalTrimChanged(True))
    store.dispatch(AlphaThresholdChanged(Decimal("50")))

    controller.open_artifact(artifact)
    assert reader.started.wait(5), "the facts worker never reached the first frame"
    retiring_thread = controller._thread  # noqa: SLF001
    retiring_worker = controller._worker  # noqa: SLF001
    assert retiring_thread is not None
    assert retiring_worker is not None

    controller._schedule_facts()  # noqa: SLF001 -- supersede while still blocked

    assert controller._thread is not retiring_thread  # noqa: SLF001
    assert not retiring_thread.isFinished(), (
        "a superseded thread must stay referenced until it genuinely finishes"
    )
    assert (retiring_thread, retiring_worker) in controller._retiring  # noqa: SLF001

    # A plain busy-wait, not qtbot.wait/waitUntil: spinning the Qt event
    # loop here could process the retired thread's queued `finished` ->
    # `deleteLater`, and isFinished() on an already-deleted QThread raises
    # RuntimeError -- exactly the class of access this fix guards against
    # in production code (see `_thread_is_finished`). Polling without
    # spinning the loop keeps the C++ object alive so this check stays
    # safe while still being a real completion condition, not a fixed sleep.
    reader.release.set()
    deadline = time.monotonic() + 5
    while not retiring_thread.isFinished() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert retiring_thread.isFinished(), "the superseded thread never finished"
    assert (retiring_thread, retiring_worker) in controller._retiring, (  # noqa: SLF001
        "a finished retiree must stay put until something prunes the list"
    )

    controller.shutdown()

    assert controller._retiring == []  # noqa: SLF001
    assert controller._thread is None  # noqa: SLF001
    assert controller._worker is None  # noqa: SLF001


def test_attach_wires_the_canvas_command_requested_to_the_store(qtbot) -> None:
    store = _FakeStore(AppState())
    controller = TransformStageController(store)
    canvas = CropCanvas()
    qtbot.addWidget(canvas)
    group = TransformGroup(lambda _event: None)
    qtbot.addWidget(group)

    controller.attach(group, canvas)
    canvas.command_requested.emit(CropChanged(CropSpec(0, 0, 10, 10)))

    assert store.dispatched == [CropChanged(CropSpec(0, 0, 10, 10))]
    controller.shutdown()


def test_attach_without_a_canvas_leaves_the_group_disabled_until_a_cut_opens(
    qtbot,
) -> None:
    store = _FakeStore(AppState())
    controller = TransformStageController(store)
    group = TransformGroup(lambda _event: None)
    qtbot.addWidget(group)

    controller.attach(group, None)

    assert group._facts is None  # noqa: SLF001
    controller.shutdown()


def test_open_artifact_ignored_leaves_an_existing_session_untouched(
    tmp_path, qtbot
) -> None:
    artifact = _seed_cut(tmp_path, "seed-keep")
    store = _FakeStore(AppState())
    controller = TransformStageController(store)
    controller.open_artifact(artifact)
    qtbot.waitUntil(lambda: controller.session is not None, timeout=5000)

    controller.open_artifact(object())

    assert isinstance(controller.session.workspace, CutWorkspace)
    controller.shutdown()


# -- C3: playhead coupling --------------------------------------------------


def test_use_playhead_requested_dispatches_the_players_current_frame(qtbot) -> None:
    store = _FakeStore(AppState())
    controller = TransformStageController(store)
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    group = TransformGroup(lambda _event: None)
    qtbot.addWidget(group)
    controller.attach(group, canvas)
    canvas._playhead_index = 5  # noqa: SLF001

    controller._use_playhead_requested("last")  # noqa: SLF001

    assert store.dispatched == [TransformChanged(TransformSpec(last_frame=5))]
    controller.shutdown()


def test_use_playhead_requested_without_a_player_frame_does_nothing(qtbot) -> None:
    store = _FakeStore(AppState())
    controller = TransformStageController(store)
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    group = TransformGroup(lambda _event: None)
    qtbot.addWidget(group)
    controller.attach(group, canvas)

    controller._use_playhead_requested("first")  # noqa: SLF001

    assert store.dispatched == []
    controller.shutdown()


# -- E24: a stale frame-load generation is discarded ------------------------


def test_frame_load_succeeded_ignores_a_stale_generation(qtbot) -> None:
    store = _FakeStore(AppState())
    controller = TransformStageController(store)
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    group = TransformGroup(lambda _event: None)
    qtbot.addWidget(group)
    controller.attach(group, canvas)
    controller._frame_generation = 5  # noqa: SLF001

    stale = PlayerFrames(
        key="stale",
        framed=(),
        transformed=(),
        delays_ms=(),
        cached=0,
        frame_count=0,
    )
    controller._frame_load_succeeded(stale, 3)  # noqa: SLF001

    assert canvas._frames is None  # noqa: SLF001
    controller.shutdown()


def test_cancelling_a_frame_load_bumps_the_generation_so_a_queued_result_is_stale(
    tmp_path, qtbot
) -> None:
    """``_cancel_frame_load`` used to leave ``_frame_generation`` untouched, so
    a ``succeeded`` from the worker it just cancelled -- already queued on the
    Qt event loop, or slipping past the cancellation check by a hair -- still
    matched and reopened a session that ``close_session`` had just closed.
    """
    artifact = _seed_cut(tmp_path, "cancel-bumps-generation")
    store = _FakeStore(AppState())
    controller = TransformStageController(store)
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    group = TransformGroup(lambda _event: None)
    qtbot.addWidget(group)
    controller.attach(group, canvas)
    controller.open_artifact(artifact)
    qtbot.waitUntil(lambda: canvas.current_frame is not None, timeout=5000)
    in_flight_generation = controller._frame_generation  # noqa: SLF001

    controller.close_session()

    stale = PlayerFrames(
        key="stale", framed=(), transformed=(), delays_ms=(), cached=0, frame_count=0
    )
    controller._frame_load_succeeded(  # noqa: SLF001
        stale, in_flight_generation
    )

    assert canvas._frames is None, (  # noqa: SLF001
        "a load in flight when the session closed must not resurrect frames"
    )
    controller.shutdown()


class _BlockingReader:
    """Read every frame, but block until released -- so a load superseded
    mid-flight would hang the caller if the supersede itself waited on it.
    """

    def __init__(self) -> None:
        self.first_read_started = threading.Event()
        self.release = threading.Event()
        self._inner = _DirectFrameReader()

    def read(self, workspace: CutWorkspace, frame: CutFrame):
        self.first_read_started.set()
        self.release.wait(5)
        return self._inner.read(workspace, frame)


def test_superseding_a_frame_load_does_not_block_the_gui_thread(
    tmp_path, qtbot
) -> None:
    """``_cancel_frame_load`` did ``thread.quit(); thread.wait(5000)`` on
    every supersede, but the worker checked no cancellation token, so
    ``quit()`` could not interrupt its running slot -- the caller (the GUI
    thread, on every ordinary crop edit) blocked for the rest of the
    in-flight load.
    """
    artifact = _seed_cut(tmp_path, "supersede-non-blocking")
    reader = _BlockingReader()
    store = ReducerStore(_ready_state(tmp_path / "source.mp4"))
    controller = TransformStageController(store, frame_reader=reader)
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    group = TransformGroup(lambda _event: None)
    qtbot.addWidget(group)
    controller.attach(group, canvas)

    controller.open_artifact(artifact)
    qtbot.waitUntil(lambda: controller.facts is not None, timeout=5000)
    qtbot.waitUntil(reader.first_read_started.is_set, timeout=5000)

    started = time.monotonic()
    controller._start_frame_load()  # noqa: SLF001 -- supersedes the blocked load
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "superseding a load must not wait on the in-flight worker"
    reader.release.set()
    controller.shutdown()


def test_trim_only_edit_does_not_reload_frames_or_lose_the_kept_range(
    tmp_path, qtbot
) -> None:
    """A trim-only edit (first_frame/last_frame) was applied to the result
    loop via ``set_kept_range`` and then wiped by ``_state_changed``
    scheduling a needless ``_sync_player_frames`` reload -- ``set_frames``
    resets ``canvas._kept`` to ``None`` (the full range) once that reload's
    result lands, even though ``apply_transform`` only crops/resizes and the
    rebuilt cache would be byte-identical to the one it replaced.
    """
    artifact = _seed_cut(tmp_path, "trim-only")
    reader = _CountingReader()
    store = ReducerStore(_ready_state(tmp_path / "source.mp4"))
    controller = TransformStageController(store, frame_reader=reader)
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    group = TransformGroup(lambda _event: None)
    qtbot.addWidget(group)
    controller.attach(group, canvas)

    controller.open_artifact(artifact)
    qtbot.waitUntil(lambda: canvas.current_frame is not None, timeout=5000)
    frame_count = artifact.manifest.frame_count
    assert frame_count >= 2, "need room to trim off the first frame"
    initial_reads = reader.calls

    store.dispatch(TransformChanged(TransformSpec(first_frame=1)))

    assert canvas._kept == range(1, frame_count)  # noqa: SLF001
    assert canvas.current_frame == 1

    qtbot.wait(500)  # long enough for the 250ms debounce to fire if scheduled

    assert reader.calls == initial_reads, "a trim-only edit must never reload the cache"
    assert canvas._kept == range(1, frame_count), (  # noqa: SLF001
        "the trim must survive even if something still reloads the cache"
    )
    assert canvas.current_frame == 1
    controller.shutdown()


# -- E33: crop-edit drags are debounced and skipped while editing -----------


class _CountingReader:
    """Wrap ``_DirectFrameReader`` to count how many stored frames were read."""

    def __init__(self) -> None:
        self.calls = 0
        self._inner = _DirectFrameReader()

    def read(self, workspace: CutWorkspace, frame: CutFrame):
        self.calls += 1
        return self._inner.read(workspace, frame)


def test_crop_edits_are_debounced_and_reuse_the_framed_cache(tmp_path, qtbot) -> None:
    artifact = _seed_cut(tmp_path, "debounce")
    reader = _CountingReader()
    store = ReducerStore(_ready_state(tmp_path / "source.mp4"))
    controller = TransformStageController(store, frame_reader=reader)
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    group = TransformGroup(lambda _event: None)
    qtbot.addWidget(group)
    controller.attach(group, canvas)

    controller.open_artifact(artifact)
    qtbot.waitUntil(lambda: canvas.current_frame is not None, timeout=5000)
    frame_count = artifact.manifest.frame_count
    initial_reads = reader.calls
    assert initial_reads == frame_count
    first_key = canvas._frames.key  # noqa: SLF001

    controller._crop_edit_toggled(True)  # noqa: SLF001
    for index in range(10):
        crop = CropSpec(0, 0, 100, 100 - index)
        store.dispatch(TransformChanged(TransformSpec(crop=crop)))
    qtbot.wait(60)
    assert reader.calls == initial_reads, "no rebuild while crop-edit is on"

    controller._crop_edit_toggled(False)  # noqa: SLF001
    qtbot.waitUntil(
        lambda: reader.calls == initial_reads + frame_count, timeout=2000
    )

    reloaded_key = canvas._frames.key  # noqa: SLF001
    assert (reloaded_key[0], reloaded_key[1], reloaded_key[4]) == (
        first_key[0],
        first_key[1],
        first_key[4],
    ), "the framed cache's identity (cache_key, framing, display size) is unchanged"
    controller.shutdown()


# -- crop-edit presentation must stay live across the whole gesture --------


def test_state_changed_refreshes_the_crop_edit_presentation(tmp_path, qtbot) -> None:
    """`apply_presentation` in ``result_player.py`` is reached only via
    ``set_crop_edit``, called only from ``_crop_edit_toggled`` -- so once
    editing is on, ``canvas._presentation`` stayed at its toggle-time crop
    forever. That is what made every nudge after the first a no-op (nudge_crop
    kept starting from the same stale crop) and made a drag rectangle never
    move on screen: the canvas painted `_geometry`, which is only rebuilt
    inside `apply_presentation`.
    """
    artifact = _seed_cut(tmp_path, "presentation-refresh")
    store = ReducerStore(_ready_state(tmp_path / "source.mp4"))
    controller = TransformStageController(store)
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    group = TransformGroup(lambda _event: None)
    qtbot.addWidget(group)
    controller.attach(group, canvas)
    controller.open_artifact(artifact)
    qtbot.waitUntil(lambda: canvas.current_frame is not None, timeout=5000)
    controller._crop_edit_toggled(True)  # noqa: SLF001
    assert canvas._presentation is not None  # noqa: SLF001
    assert canvas._presentation.crop == CropSpec(0, 0, 128, 128)  # noqa: SLF001

    new_crop = CropSpec(10, 10, 50, 50)
    store.dispatch(TransformChanged(TransformSpec(crop=new_crop)))

    assert canvas._presentation is not None  # noqa: SLF001
    assert canvas._presentation.crop == new_crop, (  # noqa: SLF001
        "the canvas's crop-edit presentation must track every transform "
        "change while editing, not just the crop it had when editing began"
    )
    controller.shutdown()


def test_crop_edit_overlay_follows_a_framed_size_change(tmp_path, qtbot) -> None:
    """``_facts_ready`` clamps the stored crop and stores the new
    ``CutFacts``, but the only overlay refresh path,
    ``_refresh_crop_edit_presentation``, otherwise only ran from
    ``_state_changed`` reacting to the clamp's own ``TransformChanged`` --
    at which point ``self._facts`` was still the OLD facts. A framing change
    that does not need to clamp anything (crop already fits) never refreshed
    the overlay at all, leaving it framed against a stale source size.
    """
    artifact = _seed_cut(tmp_path, "framed-size-change")
    store = ReducerStore(_ready_state(tmp_path / "source.mp4"))
    controller = TransformStageController(store)
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    group = TransformGroup(lambda _event: None)
    qtbot.addWidget(group)
    controller.attach(group, canvas)
    controller.open_artifact(artifact)
    qtbot.waitUntil(lambda: canvas.current_frame is not None, timeout=5000)
    original_size = controller.facts.framed_size

    controller._crop_edit_toggled(True)  # noqa: SLF001
    assert canvas._presentation is not None  # noqa: SLF001
    assert (canvas._presentation.width, canvas._presentation.height) == original_size  # noqa: SLF001

    store.dispatch(PaddingChanged(10))
    qtbot.waitUntil(
        lambda: controller.facts is not None
        and controller.facts.framed_size != original_size,
        timeout=5000,
    )
    new_size = controller.facts.framed_size

    assert canvas._presentation is not None  # noqa: SLF001
    assert (canvas._presentation.width, canvas._presentation.height) == new_size, (  # noqa: SLF001
        "the crop-edit overlay must follow the new framed size, not the one "
        "it had when editing began"
    )
    assert canvas._presentation.crop == CropSpec(0, 0, *new_size)  # noqa: SLF001
    controller.shutdown()


def test_repeated_keyboard_nudges_are_cumulative_while_crop_editing(
    tmp_path, qtbot
) -> None:
    """End-to-end version of the same defect: five arrow presses must move
    the crop five source pixels, not emit the same one-pixel move five times.
    """
    artifact = _seed_cut(tmp_path, "cumulative-nudge")
    store = ReducerStore(_ready_state(tmp_path / "source.mp4"))
    controller = TransformStageController(store)
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    group = TransformGroup(lambda _event: None)
    qtbot.addWidget(group)
    controller.attach(group, canvas)
    controller.open_artifact(artifact)
    qtbot.waitUntil(lambda: canvas.current_frame is not None, timeout=5000)
    store.dispatch(TransformChanged(TransformSpec(crop=CropSpec(50, 50, 20, 20))))
    controller._crop_edit_toggled(True)  # noqa: SLF001
    canvas.resize(200, 200)
    canvas.show()
    canvas.activateWindow()
    canvas.setFocus()
    qtbot.waitUntil(canvas.hasFocus, timeout=1000)

    for _ in range(5):
        qtbot.keyClick(canvas, Qt.Key.Key_Left)

    final_crop = store.state.parameters.transform.crop
    assert final_crop == CropSpec(45, 50, 20, 20)
    controller.shutdown()


def test_attach_wires_the_players_playhead_to_the_groups_use_playhead_buttons(
    qtbot,
) -> None:
    store = _FakeStore(AppState())
    controller = TransformStageController(store)
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    group = TransformGroup(lambda _event: None)
    qtbot.addWidget(group)

    controller.attach(group, canvas)
    canvas.playhead_changed.emit(7)

    assert group._playhead == 7  # noqa: SLF001
    controller.shutdown()


# -- a cut session must not outlive the source it belongs to ---------------


def test_loading_a_different_source_closes_the_open_cut_session(
    tmp_path, qtbot
) -> None:
    """Trigger: a cut open for a 440x444 clip, a transform set, then a
    1920x1080 file loaded. ``SourceLoadRequested`` resets the transform but
    left ``_session``/``_facts`` in place, so the reset read as an ordinary
    edit: it reloaded frames from the previous cut, the Transform controls
    stayed live on the previous cut's framed size, and a render started in
    that state would have cut a 440x444 corner out of the new video --
    ``clamp_crop`` only intervenes when the crop no longer fits.
    """
    artifact = _seed_cut(tmp_path, "source-swap")
    reader = _CountingReader()
    store = ReducerStore(_ready_state(tmp_path / "source.mp4"))
    controller = TransformStageController(store, frame_reader=reader)
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    group = TransformGroup(lambda _event: None)
    qtbot.addWidget(group)
    controller.attach(group, canvas)

    controller.open_artifact(artifact)
    qtbot.waitUntil(lambda: canvas.current_frame is not None, timeout=5000)
    frame_count = artifact.manifest.frame_count
    store.dispatch(TransformChanged(TransformSpec(crop=CropSpec(0, 0, 64, 64))))
    qtbot.waitUntil(lambda: reader.calls >= 2 * frame_count, timeout=5000)
    reads_before = reader.calls

    store.dispatch(SourceLoadRequested("other-source", "load-2"))

    assert controller.session is None
    assert controller.facts is None
    assert canvas.current_frame is None
    qtbot.wait(500)  # longer than the 250ms crop-edit debounce
    assert reader.calls == reads_before, (
        "no frame may be read from a cut whose source is no longer loaded"
    )
    controller.shutdown()


def test_reloading_the_same_source_leaves_the_open_cut_session_intact(
    tmp_path, qtbot
) -> None:
    """The source identity decides, not the event: a state change that keeps
    ``source_id`` must leave a healthy session alone. (The shell mints a fresh
    id per file dialog, so re-opening the same file does close the session.)
    """
    artifact = _seed_cut(tmp_path, "same-source")
    store = ReducerStore(_ready_state(tmp_path / "source.mp4"))
    controller = TransformStageController(store)

    controller.open_artifact(artifact)
    qtbot.waitUntil(lambda: controller.facts is not None, timeout=5000)
    facts = controller.facts

    store.dispatch(SourceLoadRequested("source", "load-2"))

    assert controller.session is not None
    assert controller.facts == facts
    controller.shutdown()
