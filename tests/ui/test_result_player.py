from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from matteloop.core.parameters import TransformChanged
from matteloop.core.specs import CropSpec, FramingSpec, TransformSpec
from matteloop.core.state import (
    AppState,
    JobKind,
    SourceLoaded,
    SourceLoadRequested,
    reduce,
)
from matteloop.jobs.render import FilesystemWorkspacePort
from matteloop.jobs.transform_stage import framing_plan
from matteloop.ui.crop_presentation import CropPresentation
from matteloop.ui.result_player import (
    FrameLoadWorker,
    PlayerFrames,
    ResultPlayerCanvas,
    _fit_budget,
)
from matteloop.ui.store import ReducerStore
from matteloop.ui.transform_group import TransformGroup
from matteloop.ui.transform_stage import TransformStageController
from tests.jobs.render_support import (
    FakeEncoder,
    FakeSegmenter,
    job,
    render_service,
    request,
)


def _presentation(crop: CropSpec) -> CropPresentation:
    return CropPresentation(
        source_id="cut",
        width=128,
        height=128,
        coded_width=128,
        coded_height=128,
        rotation=0,
        pixel_aspect=1.0,
        crop=crop,
    )


def _player_frames(
    frame_count: int, delays: tuple[int, ...], *, cached: int | None = None
) -> PlayerFrames:
    stored = cached if cached is not None else frame_count
    framed = tuple(QImage(4, 4, QImage.Format.Format_RGBA8888) for _ in range(stored))
    transformed = tuple(
        QImage(4, 4, QImage.Format.Format_RGBA8888) for _ in range(stored)
    )
    return PlayerFrames(
        key=("cut", (4, 4), None, None, (4, 4)),
        framed=framed,
        transformed=transformed,
        delays_ms=delays,
        cached=stored,
        frame_count=frame_count,
    )


def _ready_state(path: Path) -> AppState:
    loading = reduce(AppState(), SourceLoadRequested("source", "load"))

    class _Metadata:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.width = 128
            self.height = 128
            self.duration = Fraction(2)
            self.average_rate = Fraction(30)

    return reduce(loading, SourceLoaded("source", "load", _Metadata(path)))


def _seed_cut(tmp_path: Path, job_id: str = "seed", *, segmenter=None, encoder=None):
    service = render_service(
        segmenter=segmenter,
        encoder=encoder,
        workspace=FilesystemWorkspacePort(),
    )
    return service.render(request(tmp_path), job(tmp_path, job_id, JobKind.RENDER))


class _ExplodingReader:
    def read(self, workspace: object, frame: object) -> QImage:
        del workspace, frame
        raise OSError("boom")


# -- canvas playback -----------------------------------------------------


def test_canvas_shows_the_kept_range_start_and_wraps_on_the_timer(qtbot) -> None:
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    frames = _player_frames(4, (10, 10, 10, 10))

    canvas.set_frames(frames)
    canvas.set_kept_range(range(1, 4))

    assert canvas.current_frame == 1
    canvas.play()
    qtbot.waitUntil(lambda: canvas.current_frame == 2, timeout=1000)
    qtbot.waitUntil(lambda: canvas.current_frame == 3, timeout=1000)
    qtbot.waitUntil(lambda: canvas.current_frame == 1, timeout=1000)
    canvas.pause()
    frozen = canvas.current_frame
    qtbot.wait(60)
    assert canvas.current_frame == frozen


def test_new_kept_range_re_slices_without_a_new_playerframes(qtbot) -> None:
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    frames = _player_frames(4, (100, 100, 100, 100))
    canvas.set_frames(frames)
    canvas.set_kept_range(range(0, 4))
    assert canvas.current_frame == 0

    canvas.set_kept_range(range(2, 4))

    assert canvas.current_frame == 2
    assert canvas._frames is frames  # noqa: SLF001


def test_play_button_hidden_without_frames_and_shown_with_them(qtbot) -> None:
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    canvas.show()
    assert canvas.play_button.isHidden()

    canvas.set_frames(_player_frames(2, (100, 100)))
    assert not canvas.play_button.isHidden()

    canvas.set_frames(None)
    assert canvas.play_button.isHidden()


def test_session_displays_in_fit_mode_and_restores_cover(qtbot) -> None:
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    canvas.set_cover_frame(True)

    canvas.set_frames(_player_frames(2, (100, 100)))
    assert canvas._cover_frame is False  # noqa: SLF001

    canvas.set_frames(None)
    assert canvas._cover_frame is True  # noqa: SLF001


