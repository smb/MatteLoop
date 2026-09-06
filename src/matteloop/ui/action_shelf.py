"""Fixed editor actions, deliberately outside the inspector scroll area."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from matteloop.ui.ports import StateStore, WindowServices
from matteloop.ui.settings_dialog import SettingsDialog
from matteloop.ui.theme import ACCENT_COLOR


def _gear_icon() -> QIcon:
    """Paint the preferences glyph without a shipped image asset."""
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(ACCENT_COLOR))
    painter.translate(10.0, 10.0)
    for tooth in range(8):
        painter.save()
        painter.rotate(tooth * 45.0)
        painter.drawRect(QRectF(-1.35, -8.8, 2.7, 3.4))
        painter.restore()
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor(ACCENT_COLOR), 1.6))
    painter.drawEllipse(QRectF(-5.8, -5.8, 11.6, 11.6))
    painter.end()
    return QIcon(pixmap)


class ActionShelf(QFrame):
    def __init__(
        self,
        store: StateStore,
        services: WindowServices,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("action_shelf")
        self.setFixedHeight(104)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        row = QHBoxLayout()
        self.preview_button = QPushButton("Preview Frame")
        self.preview_button.setObjectName("preview_action")
        self.preview_button.setAccessibleName("Preview Frame")
        self.render_button = QPushButton("Render Video")
        self.render_button.setObjectName("render_action")
        self.render_button.setAccessibleName("Render Video")
        self.preferences_button = QPushButton()
        self.preferences_button.setObjectName("preferences_action")
        self.preferences_button.setAccessibleName("Preferences")
        self.preferences_button.setToolTip("Preferences")
        self.preferences_button.setIcon(_gear_icon())
        self.preferences_button.setIconSize(QSize(20, 20))
        self.preferences_button.setFixedWidth(48)
        self.preferences_button.setMinimumHeight(40)
        self.preferences_button.setShortcut(
            QKeySequence(QKeySequence.StandardKey.Preferences)
        )
        for button in (self.preview_button, self.render_button):
            button.setMinimumHeight(40)
            button.setSizePolicy(
                button.sizePolicy().Policy.Expanding,
                button.sizePolicy().Policy.Fixed,
            )
            row.addWidget(button, 1)
        row.addWidget(self.preferences_button)
        layout.addLayout(row)
        self.setTabOrder(self.preview_button, self.render_button)
        self.setTabOrder(self.render_button, self.preferences_button)
        self.preferences_dialog = SettingsDialog(store, services, self)
        self.preferences_button.clicked.connect(self.open_preferences)

    def open_preferences(self) -> None:
        """Reload current state and show the window-modal Preferences dialog."""
        self.preferences_dialog.load()
        self.preferences_dialog.open()
