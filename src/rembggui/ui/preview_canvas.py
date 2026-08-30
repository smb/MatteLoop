"""Shared original/result preview stage for the Task 13 shell."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

# DESIGN.md: result transparency uses this checkerboard pair.
CHECKERBOARD_LIGHT = "#343A3F"
CHECKERBOARD_DARK = "#252A2E"


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

    def set_presented_frame(self, image: object, placeholder: str) -> None:
        """Display a presented QImage or restore its placeholder message."""
        if isinstance(image, QImage) and not image.isNull():
            self.set_frame(image)
        else:
            self.set_frame(None)
            self.setText(placeholder)

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._update_pixmap()

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self.property("checkerboard") is True:
            painter = QPainter(self)
            tile = 16
            light = QColor(CHECKERBOARD_LIGHT)
            dark = QColor(CHECKERBOARD_DARK)
            for row, y in enumerate(range(0, self.height(), tile)):
                for column, x in enumerate(range(0, self.width(), tile)):
                    painter.fillRect(
                        x,
                        y,
                        tile,
                        tile,
                        light if (row + column) % 2 == 0 else dark,
                    )
        super().paintEvent(event)

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
