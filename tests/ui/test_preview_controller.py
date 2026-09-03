from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from threading import Event, get_ident

import pytest
from PIL import Image
from PySide6.QtCore import QSettings, Qt

from matteloop.core.crop_state import CropChanged
from matteloop.core.specs import CropSpec
from matteloop.core.state import (
    CancelRequested,
    JobStageChanged,
    ModelPrepared,
    PreviewRequested,
    PreviewState,
    PreviewSucceeded,
)
from matteloop.core.timeline import EndChanged, PlayheadChanged, StartChanged
from matteloop.jobs.context import JobContext, ProgressEvent
from matteloop.jobs.render import ImmutableRgba, PreparedSegmentation, PreviewResult
from matteloop.ui.controller import SourceController, SourceLoadResult
from matteloop.ui.main_window import MainWindow
from matteloop.ui.ports import PreviewFrameRequested, VideoDropped
from matteloop.ui.preview_controller import (
    PreviewController,
    PreviewJobDialog,
    PreviewRuntime,
)
from matteloop.ui.store import ReducerStore


@dataclass(frozen=True)
class Metadata:
    path: Path
    width: int = 128
    height: int = 128
    duration: Fraction = Fraction(2)
    average_rate: Fraction = Fraction(30)


class FakeSourceAdapter:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, path: Path, request_id: int) -> SourceLoadResult:
        del request_id
        return SourceLoadResult(
            Metadata(path),
            Image.new("RGB", (128, 128), (20, 40, 60)),
        )


class FakePreviewRuntime(PreviewRuntime):
    def __init__(self) -> None:
        self.prepare_calls: list[tuple[str, dict[str, object]]] = []
        self.requests = []
        self.thread_ids: list[int] = []

    def prepare(
        self, model_id: str, extras: dict[str, object], context: JobContext
    ) -> PreparedSegmentation:
        self.prepare_calls.append((model_id, extras))
        context.progress(
            "Downloading model",
            64,
            total=128,
            detail="64 / 128 bytes",
        )
        return PreparedSegmentation(
            self,
            "birefnet-portrait",
            "ab" * 32,
            "2.0.75",
            frozenset({"standard"}),
        )

    def segment(self, frame, request):
        del request
        return frame

    def preview(
        self, request, playhead: Fraction, context: JobContext
    ) -> PreviewResult:
        self.thread_ids.append(get_ident())
        self.requests.append((request, playhead))
        context.progress("Segmentation", 0, detail="")
        image = Image.new("RGBA", (128, 128), (200, 100, 40, 255))
        return PreviewResult(
            "preview-fingerprint",
            playhead,
            playhead,
            ImmutableRgba(128, 128, image.tobytes()),
            ImmutableRgba(128, 128, image.tobytes()),
            None,
            None,
            False,
            False,
            1,
        )

    def close(self) -> None:
        return