def test_new_preview_image_pauses_and_a_repeated_call_is_ignored(qtbot) -> None:
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    canvas.set_frames(_player_frames(3, (500, 500, 500)))
    canvas.play()
    assert canvas.playing

    preview = QImage(2, 2, QImage.Format.Format_RGBA8888)
    canvas.set_presented_frame(preview, "placeholder")
    assert not canvas.playing

    canvas.play()
    assert canvas.playing
    canvas.set_presented_frame(preview, "placeholder")
    assert canvas.playing


def test_set_frames_shows_the_truncation_marker_when_cache_was_truncated(qtbot) -> None:
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    frames = _player_frames(10, tuple([100] * 4), cached=4)

    canvas.set_frames(frames)

    assert canvas.status_label.text() == "Previewing the first 4 of 10 frames"
    assert not canvas.status_label.isHidden()

    canvas.set_frames(_player_frames(4, (100, 100, 100, 100)))
    assert canvas.status_label.text() == ""


def test_keyboard_nudge_never_emits_a_source_crop_change(qtbot) -> None:
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(200, 200)
    canvas.set_frame(QImage(128, 128, QImage.Format.Format_RGBA8888))
    presentation = _presentation(CropSpec(10, 10, 40, 20))
    canvas.set_crop_edit(True, presentation, TransformSpec(first_frame=2))
    canvas.show()
    canvas.activateWindow()
    canvas.setFocus()
    qtbot.waitUntil(canvas.hasFocus, timeout=1000)
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    qtbot.keyClick(canvas, Qt.Key.Key_Left)

    assert events, "the nudge should dispatch a command"
    assert all(isinstance(event, TransformChanged) for event in events)
    assert events[-1].transform.first_frame == 2
    assert events[-1].transform.crop == CropSpec(9, 10, 40, 20)


def test_aspect_lock_constrains_the_crop_before_dispatch(qtbot) -> None:
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    presentation = _presentation(CropSpec(0, 0, 40, 40))
    canvas.set_crop_edit(True, presentation, TransformSpec())
    canvas.set_aspect_lock(Fraction(2, 1))

    constrained = canvas._constrain(  # noqa: SLF001
        CropSpec(0, 0, 40, 60), "south_east"
    )

    assert Fraction(constrained.width, constrained.height) == Fraction(2, 1)


# -- budget math -----------------------------------------------------------


def test_fit_budget_shrinks_the_display_size_before_truncating_frames() -> None:
    display_size, cached = _fit_budget(10, (1000, 1000), 10_000_000)
    assert cached == 10
    assert display_size[0] < 1000


def test_fit_budget_truncates_frames_once_the_floor_is_hit() -> None:
    display_size, cached = _fit_budget(1000, (1000, 1000), 200_000)
    assert display_size == (64, 64)
    assert 1 <= cached < 1000


# -- worker failure (E23) ---------------------------------------------------


def test_frame_load_worker_reports_unreadable_frames(tmp_path) -> None:
    artifact = _seed_cut(tmp_path, "worker-fail")
    manifest = artifact.manifest
    plan = framing_plan((manifest.width, manifest.height), None, FramingSpec())
    worker = FrameLoadWorker(
        artifact.cut_workspace,
        manifest,
        _ExplodingReader(),
        plan,
        TransformSpec(),
        (100,) * manifest.frame_count,
        1,
    )
    failures: list[str] = []
    worker.failed.connect(lambda message, _generation: failures.append(message))
    worker.run()

    assert failures == ["Cut frames could not be read"]


# -- AC 9: zero segmentation, zero encoder calls during playback ------------


def test_playback_never_calls_segmentation_or_the_encoder(tmp_path, qtbot) -> None:
    segmenter = FakeSegmenter()
    encoder = FakeEncoder()
    artifact = _seed_cut(tmp_path, "ac9", segmenter=segmenter, encoder=encoder)

    store = ReducerStore(_ready_state(tmp_path / "source.mp4"))
    controller = TransformStageController(store)
    canvas = ResultPlayerCanvas()
    qtbot.addWidget(canvas)
    group = TransformGroup(lambda _event: None)
    qtbot.addWidget(group)
    controller.attach(group, canvas)

    controller.open_artifact(artifact)
    qtbot.waitUntil(lambda: canvas.current_frame is not None, timeout=5000)

    segment_calls = len(segmenter.calls)
    encode_calls = len(encoder.calls)

    canvas.play()
    for _ in range(6):
        canvas._advance()  # noqa: SLF001
    canvas.pause()

    assert len(segmenter.calls) == segment_calls
    assert len(encoder.calls) == encode_calls
    controller.shutdown()
