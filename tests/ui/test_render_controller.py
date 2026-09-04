from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from threading import Event, get_ident

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices, QImage

from matteloop.core.crop_state import CropChanged
from matteloop.core.parameters import TransformChanged
from matteloop.core.specs import CropSpec, TransformSpec
from matteloop.core.state import (
    AppState,
    ArtifactState,
    JobKind,
    JobStageChanged,
    PreviewRequested,
    PreviewResult,
    PreviewState,
    PreviewSucceeded,
    RenderPreflightDismissed,
    RenderPreflightRequested,
    SourceLoaded,
    SourceLoadRequested,
    reduce,
)
from matteloop.core.timeline import EndChanged, StartChanged
from matteloop.core.webp import validate_webp
from matteloop.jobs.context import CancellationState, JobContext, ProgressEvent
from matteloop.jobs.render import ImmutableRgba, PreparedSegmentation, RenderArtifact
from matteloop.jobs.workspace import CutWorkspace, WorkspaceLifecycle, WorkspaceSummary
from matteloop.ui.controller import SourceController
from matteloop.ui.ports import (
    OpenOutputFolderRequested,
    OpenOutputRequested,
    RenderVideoRequested,
)
from matteloop.ui.preview_controller import PreviewRuntime
from matteloop.ui.render_pipeline import _StageReporter, render_prepared
from matteloop.ui.store import ReducerStore
from tests.fixtures.media_factory import make_video


@dataclass(frozen=True)
class Metadata:
    path: Path
    width: int = 128
    height: int = 128
    duration: Fraction = Fraction(2)
    average_rate: Fraction = Fraction(30)


class FakeSegmenter:
    def segment(self, frame, request):
        del request
        return frame


def test_stage_change_preserves_current_frame_and_overall_counts(tmp_path) -> None:
    events: list[ProgressEvent] = []
    context = JobContext(
        "stage-context",
        JobKind.RENDER,
        tmp_path,
        events.append,
        CancellationState(),
    )
    context.set_frame_context(12, 39, overall=(11, 78))

    _StageReporter(context).report("Segmentation")

    assert events[-1] == ProgressEvent(
        "stage-context",
        "Segmentation",
        12,
        39,
        "Frame 12 of 39",
        11,
        78,
    )


def test_stage_change_preserves_the_last_published_frame_event(tmp_path) -> None:
    events: list[ProgressEvent] = []
    context = JobContext(
        "published-frame",
        JobKind.RENDER,
        tmp_path,
        events.append,
        CancellationState(),
    )
    context.progress(
        "render-cut",
        12,
        total=39,
        detail="Cut frame 12 of 39",
        overall_completed=12,
        overall_total=78,
    )

    _StageReporter(context).report("Segmentation")

    assert events[-1] == ProgressEvent(
        "published-frame",
        "Segmentation",
        12,
        39,
        "Cut frame 12 of 39",
        12,
        78,
    )


class FakeRenderRuntime(PreviewRuntime):
    default_model_id = "birefnet-portrait"

    def __init__(self) -> None:
        self.render_requests = []
        self.render_thread_id: int | None = None
        self.prepare_count = 0

    def prepare(self, model_id, extras, context):
        del extras, context
        self.prepare_count += 1
        return PreparedSegmentation(
            FakeSegmenter(), model_id, "ab" * 32, "2.0.75", frozenset({"standard"})
        )

    def preview(self, request, playhead, context):
        del request, playhead
        context.progress("Segmentation", 0)
        image = Image.new("RGBA", (128, 128), (10, 20, 30, 255))
        return type(
            "Preview",
            (),
            {"display_rgba": ImmutableRgba(128, 128, image.tobytes())},
        )()

    def render(self, request, context) -> RenderArtifact:
        self.render_requests.append(request)
        self.render_thread_id = get_ident()
        for stage in ("Decode", "Segmentation", "Post-process", "Encode", "Validate"):
            context.progress(stage, 0)
        return type("Artifact", (), {"output_path": request.output.path})()

    def close(self) -> None:
        return


