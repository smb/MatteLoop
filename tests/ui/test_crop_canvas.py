from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage

from matteloop.core.crop_state import CropChanged
from matteloop.core.geometry import MediaTransform, PointF
from matteloop.core.specs import CropSpec
from matteloop.ui.crop_canvas import CropCanvas
from matteloop.ui.crop_presentation import CropPresentation
from matteloop.ui.preview_canvas import PreviewStage


def _presentation(crop: CropSpec = CropSpec(10, 10, 40, 20)) -> CropPresentation:
    return CropPresentation(
        source_id="source",
        width=100,
        height=50,
        coded_width=100,
        coded_height=50,
        rotation=0,
        pixel_aspect=1,
        crop=crop,
    )


def _canvas(qtbot) -> CropCanvas:
    canvas = CropCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(200, 100)
    canvas.set_frame(QImage(100, 50, QImage.Format.Format_RGBA8888))
    canvas.apply_presentation(_presentation(), active=True, editable=True)
    canvas.show()
    return canvas


def test_crop_canvas_drag_emits_oriented_crop_changes_from_shared_geometry(
    qtbot,
) -> None:
    canvas = _canvas(qtbot)
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    qtbot.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(60, 80))
    qtbot.mouseMove(canvas, QPoint(80, 90))
    qtbot.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(80, 90))

    assert isinstance(events[-1], CropChanged)
    assert events[-1].crop == CropSpec(20, 15, 40, 20)


def test_crop_canvas_keyboard_moves_overlay_by_one_source_pixel(qtbot) -> None:
    canvas = _canvas(qtbot)
    events: list[object] = []
    canvas.command_requested.connect(events.append)
    canvas.activateWindow()
    canvas.setFocus()
    qtbot.waitUntil(canvas.hasFocus, timeout=1000)

    qtbot.keyClick(canvas, Qt.Key.Key_Left)
    canvas.apply_presentation(
        _presentation(CropSpec(9, 10, 40, 20)), active=True, editable=True
    )
    qtbot.keyClick(canvas, Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)

    assert [event.crop for event in events if isinstance(event, CropChanged)] == [
        CropSpec(9, 10, 40, 20),
        CropSpec(9, 0, 40, 20),
    ]


def test_crop_canvas_focused_handle_resizes_with_shift_keyboard_step(qtbot) -> None:
    canvas = _canvas(qtbot)
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    qtbot.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(100, 80))
    qtbot.waitUntil(canvas.hasFocus, timeout=1000)
    qtbot.keyClick(canvas, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)

    assert isinstance(events[-1], CropChanged)
    assert events[-1].crop == CropSpec(10, 10, 50, 20)


def test_crop_canvas_announces_current_oriented_bounds_on_its_standard_widget(
    qtbot,
) -> None:
    canvas = _canvas(qtbot)

    assert "x 10" in canvas.accessibleDescription()
    assert "100 × 50" in canvas.accessibleDescription()


def _large_source_canvas(qtbot, crop: CropSpec) -> tuple[PreviewStage, CropCanvas]:
    stage = PreviewStage()
    qtbot.addWidget(stage)
    canvas = stage.original_canvas
    canvas.resize(478, 574)
    canvas.set_frame(QImage(1280, 720, QImage.Format.Format_RGBA8888))
    canvas.apply_presentation(
        CropPresentation(
            source_id="source",
            width=1280,
            height=720,
            coded_width=1280,
            coded_height=720,
            rotation=0,
            pixel_aspect=1,
            crop=crop,
        ),
        active=True,
        editable=True,
    )
    canvas.show()
    qtbot.wait(10)
    return stage, canvas


def test_crop_canvas_fits_source_corners_inside_the_painted_pixmap(qtbot) -> None:
    _stage, canvas = _large_source_canvas(qtbot, CropSpec(0, 0, 1280, 720))
    assert canvas._geometry is not None
    transform = canvas._geometry.transform
    assert isinstance(transform, MediaTransform)

    for corner in (PointF(0, 0), PointF(1280, 720)):
        mapped = transform.source_to_widget(corner)
        assert 0 <= mapped.x <= canvas.width()
        assert 0 <= mapped.y <= canvas.height()

    assert transform.scale == pytest.approx(478 / 1280)
    assert transform.content_rect.width == pytest.approx(
        canvas.pixmap().width(), abs=0.01
    )
    assert transform.content_rect.height == pytest.approx(
        canvas.pixmap().height(), abs=1.0
    )
    assert transform.content_rect.x == pytest.approx(
        (canvas.width() - canvas.pixmap().width()) / 2, abs=0.5
    )
    assert transform.content_rect.y == pytest.approx(
        (canvas.height() - canvas.pixmap().height()) / 2, abs=0.5
    )


def test_crop_canvas_drag_uses_the_fitted_frame_scale(qtbot) -> None:
    _stage, canvas = _large_source_canvas(qtbot, CropSpec(100, 100, 400, 200))
    assert canvas._geometry is not None
    transform = canvas._geometry.transform
    assert isinstance(transform, MediaTransform)
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    qtbot.mousePress(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=QPoint(80, 200),
    )
    qtbot.mouseMove(canvas, QPoint(160, 200))
    qtbot.mouseRelease(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=QPoint(160, 200),
    )

    assert isinstance(events[-1], CropChanged)
    assert events[-1].crop == CropSpec(314, 100, 400, 200)
