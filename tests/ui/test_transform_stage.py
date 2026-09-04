"""The controller owning the currently open cut and its derived facts (D5)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from matteloop.core.crop_state import CropChanged
from matteloop.core.parameters import PaddingChanged, TransformChanged
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