class DetailedRenderRuntime(FakeRenderRuntime):
    active_provider = "CoreMLExecutionProvider"

    def render(self, request, context) -> RenderArtifact:
        self.render_requests.append(request)
        return type(
            "Artifact",
            (),
            {
                "output_path": request.output.path,
                "frame_count": 7,
                "width": 640,
                "height": 360,
                "file_size": 2 * 1024**2,
                "duration_ms": 2500,
                "rebuilt": False,
            },
        )()


class BlockingRenderRuntime(FakeRenderRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def render(self, request, context) -> RenderArtifact:
        self.started.set()
        while not self.release.wait(0.01):
            context.checkpoint("render")
        return super().render(request, context)


class FailingRenderRuntime(FakeRenderRuntime):
    def render(self, request, context) -> RenderArtifact:
        del request, context
        raise RuntimeError("encoder failed")


class ServiceRenderRuntime(FakeRenderRuntime):
    def render(self, request, context) -> RenderArtifact:
        self.render_requests.append(request)
        self.render_thread_id = get_ident()
        return render_prepared(
            PreparedSegmentation(
                FakeSegmenter(),
                "birefnet-portrait",
                "ab" * 32,
                "2.0.75",
                frozenset({"standard"}),
            ),
            request,
            context,
        )


class MatchingCutsRuntime(FakeRenderRuntime):
    def __init__(self, workspace: CutWorkspace) -> None:
        super().__init__()
        self.workspace = workspace

    def find_matching_workspace(self, request, context):
        del request, context
        return self.workspace


class RebuildRuntime(FakeRenderRuntime):
    """Answers ``rebuild`` the way a matched-cut path requires."""

    def rebuild(self, request, workspace, context):
        del workspace, context
        self.render_requests.append(request)
        return type("Artifact", (), {"output_path": request.output.path})()


@dataclass(frozen=True)
class _RebuildManifest:
    """Duck-typed stand-in exposing only what request_for_workspace reads."""

    cache_key_inputs: dict[str, object]
    source_path: str


def _rebuild_manifest(source: Path) -> _RebuildManifest:
    return _RebuildManifest(
        cache_key_inputs={
            "sampling": {
                "start": {"numerator": 0, "denominator": 1},
                "end": {"numerator": 2, "denominator": 1},
                "fps": 15,
            },
            "crop": {"x": 0, "y": 0, "width": 128, "height": 128},
            "model": {"id": "u2net"},
            "edge_settings": {
                "mode": "standard",
                "alpha_matting": {
                    "foreground_threshold": 240,
                    "background_threshold": 10,
                    "erode_size": 10,
                },
            },
        },
        source_path=str(source),
    )


class RecordingStore(ReducerStore):
    def __init__(self, state) -> None:
        super().__init__(state)
        self.events: list[object] = []

    def dispatch(self, event) -> None:
        self.events.append(event)
        super().dispatch(event)


def _ready_state(path: Path):
    loading = reduce(AppState(), SourceLoadRequested("source", "load"))
    return reduce(loading, SourceLoaded("source", "load", Metadata(path)))


def _current_state(path: Path):
    ready = _ready_state(path)
    running = reduce(ready, PreviewRequested("preview", "preview-request"))
    return reduce(
        running,
        PreviewSucceeded(
            "preview", PreviewResult("source", "preview-request", QImage())
        ),
    )


def test_render_command_writes_default_request_off_gui_thread(tmp_path, qtbot) -> None:
    source = tmp_path / "holiday clip.mp4"
    source.write_bytes(b"fixture")
    runtime = FakeRenderRuntime()
    store = RecordingStore(_current_state(source))
    controller = SourceController(store, preview_runtime=runtime)

    controller.dispatch(RenderVideoRequested())

    qtbot.waitUntil(lambda: store.state.artifact is ArtifactState.VALID, timeout=5000)
    request = runtime.render_requests[0]
    assert runtime.render_thread_id != get_ident()
    assert request.source == source
    assert request.sampling.start == Fraction(0)
    assert request.sampling.end == Fraction(2)
    assert request.sampling.fps == 15
    assert (
        request.crop.x,
        request.crop.y,
        request.crop.width,
        request.crop.height,
    ) == (0, 0, 128, 128)
    assert request.segmentation.model_id == "birefnet-portrait"
    assert request.framing.trim is False
    assert request.framing.padding == 0
    assert request.framing.stretch_x == 1
    assert request.output.path == tmp_path / "holiday clip.webp"
    assert store.state.artifact_result is not None
    assert store.state.artifact_result.output_path == request.output.path
    assert [
        event.stage for event in store.events if isinstance(event, JobStageChanged)
    ] == ["Decode", "Segmentation", "Post-process", "Encode", "Validate"]
    controller.shutdown()


def test_successful_render_keeps_dialog_open_with_the_artifact_summary(
    tmp_path, qtbot
) -> None:
    source = tmp_path / "holiday clip.mp4"
    source.write_bytes(b"fixture")
    runtime = DetailedRenderRuntime()
    store = RecordingStore(_current_state(source))
    controller = SourceController(store, preview_runtime=runtime)

    controller.dispatch(RenderVideoRequested())

    qtbot.waitUntil(
        lambda: (
            controller.render_controller.dialog is not None
            and controller.render_controller.dialog.completion_visible
        ),
        timeout=5000,
    )
    dialog = controller.render_controller.dialog
    result = store.state.artifact_result
    assert dialog is not None
    assert result is not None
    assert dialog.isVisible()
    assert [
        button.text()
        for button in (
            dialog.open_output_button,
            dialog.open_folder_button,
            dialog.close_button,
        )
    ] == ["Open output", "Open folder", "Close"]
    assert dialog.completion_actions.isVisible()
    assert dialog.stage_label.text() == "Complete"
    assert dialog.output_label.text() == "Output: holiday clip.webp"
    assert dialog.output_label.toolTip() == str(source.with_suffix(".webp"))
    assert dialog.output_label.accessibleDescription() == str(
        source.with_suffix(".webp")
    )
    assert dialog.completion_dimensions.text() == "640 × 360"
    assert dialog.completion_frames.text() == "7 frames"
    assert dialog.completion_duration.text() == "0:02.500"
    assert dialog.completion_size.text() == "2.0 MiB"
    assert dialog.completion_fps.text() == "15 fps"
    assert dialog.completion_cuts.text() == "Fresh segmentation"
    assert dialog.model_provider_label.text() == "BiRefNet Portrait · Core ML"
    assert dialog.completion_job_time.text() != "—"
    assert result.frame_count == 7
    assert result.width == 640
    assert result.height == 360
    assert result.file_size == 2 * 1024**2
    assert result.duration_ms == 2500
    assert result.output_fps == 15
    assert result.model_id == "birefnet-portrait"
    assert result.execution_provider == "CoreMLExecutionProvider"
    assert result.cuts_reused is False
    assert result.job_duration_ms is not None
    assert dialog.close_button.hasFocus()
    qtbot.mouseClick(dialog.close_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not dialog.isVisible(), timeout=1000)
    controller.shutdown()


def test_cancelled_render_closes_without_showing_completion_summary(
    tmp_path, qtbot
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    runtime = BlockingRenderRuntime()
    store = RecordingStore(_current_state(source))
    controller = SourceController(store, preview_runtime=runtime)

    controller.dispatch(RenderVideoRequested())
    qtbot.waitUntil(runtime.started.is_set, timeout=5000)
    dialog = controller.render_controller.dialog
    assert dialog is not None
    qtbot.mouseClick(dialog.cancel_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(
        lambda: store.state.job.phase.value == "idle" and not dialog.isVisible(),
        timeout=5000,
    )
    assert not dialog.completion_visible
    controller.shutdown()


def test_failed_render_closes_without_showing_completion_summary(
    tmp_path, qtbot
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    runtime = FailingRenderRuntime()
    store = RecordingStore(_current_state(source))
    controller = SourceController(store, preview_runtime=runtime)

    controller.dispatch(RenderVideoRequested())
    dialog = controller.render_controller.dialog
    assert dialog is not None
    qtbot.waitUntil(
        lambda: store.state.job.phase.value == "idle" and not dialog.isVisible(),
        timeout=5000,
    )

    assert not dialog.completion_visible
    assert store.state.artifact_result is None
    controller.shutdown()


def test_render_command_uses_the_selected_export_range(tmp_path, qtbot) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    runtime = FakeRenderRuntime()
    selected = reduce(_ready_state(source), StartChanged(Fraction(1, 2)))
    selected = reduce(selected, EndChanged(Fraction(3, 2)))
    selected = reduce(selected, PreviewRequested("preview", "preview-request"))
    selected = reduce(
        selected,
        PreviewSucceeded(
            "preview", PreviewResult("source", "preview-request", QImage())
        ),
    )
    store = RecordingStore(selected)
    controller = SourceController(store, preview_runtime=runtime)

    controller.dispatch(RenderVideoRequested())

    qtbot.waitUntil(lambda: store.state.artifact is ArtifactState.VALID, timeout=5000)
    request = runtime.render_requests[0]
    assert request.sampling.start == Fraction(1, 2)
    assert request.sampling.end == Fraction(3, 2)
    controller.shutdown()


def test_render_command_uses_the_selected_oriented_crop(tmp_path, qtbot) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    runtime = FakeRenderRuntime()
    selected = reduce(_ready_state(source), CropChanged(CropSpec(8, 12, 96, 80)))
    selected = reduce(selected, PreviewRequested("preview", "preview-request"))
    selected = reduce(
        selected,
        PreviewSucceeded(
            "preview", PreviewResult("source", "preview-request", QImage())
        ),
    )
    store = RecordingStore(selected)
    controller = SourceController(store, preview_runtime=runtime)

    controller.dispatch(RenderVideoRequested())

    qtbot.waitUntil(lambda: store.state.artifact is ArtifactState.VALID, timeout=5000)
    assert runtime.render_requests[0].crop == CropSpec(8, 12, 96, 80)
    controller.shutdown()


def test_render_command_publishes_a_lossless_animated_webp(tmp_path, qtbot) -> None:
    source = tmp_path / "source.mp4"
    make_video(
        source,
        [
            Image.new(
                "RGB",
                (128, 128),
                (index * 7 % 256, index * 11 % 256, index * 13 % 256),
            )
            for index in range(30)
        ],
        Fraction(15),
    )
    runtime = ServiceRenderRuntime()
    store = RecordingStore(_current_state(source))
    controller = SourceController(store, preview_runtime=runtime)

    controller.dispatch(RenderVideoRequested())

    qtbot.waitUntil(lambda: store.state.job.phase.value == "idle", timeout=10000)
    assert store.state.artifact_error is None
    output = source.with_suffix(".webp")
    assert output.is_file()
    info = validate_webp(output, expected_frames=30, expected_duration_ms=2000)
    assert info.lossless
    assert info.has_alpha
    controller.shutdown()


def test_render_without_preview_defaults_to_preview_first(tmp_path, qtbot) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    runtime = FakeRenderRuntime()
    store = RecordingStore(_ready_state(source))
    controller = SourceController(store, preview_runtime=runtime)

    controller.dispatch(RenderVideoRequested())
    dialog = controller.render_controller.preflight_dialog
    assert dialog is not None
    assert dialog.defaultButton().text() == "Preview first"
    assert [button.text() for button in dialog.buttons()] == [
        "Preview first",
        "Render anyway",
        "Cancel",
    ]
    assert (
        sum(isinstance(event, RenderPreflightRequested) for event in store.events) == 1
    )

    qtbot.mouseClick(dialog.buttons()[0], Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: store.state.preview is PreviewState.CURRENT, timeout=5000)
    assert store.state.job.phase.value == "idle"
    assert runtime.prepare_count == 1
    assert not runtime.render_requests
    assert (
        sum(isinstance(event, RenderPreflightDismissed) for event in store.events) == 1
    )
    controller.shutdown()


def test_existing_output_requires_explicit_replace_choice(tmp_path, qtbot) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    output = tmp_path / "source.webp"
    output.write_bytes(b"old")
    runtime = FakeRenderRuntime()
    store = RecordingStore(_current_state(source))
    controller = SourceController(store, preview_runtime=runtime)

    controller.dispatch(RenderVideoRequested())
    dialog = controller.render_controller.collision_dialog
    assert dialog is not None
    assert [button.text() for button in dialog.buttons()] == [
        "Replace",
        "Choose another name",
        "Cancel",
    ]
    assert store.state.job.phase.value == "idle"

    qtbot.mouseClick(dialog.buttons()[0], Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: store.state.artifact is ArtifactState.VALID, timeout=5000)
    assert runtime.render_requests[0].output.collision_policy.value == "replace"
    controller.shutdown()


def test_matching_cut_set_offers_three_choices_with_rebuild_default(
    tmp_path, qtbot
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    cuts_root = tmp_path / ".matteloop-work" / "cuts"
    workspace = CutWorkspace(
        tmp_path,
        tmp_path / ".matteloop-work",
        cuts_root,
        tmp_path / ".matteloop-work" / "scratch",
        "a" * 64,
        cuts_root / "source-aaaaaaaa",
        WorkspaceLifecycle.PROMOTED,
        None,
        "source-aaaaaaaa",
    )
    runtime = MatchingCutsRuntime(workspace)
    store = RecordingStore(_current_state(source))
    controller = SourceController(store, preview_runtime=runtime)

    controller.dispatch(RenderVideoRequested())

    qtbot.waitUntil(
        lambda: controller.render_controller.reuse_dialog is not None,
        timeout=5000,
    )
    dialog = controller.render_controller.reuse_dialog
    assert dialog is not None
    assert [button.text() for button in dialog.buttons()] == [
        "Rebuild",
        "Regenerate",
        "Cancel",
    ]
    assert dialog.defaultButton().text() == "Rebuild"
    qtbot.mouseClick(dialog.buttons()[2], Qt.MouseButton.LeftButton)
    qtbot.waitUntil(
        lambda: controller.render_controller.reuse_dialog is None,
        timeout=5000,
    )
    assert store.state.job.kind is None
    assert not runtime.render_requests
    controller.shutdown()


def test_artifact_ready_fires_with_the_workers_raw_artifact(tmp_path, qtbot) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    runtime = FakeRenderRuntime()
    store = RecordingStore(_current_state(source))
    controller = SourceController(store, preview_runtime=runtime)
    received: list[object] = []
    controller.render_controller.artifact_ready.connect(received.append)

    controller.dispatch(RenderVideoRequested())

    qtbot.waitUntil(lambda: store.state.artifact is ArtifactState.VALID, timeout=5000)
    assert len(received) == 1
    assert received[0].output_path == runtime.render_requests[0].output.path
    controller.shutdown()


def test_use_this_set_restores_the_stored_transform_before_rebuilding(
    tmp_path, qtbot
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    cuts_root = tmp_path / ".matteloop-work" / "cuts"
    workspace = CutWorkspace(
        tmp_path,
        tmp_path / ".matteloop-work",
        cuts_root,
        tmp_path / ".matteloop-work" / "scratch",
        "a" * 64,
        cuts_root / "source-aaaaaaaa",
        WorkspaceLifecycle.PROMOTED,
        None,
        "source-aaaaaaaa",
    )
    manifest = _rebuild_manifest(source)
    runtime = RebuildRuntime()
    store = RecordingStore(_current_state(source))
    controller = SourceController(store, preview_runtime=runtime)
    restored: list[CutWorkspace] = []
    restored_transform = TransformSpec(first_frame=2)

    def restore(target: CutWorkspace) -> None:
        restored.append(target)
        store.dispatch(TransformChanged(restored_transform))

    controller.render_controller.transform_restore = restore

    controller.render_controller._use_workspace(  # noqa: SLF001
        WorkspaceSummary(workspace, manifest, 0)
    )

    qtbot.waitUntil(lambda: len(runtime.render_requests) == 1, timeout=5000)
    assert restored == [workspace]
    assert runtime.render_requests[0].transform == restored_transform
    controller.shutdown()


def test_output_actions_open_artifact_and_its_folder(
    tmp_path, qtbot, monkeypatch
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    output = tmp_path / "source.webp"
    runtime = FakeRenderRuntime()
    store = RecordingStore(_current_state(source))
    controller = SourceController(store, preview_runtime=runtime)
    controller.dispatch(RenderVideoRequested())
    qtbot.waitUntil(lambda: store.state.artifact is ArtifactState.VALID, timeout=5000)

    opened: list[str] = []
    monkeypatch.setattr(
        QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    controller.dispatch(OpenOutputRequested())
    controller.dispatch(OpenOutputFolderRequested())
    assert opened == [str(output), str(tmp_path)]
    controller.shutdown()
