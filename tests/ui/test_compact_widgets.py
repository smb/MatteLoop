"""Behaviour of the shared inspector field wrapper in compact_widgets.

Wheel events are delivered with ``QTest.wheelEvent(QWindow, ...)``, which
enters through ``QWindowSystemInterface`` like a platform event: the event is
spontaneous, ``QWidgetWindow`` picks the child under the pointer, and
``QApplication::notify`` runs its focus-on-wheel and parent-propagation logic.
``QApplication.sendEvent`` reproduces none of that (notify short-circuits
synthesized wheel events), which is how two earlier fixes shipped green and
failed at the screen.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from matteloop.ui.compact_widgets import compact_field

_ONE_NOTCH_DOWN = QPoint(0, -120)


class _Form:
    """A scrollable form with one field under test and a focus sink above it."""

    def __init__(self, qtbot, field: QWidget) -> None:
        self.area = QScrollArea()
        qtbot.addWidget(self.area)
        self.area.setWidgetResizable(True)
        self.area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        content = QWidget()
        layout = QVBoxLayout(content)
        self.sink = QLineEdit()
        layout.addWidget(self.sink)
        self.field = field
        layout.addWidget(field)
        for index in range(40):
            layout.addWidget(QLabel(f"filler {index}"))
        self.area.setWidget(content)
        self.area.resize(300, 200)
        self.area.show()
        self.area.activateWindow()
        assert QTest.qWaitForWindowActive(self.area)
        self.sink.setFocus()
        qtbot.waitUntil(self.sink.hasFocus, timeout=1000)
        assert not field.hasFocus()

    def wheel_over_field(self) -> None:
        position = self.field.mapTo(self.area, self.field.rect().center())
        QTest.wheelEvent(self.area.windowHandle(), position, _ONE_NOTCH_DOWN)

    def scrolled(self) -> int:
        return self.area.verticalScrollBar().value()


def _spinbox() -> QSpinBox:
    spinbox = QSpinBox()
    spinbox.setRange(0, 10)
    spinbox.setValue(5)
    return spinbox


def _combobox() -> QComboBox:
    combo = QComboBox()
    combo.addItems(["a", "b", "c", "d", "e"])
    combo.setCurrentIndex(2)
    return combo


def test_wheel_over_unfocused_spinbox_scrolls_the_form_and_leaves_it_alone(
    qtbot,
) -> None:
    form = _Form(qtbot, compact_field(_spinbox()))

    form.wheel_over_field()

    assert form.field.value() == 5
    assert not form.field.hasFocus()
    assert form.sink.hasFocus()
    assert form.scrolled() > 0


def test_wheel_over_unfocused_combobox_scrolls_the_form_and_leaves_it_alone(
    qtbot,
) -> None:
    form = _Form(qtbot, compact_field(_combobox()))

    form.wheel_over_field()

    assert form.field.currentIndex() == 2
    assert not form.field.hasFocus()
    assert form.sink.hasFocus()
    assert form.scrolled() > 0


def test_wheel_over_focused_spinbox_edits_it_instead_of_scrolling(qtbot) -> None:
    form = _Form(qtbot, compact_field(_spinbox()))
    form.field.setFocus()
    qtbot.waitUntil(form.field.hasFocus, timeout=1000)

    form.wheel_over_field()

    assert form.field.value() == 4
    assert form.scrolled() == 0


def test_wheel_over_focused_combobox_edits_it_instead_of_scrolling(qtbot) -> None:
    form = _Form(qtbot, compact_field(_combobox()))
    form.field.setFocus()
    qtbot.waitUntil(form.field.hasFocus, timeout=1000)

    form.wheel_over_field()

    assert form.field.currentIndex() == 3
    assert form.scrolled() == 0


def test_tab_still_reaches_a_compact_field(qtbot) -> None:
    form = _Form(qtbot, compact_field(_spinbox()))

    QTest.keyClick(form.sink, Qt.Key.Key_Tab)

    assert form.field.hasFocus()


def test_wheel_over_an_unwrapped_spinbox_focuses_and_edits_it(qtbot) -> None:
    """The defect compact_field removes, kept as proof the harness can see it."""
    form = _Form(qtbot, _spinbox())

    form.wheel_over_field()

    assert form.field.hasFocus()
    assert form.field.value() == 4
    assert form.scrolled() == 0
