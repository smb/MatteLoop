"""Restoring a matched cut's stored transform when Rebuild is chosen.

The reuse request is assembled before the workspace probe ever looks at the
cut, so on a cold start it carries an identity transform. Rebuilding with it
dropped the stored trim/crop/resize and then deleted the sidecar holding
them, because ``store_transform`` records nothing for an identity result.

These live in their own module rather than in ``test_render_controller.py``:
adding to that module's import and collection scope reproducibly turns a
latent shutdown crash there (``Windows fatal exception: access violation``,
present at HEAD at roughly 1 run in 8) into a near-certain one.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from matteloop.core.parameters import TransformChanged
from matteloop.core.specs import CropSpec, TransformSpec
from matteloop.core.state import (
    AppState,
    PreviewRequested,
    PreviewResult,
    PreviewSucceeded,
    SourceLoaded,
    SourceLoadRequested,
    reduce,
)
from matteloop.jobs.transform_store import store_transform, transform_sidecar_path
from matteloop.jobs.workspace import CutWorkspace, WorkspaceLifecycle
from matteloop.ui.controller import SourceController
from matteloop.ui.ports import RenderVideoRequested
from matteloop.ui.preview_controller import PreviewRuntime
from matteloop.ui.store import ReducerStore


@dataclass(frozen=True)
class _Metadata:
    path: Path
    width: int = 128
    height: int = 128
    duration: Fraction = Fraction(2)
    average_rate: Fraction = Fraction(30)


class _MatchedCutRuntime(PreviewRuntime):
    """Match one promoted cut set and answer the Rebuild the dialog offers.

    Only the rebuild path is reached: ``RenderWorker`` skips model
    preparation for a rebuild, so ``prepare``, ``preview`` and ``render``
    stand as assertions that nothing strayed off it.
    """

    default_model_id = "birefnet-portrait"

    def __init__(self, workspace: CutWorkspace) -> None:
        self.workspace = workspace
        self.rebuild_requests: list[object] = []

    def prepare(self, model_id, extras, context):
        raise AssertionError("a rebuild must not prepare a model session")

    def preview(self, request, playhead, context):
        raise AssertionError("these tests never preview")

    def render(self, request, context):
        raise AssertionError("these tests always rebuild")

    def find_matching_workspace(self, request, context):
        del request, context
        return self.workspace

    def rebuild(self, request, workspace, context):
        del workspace, context
        self.rebuild_requests.append(request)
        return type("Artifact", (), {"output_path": request.output.path})()

    def close(self) -> None:
        return


def _previewed_state(path: Path) -> AppState:
    """READY with a current preview: ``can_render`` is on and Render goes
    straight to the workspace probe instead of the preflight prompt."""
    loading = reduce(AppState(), SourceLoadRequested("source", "load"))
    ready = reduce(loading, SourceLoaded("source", "load", _Metadata(path)))
    running = reduce(ready, PreviewRequested("preview", "preview-request"))
    return reduce(
        running,
        PreviewSucceeded(
            "preview", PreviewResult("source", "preview-request", QImage())
        ),
    )


def _promoted_cut(tmp_path: Path, key: str) -> CutWorkspace:
    """A promoted cut set as a restarted application would find it.

    The transform sidecar is addressed by ``cuts_root`` and ``cache_key``
    alone, so no cut frames or manifest are needed to store and restore one.
    """
    cuts_root = tmp_path / ".matteloop-work" / "cuts"
    cuts_root.mkdir(parents=True, exist_ok=True)
    return CutWorkspace(
        tmp_path,
        tmp_path / ".matteloop-work",
        cuts_root,
        tmp_path / ".matteloop-work" / "scratch",
        key * 64,
        cuts_root / f"source-{key * 8}",
        WorkspaceLifecycle.PROMOTED,
        None,
        f"source-{key * 8}",
    )


def _controller(tmp_path: Path, runtime: _MatchedCutRuntime):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    store = ReducerStore(_previewed_state(source))
    return store, SourceController(store, preview_runtime=runtime)


def _rebuild_from_reuse_dialog(controller: SourceController, qtbot) -> None:
    """Answer the "Matching cut set found" prompt with Rebuild."""
    qtbot.waitUntil(
        lambda: controller.render_controller.reuse_dialog is not None,
        timeout=5000,
    )
    dialog = controller.render_controller.reuse_dialog
    assert dialog is not None
    assert dialog.buttons()[0].text() == "Rebuild"
    qtbot.mouseClick(dialog.buttons()[0], Qt.MouseButton.LeftButton)


def test_rebuilding_a_matched_cut_restores_its_stored_transform(
    tmp_path, qtbot
) -> None:
    """A cold start holds no transform, so the sidecar is the only record of
    the trim/crop/resize the last render applied.
    """
    workspace = _promoted_cut(tmp_path, "a")
    stored = TransformSpec(first_frame=1, crop=CropSpec(8, 8, 64, 64))
    store_transform(workspace, stored, [])
    runtime = _MatchedCutRuntime(workspace)
    _store, controller = _controller(tmp_path, runtime)

    controller.dispatch(RenderVideoRequested())
    _rebuild_from_reuse_dialog(controller, qtbot)

    qtbot.waitUntil(lambda: len(runtime.rebuild_requests) == 1, timeout=5000)
    assert runtime.rebuild_requests[0].transform == stored
    controller.shutdown()


def test_rebuilding_the_open_cut_keeps_unsaved_inspector_edits(
    tmp_path, qtbot
) -> None:
    """The stored transform must never overwrite an edit the user has made to
    the cut currently on screen -- the same loss in the other direction.
    """
    workspace = _promoted_cut(tmp_path, "a")
    store_transform(workspace, TransformSpec(first_frame=1), [])
    runtime = _MatchedCutRuntime(workspace)
    store, controller = _controller(tmp_path, runtime)
    controller.render_controller.open_cut_key = lambda: workspace.cache_key
    live = TransformSpec(crop=CropSpec(4, 4, 32, 32))
    store.dispatch(TransformChanged(live))

    controller.dispatch(RenderVideoRequested())
    _rebuild_from_reuse_dialog(controller, qtbot)

    qtbot.waitUntil(lambda: len(runtime.rebuild_requests) == 1, timeout=5000)
    assert runtime.rebuild_requests[0].transform == live
    controller.shutdown()


def test_rebuilding_a_different_cut_restores_that_cuts_stored_transform(
    tmp_path, qtbot
) -> None:
    """Keeping live edits is scoped to the open cut: a match on some other
    cut set restores that set's sidecar, so the rebuild cannot overwrite it
    with a transform belonging to the cut on screen.
    """
    opened = _promoted_cut(tmp_path, "a")
    matched = _promoted_cut(tmp_path, "b")
    stored = TransformSpec(first_frame=2)
    store_transform(matched, stored, [])
    runtime = _MatchedCutRuntime(matched)
    store, controller = _controller(tmp_path, runtime)
    controller.render_controller.open_cut_key = lambda: opened.cache_key
    store.dispatch(TransformChanged(TransformSpec(crop=CropSpec(4, 4, 32, 32))))

    controller.dispatch(RenderVideoRequested())
    _rebuild_from_reuse_dialog(controller, qtbot)

    qtbot.waitUntil(lambda: len(runtime.rebuild_requests) == 1, timeout=5000)
    assert runtime.rebuild_requests[0].transform == stored
    controller.shutdown()


def test_rebuilding_a_cut_without_a_stored_transform_stays_identity(
    tmp_path, qtbot
) -> None:
    workspace = _promoted_cut(tmp_path, "a")
    assert not transform_sidecar_path(workspace).exists()
    runtime = _MatchedCutRuntime(workspace)
    _store, controller = _controller(tmp_path, runtime)

    controller.dispatch(RenderVideoRequested())
    _rebuild_from_reuse_dialog(controller, qtbot)

    qtbot.waitUntil(lambda: len(runtime.rebuild_requests) == 1, timeout=5000)
    assert runtime.rebuild_requests[0].transform == TransformSpec()
    controller.shutdown()
