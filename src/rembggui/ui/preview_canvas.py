"""Shared original/result preview stage for the Task 13 shell."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget


class PreviewCanvas(QLabel):
    def __init__(
        self, title: str, object_name: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._placeholder = title
        self._frame: QImage | None = None
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
        self.setText(self._placeholder)
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

    def set_frame(self, image: QImage | None) -> None:
        """Display a GUI-owned pixmap scaled to fit the current canvas."""
        if image is None or image.isNull():
            self._frame = None
            self.clear()
            self.setText(self._placeholder)
            return
        self._frame = QImage(image)
        self.setText("")
        self._update_pixmap()

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._update_pixmap()

    def _update_pixmap(self) -> None:
        if self._frame is None:
            return
        margins = self.layout().contentsMargins()  # type: ignore[union-attr]
        size = QSize(
            max(1, self.width() - margins.left() - margins.right()),
            max(1, self.height() - margins.top() - margins.bottom()),
        )
        pixmap = QPixmap.fromImage(self._frame).scaled(
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(pixmap)


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
