from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from rembggui.core.timeline import (
    PlayheadChanged,
    ResetRange,
    SetEndToPlayhead,
    SetStartToPlayhead,
    StepFrame,
    TimelineState,
)
from rembggui.ui.timeline import TimelineWidget
from rembggui.ui.timeline_presentation import TimelinePresentation


def _presentation(*, playhead: Fraction = Fraction(0)) -> TimelinePresentation:
    state = TimelineState(
        Fraction(4), Fraction(1, 2), Fraction(3, 2), playhead, source_fps=Fraction(30)
    )
    return TimelinePresentation(
        state,
        "source",
        Path("source.mp4"),
        128,
        128,
        None,
        None,
    )


def test_timeline_shows_exact_playhead_range_and_outside_status(qtbot) -> None:
    widget = TimelineWidget()
    qtbot.addWidget(widget)
    widget.resize(900, 176)
    widget.set_presentation(_presentation(playhead=Fraction(2)))

    assert widget.height() >= 176
    assert widget.timecode_label.text() == "0:02.000"
    assert widget.frame_label.text() == "Frame 000061"
    assert widget.range_label.text() == "IN 0:00.500   OUT 0:01.500"
    assert widget.range_status_label.text() == "Outside export range"
    assert widget.accessibleDescription().startswith("Preview frame playhead")


def test_focused_timeline_emits_keyboard_edit_commands(qtbot) -> None:
    widget = TimelineWidget()
    qtbot.addWidget(widget)
    widget.resize(900, 176)
    widget.set_presentation(_presentation())
    widget.show()
    widget.activateWindow()
    events: list[object] = []
    widget.command_requested.connect(events.append)
    widget.setFocus()
    qtbot.waitUntil(widget.hasFocus, timeout=1000)

    QTest.keyClick(widget, Qt.Key.Key_Right)
    QTest.keyClick(widget, Qt.Key.Key_Left, Qt.KeyboardModifier.ShiftModifier)
    QTest.keyClick(widget, Qt.Key.Key_I)
    QTest.keyClick(widget, Qt.Key.Key_O)

    assert events[:2] == [StepFrame(1), StepFrame(-10)]
    assert isinstance(events[2], SetStartToPlayhead)
    assert isinstance(events[3], SetEndToPlayhead)


def test_timeline_shortcuts_do_not_escape_from_a_child_button(qtbot) -> None:
    widget = TimelineWidget()
    qtbot.addWidget(widget)
    widget.set_presentation(_presentation())
    widget.show()
    events: list[object] = []
    widget.command_requested.connect(events.append)
    widget.set_start_button.setFocus()

    QTest.keyClick(widget.set_start_button, Qt.Key.Key_I)

    assert events == []


def test_timeline_pointer_scrub_emits_immediately_from_shared_geometry(qtbot) -> None:
    widget = TimelineWidget()
    qtbot.addWidget(widget)
    widget.resize(900, 176)
    widget.set_presentation(_presentation())
    events: list[object] = []
    widget.command_requested.connect(events.append)

    qtbot.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(450, 70))

    assert len(events) == 1
    assert isinstance(events[0], PlayheadChanged)
    assert Fraction(1) < events[0].timestamp < Fraction(3)


def test_timeline_buttons_emit_range_commands_without_thumbnail_decode(qtbot) -> None:
    widget = TimelineWidget()
    qtbot.addWidget(widget)
    widget.set_presentation(_presentation())
    events: list[object] = []
    widget.command_requested.connect(events.append)

    qtbot.mouseClick(widget.set_start_button, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(widget.set_end_button, Qt.MouseButton.LeftButton)

    assert isinstance(events[0], SetStartToPlayhead)
    assert isinstance(events[1], SetEndToPlayhead)


def test_timeline_reset_range_button_emits_reset_range_command(qtbot) -> None:
    widget = TimelineWidget()
    qtbot.addWidget(widget)
    widget.set_presentation(_presentation())
    events: list[object] = []
    widget.command_requested.connect(events.append)

    qtbot.mouseClick(widget.reset_range_button, Qt.MouseButton.LeftButton)

    assert events == [ResetRange()]
