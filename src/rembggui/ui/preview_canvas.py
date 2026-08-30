"""Shared original/result preview stage for the Task 13 shell."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget


class PreviewCanvas(QLabel):
    def __init__(
        self, title: str, object_name: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setAccessibleName(
            "Original video frame"
            if title == "Original"
            else "Background-removed result"
        )
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumWidth(200)
        self.setMinimumHeight(180)
        self.setWordWrap(True)
        self.status_label = QLabel(self)
        self.status_label.setObjectName(f"{object_name}_status")
        self.status_label.setProperty("secondary", True)
        self.status_label.hide()
        overlay = QVBoxLayout(self)
        overlay.setContentsMargins(8, 8, 8, 8)
        overlay.addWidget(self.status_label)
        overlay.addStretch(1)

    def set_status_marker(self, marker: str | None) -> None:
        self.status_label.setText(marker or "")
        self.status_label.setVisible(bool(marker))


class PreviewStage(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("preview_stage")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(1)
        self.original_canvas = PreviewCanvas("Original", "original_canvas")
        self.result_canvas = PreviewCanvas("Result", "result_canvas")
        self.original_canvas.setText("Original")
        self.result_canvas.setProperty("status", "none")
        self.result_canvas.setProperty("checkerboard", False)
        layout.addWidget(self.original_canvas, 1)
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setObjectName("preview_divider")
        layout.addWidget(divider)
        layout.addWidget(self.result_canvas, 1)


class TimelinePlaceholder(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("timeline_placeholder")
        self.setAccessibleName("Video timeline")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(176)
        layout = QVBoxLayout(self)
        label = QLabel("Timeline editing arrives in the next step")
        label.setProperty("secondary", True)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