class BlockingPreviewRuntime(FakePreviewRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()

    def preview(
        self, request, playhead: Fraction, context: JobContext
    ) -> PreviewResult:
        del request, playhead
        self.started.set()
        while not context.cancellation.requested:
            Event().wait(0.01)
        context.checkpoint("segmentation")
        raise AssertionError("cancellation checkpoint must raise")


class RecordingStore(ReducerStore):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[object] = []

    def dispatch(self, event) -> None:
        self.events.append(event)
        super().dispatch(event)


def _settings() -> QSettings:
    settings = QSettings(
        QSettings.IniFormat, QSettings.UserScope, "matteloop-preview-test", "ui"
    )
    settings.clear()
    return settings


def test_preview_request_prepares_model_and_displays_the_first_frame_cutout(
    tmp_path: Path, qtbot, request
) -> None:
    path = tmp_path / "source.mp4"
    path.write_bytes(b"fixture")
    runtime = FakePreviewRuntime()
    store = RecordingStore()
    controller = SourceController(
        store,
        source_adapter=FakeSourceAdapter(path),
        preview_runtime=runtime,
    )
    request.addfinalizer(controller.shutdown)
    window = MainWindow(store, controller, _settings())
    qtbot.addWidget(window)
    window.show()

    controller.dispatch(VideoDropped(path))
    qtbot.waitUntil(lambda: store.state.source.value == "ready", timeout=5000)
    controller.dispatch(PreviewFrameRequested())

    qtbot.waitUntil(
        lambda: store.state.preview is PreviewState.CURRENT,
        timeout=5000,
    )

    assert runtime.prepare_calls == [
        (
            "birefnet-portrait",
            {"execution_provider": "CPUExecutionProvider"},
        )
    ]
    assert runtime.thread_ids and runtime.thread_ids[0] != get_ident()
    request, playhead = runtime.requests[0]
    assert playhead == Fraction(0)
    assert request.source == path
    assert request.sampling.start == Fraction(0)
    assert request.sampling.end == Fraction(2)
    assert request.sampling.fps == 15
    assert (request.crop.x, request.crop.y) == (0, 0)
    assert (request.crop.width, request.crop.height) == (128, 128)
    assert request.segmentation.model_id == "birefnet-portrait"
    assert request.segmentation.edge_mode.value == "standard"
    assert request.framing.trim is False
    assert request.framing.padding == 0
    assert request.framing.stretch_x == 1

    assert any(isinstance(event, PreviewRequested) for event in store.events)
    assert any(isinstance(event, ModelPrepared) for event in store.events)
    assert any(isinstance(event, JobStageChanged) for event in store.events)
    assert any(isinstance(event, PreviewSucceeded) for event in store.events)
    assert window.result_canvas.pixmap() is not None
    assert not window.result_canvas.pixmap().isNull()
    assert window.result_canvas.property("checkerboard") is True
    assert window.primary_action_name() == "render"
    assert window.render_button.isEnabled()
    assert window.requested_focus_name() == "result_canvas"


def test_preview_request_uses_the_playhead_and_selected_export_range(
    tmp_path: Path, qtbot
) -> None:
    path = tmp_path / "source.mp4"
    path.write_bytes(b"fixture")
    runtime = FakePreviewRuntime()
    store = RecordingStore()
    controller = SourceController(
        store,
        source_adapter=FakeSourceAdapter(path),
        preview_runtime=runtime,
    )

    controller.dispatch(VideoDropped(path))
    qtbot.waitUntil(lambda: store.state.source.value == "ready", timeout=5000)
    controller.dispatch(StartChanged(Fraction(1, 2)))
    controller.dispatch(EndChanged(Fraction(3, 2)))
    controller.dispatch(PlayheadChanged(Fraction(1)))
    controller.dispatch(PreviewFrameRequested())

    qtbot.waitUntil(lambda: store.state.preview is PreviewState.CURRENT, timeout=5000)
    request, playhead = runtime.requests[0]

    assert playhead == Fraction(1)
    assert request.sampling.start == Fraction(1, 2)
    assert request.sampling.end == Fraction(3, 2)
    controller.shutdown()


def test_preview_request_uses_the_selected_oriented_crop(tmp_path: Path, qtbot) -> None:
    path = tmp_path / "source.mp4"
    path.write_bytes(b"fixture")
    runtime = FakePreviewRuntime()
    store = RecordingStore()
    controller = SourceController(
        store,
        source_adapter=FakeSourceAdapter(path),
        preview_runtime=runtime,
    )

    controller.dispatch(VideoDropped(path))
    qtbot.waitUntil(lambda: store.state.source.value == "ready", timeout=5000)
    controller.dispatch(CropChanged(CropSpec(10, 12, 80, 70)))
    controller.dispatch(PreviewFrameRequested())

    qtbot.waitUntil(lambda: store.state.preview is PreviewState.CURRENT, timeout=5000)
    request, _playhead = runtime.requests[0]

    assert request.crop == CropSpec(10, 12, 80, 70)
    controller.shutdown()


@pytest.mark.parametrize("use_escape", [False, True])
def test_cancel_keeps_modal_dialog_open_until_the_safe_checkpoint(
    tmp_path: Path, qtbot, use_escape: bool
) -> None:
    path = tmp_path / "source.mp4"
    path.write_bytes(b"fixture")
    runtime = BlockingPreviewRuntime()
    store = RecordingStore()
    preview_controller = PreviewController(store, runtime=runtime)
    controller = SourceController(
        store,
        source_adapter=FakeSourceAdapter(path),
        preview_controller=preview_controller,
    )
    window = MainWindow(store, controller, _settings())
    qtbot.addWidget(window)
    window.show()

    controller.dispatch(VideoDropped(path))
    qtbot.waitUntil(lambda: store.state.source.value == "ready", timeout=5000)
    controller.dispatch(PreviewFrameRequested())
    qtbot.waitUntil(runtime.started.is_set, timeout=5000)
    dialog = preview_controller.dialog
    assert dialog is not None and dialog.isVisible()

    if use_escape:
        qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    else:
        qtbot.mouseClick(dialog.cancel_button, Qt.MouseButton.LeftButton)
    assert store.state.job.phase.value == "cancelling"
    assert dialog.isVisible()
    assert not dialog.cancel_button.isEnabled()
    assert dialog.cancel_button.text() == "Cancelling…"
    assert sum(isinstance(event, CancelRequested) for event in store.events) == 1

    qtbot.waitUntil(lambda: store.state.job.phase.value == "idle", timeout=5000)
    assert not dialog.isVisible()


def test_job_dialog_rejects_user_close_until_terminal_event(qtbot) -> None:
    dialog = PreviewJobDialog()
    qtbot.addWidget(dialog)
    dialog.open()
    qtbot.waitUntil(dialog.isVisible, timeout=1000)

    dialog.close()
    assert dialog.isVisible()

    dialog.close_for_terminal()
    assert not dialog.isVisible()


def test_model_download_dialog_shows_human_byte_totals(qtbot) -> None:
    dialog = PreviewJobDialog()
    qtbot.addWidget(dialog)

    dialog.set_progress(
        ProgressEvent(
            "job",
            "Downloading model",
            int(412.3 * 1024**2),
            int(927.6 * 1024**2),
            "Downloading BiRefNet Portrait — 412.3 MiB of 927.6 MiB",
        )
    )

    assert dialog.progress_bar.format() == "412.3 MiB of 927.6 MiB"
    assert dialog.detail_label.text() == (
        "Downloading BiRefNet Portrait — 412.3 MiB of 927.6 MiB"
    )


def test_job_dialog_keeps_stage_detail_and_overall_progress_separate(qtbot) -> None:
    dialog = PreviewJobDialog()
    qtbot.addWidget(dialog)
    dialog.set_job_details("birefnet-portrait", "clip.webp")
    dialog.set_execution_provider("CPUExecutionProvider")
    dialog.set_provider_notice("Using the CPU fallback")

    dialog.set_progress(
        ProgressEvent(
            "job",
            "Encode",
            12,
            39,
            "Encode frame 12 of 39",
            51,
            78,
        )
    )

    assert dialog.stage_label.text() == "Encode"
    assert dialog.detail_label.text() == "Encode frame 12 of 39"
    assert dialog.model_provider_label.text() == "BiRefNet Portrait · CPU"
    assert dialog.output_label.text() == "Output: clip.webp"
    assert dialog.provider_notice_label.text() == "Using the CPU fallback"
    assert dialog.stage_progress_label.text() == "Stage progress"
    assert dialog.overall_progress_label.text() == "Overall progress"
    assert (dialog.progress_bar.minimum(), dialog.progress_bar.maximum()) == (0, 39)
    assert dialog.progress_bar.value() == 12
    assert (
        dialog.overall_progress_bar.minimum(),
        dialog.overall_progress_bar.maximum(),
    ) == (0, 78)
    assert dialog.overall_progress_bar.value() == 51
    assert dialog.rate_label.text() == ""
    assert dialog.estimate_label.text() == ""


def test_job_dialog_marks_unknown_overall_progress_as_indeterminate(qtbot) -> None:
    dialog = PreviewJobDialog()
    qtbot.addWidget(dialog)

    dialog.set_progress(
        ProgressEvent(
            "job",
            "Auto-fit, attempt 3 of at most 12",
            12,
            39,
            "Frame 12 of 39",
        )
    )

    assert dialog.stage_label.text() == "Auto-fit, attempt 3 of at most 12"
    assert dialog.detail_label.text() == "Frame 12 of 39"
    assert dialog.overall_progress_label.text() == "Overall progress (indeterminate)"
    assert (
        dialog.overall_progress_bar.minimum(),
        dialog.overall_progress_bar.maximum(),
    ) == (0, 0)
