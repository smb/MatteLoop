"""Small Qt helpers for inspector fields that must fit narrow columns."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QFontMetrics, QPaintEvent, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QLineEdit,
    QSizePolicy,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QWidget,
)


class _IgnoreUnfocusedWheel(QObject):
    """Let the wheel scroll past a combo/spin box unless it holds focus.

    The wheel edits a field only after the user has focused it by clicking or
    tabbing; an unfocused field leaves the event unaccepted so it propagates
    to the enclosing scroll area exactly as it would from a plain label.

    This filter is only half of the mechanism and cannot work alone. Qt gives
    focus *before* it delivers a wheel event: ``QApplication::notify`` calls
    ``giveFocusAccordingToFocusPolicy`` on the widget under the pointer, and
    any enabled ``Qt::WheelFocus`` widget there receives focus on the spot.
    ``QComboBox`` and ``QAbstractSpinBox`` are both constructed with
    ``WheelFocus``, so a ``hasFocus()`` check seen from this filter is already
    true and the field edits anyway. ``compact_field`` therefore lowers the
    policy to ``Qt::StrongFocus`` (click and Tab, never the wheel), which is
    what keeps the check meaningful.

    Returning ``True`` is likewise load-bearing: it stops the widget's own
    ``wheelEvent`` from running and re-accepting the event. Propagation is
    unaffected, because ``QApplication::notify`` continues up the parent
    chain whenever the event is left unaccepted, regardless of the filter's
    return value.
    """

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.Wheel
            and isinstance(watched, QWidget)
            and not watched.hasFocus()
        ):
            event.ignore()
            return True
        return super().eventFilter(watched, event)


def compact_field[WidgetT: QWidget](widget: WidgetT) -> WidgetT:
    """Allow a standard field to use the width its enclosing form provides.

    Combo and spin boxes additionally give up the wheel while unfocused; see
    ``_IgnoreUnfocusedWheel`` for why the focus policy must change with it.
    """
    widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
    if isinstance(widget, (QComboBox, QAbstractSpinBox)):
        widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        widget.installEventFilter(_IgnoreUnfocusedWheel(widget))
    return widget


class ElidingComboBox(QComboBox):
    """Paint the closed selection with the available width, not raw text width."""

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        text_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxEditField,
            self,
        )
        available = text_rect.width()
        if not option.currentIcon.isNull():
            available -= option.iconSize.width() + 4
        option.currentText = QFontMetrics(self.font()).elidedText(
            option.currentText,
            Qt.TextElideMode.ElideRight,
            max(0, available),
        )
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)


class MiddleElidingLineEdit(QLineEdit):
    """Display a complete value with middle elision while retaining its semantics."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""

    def setText(self, text: str | None) -> None:
        value = text or ""
        self._full_text = value
        self.setToolTip(value)
        self.setAccessibleDescription(value)
        self._refresh_display()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._refresh_display()

    def _refresh_display(self) -> None:
        display = QFontMetrics(self.font()).elidedText(
            self._full_text,
            Qt.TextElideMode.ElideMiddle,
            max(0, self.width() - 20),
        )
        if display == QLineEdit.text(self):
            return
        blocked = self.blockSignals(True)
        try:
            QLineEdit.setText(self, display)
        finally:
            self.blockSignals(blocked)
