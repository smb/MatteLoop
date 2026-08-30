"""Fixed editor actions, deliberately outside the inspector scroll area."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
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
        layout.setContentsMargins(16, 8, 16, 12)
        self.success_banner = QLabel("Output ready")
        self.success_banner.setObjectName("success_banner")
        self.success_banner.setAccessibleName("Output ready")
        self.success_banner.hide()
        layout.addWidget(self.success_banner)
        row = QHBoxLayout()
        self.preview_button = QPushButton("Preview Frame")
        self.preview_button.setObjectName("preview_action")
        self.preview_button.setAccessibleName("Preview Frame")
        self.render_button = QPushButton("Render Video")
        self.render_button.setObjectName("render_action")
        self.render_button.setAccessibleName("Render Video")
        self.rebuild_button = QPushButton("Rebuild from edited cuts")
        self.rebuild_button.setObjectName("rebuild_action")
        self.rebuild_button.setAccessibleName("Rebuild from edited cuts")
        self.open_output_button = QPushButton("Open output")
        self.open_output_button.setObjectName("open_output")
        self.open_output_button.setAccessibleName("Open output")
        self.open_folder_button = QPushButton("Open folder")
        self.open_folder_button.setObjectName("open_output_folder")
        self.open_folder_button.setAccessibleName("Open output folder")
        for button in (self.preview_button, self.render_button, self.rebuild_button):
            button.setMinimumHeight(40)
            row.addWidget(button)
        row.addStretch(1)
        row.addWidget(self.open_output_button)
        row.addWidget(self.open_folder_button)
        layout.addLayout(row)
