"""Small Qt helpers for inspector fields that must fit narrow columns."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics, QPaintEvent, QResizeEvent
from PySide6.QtWidgets import (
    QComboBox,
    QLineEdit,
    QSizePolicy,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QWidget,
)


def compact_field[WidgetT: QWidget](widget: WidgetT) -> WidgetT:
    """Allow a standard field to use the width its enclosing form provides."""
    widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
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
