"""Fixed editor actions, deliberately outside the inspector scroll area."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ActionShelf(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
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
        for button in (self.preview_button, self.render_button):
            button.setMinimumHeight(40)
            button.setSizePolicy(
                button.sizePolicy().Policy.Expanding,
                button.sizePolicy().Policy.Fixed,
            )
            row.addWidget(button, 1)
        layout.addLayout(row)
