from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage

from rembggui.core.crop_state import CropChanged
from rembggui.core.specs import CropSpec
from rembggui.ui.crop_canvas import CropCanvas
from rembggui.ui.crop_presentation import CropPresentation


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
