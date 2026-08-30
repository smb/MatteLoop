from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from threading import get_ident

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices, QImage

from rembggui.core.crop_state import CropChanged
from rembggui.core.specs import CropSpec
from rembggui.core.state import (
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
from rembggui.core.timeline import EndChanged, StartChanged
from rembggui.core.webp import validate_webp
from rembggui.jobs.context import CancellationState, JobContext, ProgressEvent
from rembggui.jobs.render import ImmutableRgba, PreparedSegmentation, RenderArtifact
from rembggui.jobs.workspace import CutWorkspace, WorkspaceLifecycle
from rembggui.ui.controller import SourceController
from rembggui.ui.ports import (
    OpenOutputFolderRequested,
    OpenOutputRequested,
    RenderVideoRequested,
)
from rembggui.ui.preview_controller import PreviewRuntime
from rembggui.ui.render_pipeline import _StageReporter, render_prepared
from rembggui.ui.store import ReducerStore
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
            FakeSegmenter(), model_id, "ab" * 32, "2.0.72", frozenset({"standard"})
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


class ServiceRenderRuntime(FakeRenderRuntime):
    def render(self, request, context) -> RenderArtifact:
        self.render_requests.append(request)
        self.render_thread_id = get_ident()
        return render_prepared(
            PreparedSegmentation(
                FakeSegmenter(),
                "birefnet-portrait",
                "ab" * 32,
                "2.0.72",
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
    assert store.state.artifact_result.value == request.output.path
    assert [
        event.stage for event in store.events if isinstance(event, JobStageChanged)
    ] == ["Decode", "Segmentation", "Post-process", "Encode", "Validate"]
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
    assert sum(
        isinstance(event, RenderPreflightRequested) for event in store.events
    ) == 1

    qtbot.mouseClick(dialog.buttons()[0], Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: store.state.preview is PreviewState.CURRENT, timeout=5000)
    assert store.state.job.phase.value == "idle"
    assert runtime.prepare_count == 1
    assert not runtime.render_requests
    assert sum(
        isinstance(event, RenderPreflightDismissed) for event in store.events
    ) == 1
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
    cuts_root = tmp_path / ".rembggui-work" / "cuts"
    workspace = CutWorkspace(
        tmp_path,
        tmp_path / ".rembggui-work",
        cuts_root,
        tmp_path / ".rembggui-work" / "scratch",
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
