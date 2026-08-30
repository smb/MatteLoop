"""Source identity row and empty/error recovery surface."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


class SourceStrip(QWidget):
    """A compact metadata strip that only reveals metadata actually provided."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("source_strip")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)
        self.filename = QLabel()
        self.filename.setObjectName("source_filename")
        self.filename.setProperty("mono", True)
        self.dimensions = self._label("source_dimensions")
        self.duration = self._label("source_duration")
        self.frame_rate = self._label("source_frame_rate")
        self.file_size = self._label("source_file_size")
        self.replace_button = QPushButton("Replace…")
        self.replace_button.setObjectName("replace_video")
        self.replace_button.setAccessibleName("Replace video")
        for widget in (
            self.filename,
            self.dimensions,
            self.duration,
            self.frame_rate,
            self.file_size,
        ):
            layout.addWidget(widget)
        layout.addStretch(1)
        layout.addWidget(self.replace_button)

    def _label(self, name: str) -> QLabel:
        value = QLabel()
        value.setObjectName(name)
        value.setProperty("secondary", True)
        value.setProperty("mono", True)
        return value

    def set_metadata(self, metadata: object | None) -> None:
        path = getattr(metadata, "path", None)
        if path is not None:
            full_path = str(path)
            self.filename.setText(
                QFontMetrics(self.filename.font()).elidedText(
                    full_path, Qt.TextElideMode.ElideMiddle, 360
                )
            )
            self.filename.setToolTip(full_path)
            self.filename.setAccessibleDescription(full_path)
        else:
            self.filename.setText("Video loaded")
            self.filename.setToolTip("")
            self.filename.setAccessibleDescription("")
        width, height = (
            getattr(metadata, "width", None),
            getattr(metadata, "height", None),
        )
        self.dimensions.setText(f"{width} × {height}" if width and height else "")
        duration = getattr(metadata, "duration", None)
        self.duration.setText(str(duration) if duration is not None else "")
        rate = getattr(metadata, "average_rate", None) or getattr(
            metadata, "peak_rate", None
        )
        self.frame_rate.setText(f"{rate} fps" if rate is not None else "")
        if path is not None:
            try:
                self.file_size.setText(f"{Path(path).stat().st_size:,} bytes")
            except OSError:
                self.file_size.setText("")
        else:
            self.file_size.setText("")


class SourceDropSurface(QWidget):
    """Restrained empty/recovery source-selection surface."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("source_drop_surface")
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.heading = QLabel("Drop a video here")
        self.heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.heading.setAccessibleName("Choose a video")
        self.button = QPushButton("Choose Video…")
        self.button.setObjectName("choose_video")
        self.button.setAccessibleName("Choose Video…")
        self.button.setMinimumHeight(44)
        layout.addWidget(self.heading)
        layout.addWidget(self.button, alignment=Qt.AlignmentFlag.AlignCenter)
