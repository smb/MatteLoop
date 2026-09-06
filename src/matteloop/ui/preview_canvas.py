"""Shared original/result preview stage for the Task 13 shell."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QRect, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from matteloop.resources import status_icon_asset
from matteloop.ui.theme import CHECKERBOARD_DARK, CHECKERBOARD_LIGHT


class StatusLabel(QLabel):
    """Keep status wording as text while painting an optional adjacent icon."""

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        runtime_root: Path | None = None,
    ) -> None:
        super().__init__(text, parent)
        self._runtime_root = runtime_root
        self._requested_icon_name: str | None = None
        self._requested_icon_size = 24
        self._status_icon_pixmap: QPixmap | None = None
        self._status_icon_name: str | None = None
        self._status_icon_logical_size = 0
        self.setMinimumHeight(24)
        margins = self.contentsMargins()
        self._text_margins = (
            margins.left(),
            margins.top(),
            margins.right(),
            margins.bottom(),
        )

    def set_status_icon(self, name: str | None, requested_size: int = 24) -> None:
        """Load a GUI-owned icon, retaining text when the packaged file is absent."""
        self._requested_icon_name = name
        self._requested_icon_size = requested_size
        self._load_status_icon()

    @property
    def status_icon_name(self) -> str | None:
        return self._status_icon_name

    @property
    def status_icon_pixmap(self) -> QPixmap | None:
        return self._status_icon_pixmap

    @property
    def status_icon_logical_size(self) -> int:
        return self._status_icon_logical_size

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._load_status_icon()
        super().showEvent(event)

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._status_icon_pixmap is not None:
            left, top, _right, bottom = self._text_margins
            size = self._status_icon_logical_size
            available_height = max(0, self.height() - top - bottom)
            y = top + max(0, (available_height - size) // 2)
            painter = QPainter(self)
            painter.drawPixmap(QRect(left, y, size, size), self._status_icon_pixmap)
            painter.end()
        super().paintEvent(event)

    def _load_status_icon(self) -> None:
        self._clear_status_icon()
        name = self._requested_icon_name
        if name is None:
            return
        try:
            asset = status_icon_asset(
                name,
                self._requested_icon_size,
                device_pixel_ratio=self.devicePixelRatioF(),
                runtime_root=self._runtime_root,
            )
            pixmap = QPixmap(str(asset.path))
        except (FileNotFoundError, OSError, RuntimeError):
            return
        if pixmap.isNull():
            return
        logical_size = max(24, self._requested_icon_size)
        pixmap.setDevicePixelRatio(asset.pixel_size / logical_size)
        self._status_icon_pixmap = pixmap
        self._status_icon_name = name
        self._status_icon_logical_size = logical_size
        self.setMinimumHeight(logical_size)
        left, top, right, bottom = self._text_margins
        self.setContentsMargins(left + logical_size + 8, top, right, bottom)
        self.updateGeometry()
        self.update()

    def _clear_status_icon(self) -> None:
        self._status_icon_pixmap = None
        self._status_icon_name = None
        self._status_icon_logical_size = 0
        self.setMinimumHeight(24)
        left, top, right, bottom = self._text_margins
        self.setContentsMargins(left, top, right, bottom)


class PreviewCanvas(QLabel):
    def __init__(
        self,
        title: str,
        object_name: str,
        parent: QWidget | None = None,
        *,
        runtime_root: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._placeholder = title
        self._frame: QImage | None = None
        self._cover_frame = False
        self.setObjectName(object_name)
        self.setAccessibleName(
            QCoreApplication.translate("PreviewCanvas", "Original video frame")
            if object_name == "original_canvas"
            else QCoreApplication.translate(
                "PreviewCanvas", "Background-removed result"
            )
        )
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumWidth(200)
        self.setMinimumHeight(180)
        self.setWordWrap(True)
        self.setText(self._placeholder)
        self.status_label = StatusLabel(parent=self, runtime_root=runtime_root)
        self.status_label.setObjectName(f"{object_name}_status")
        self.status_label.setProperty("secondary", True)
        self.status_label.hide()
        overlay = QVBoxLayout(self)
        overlay.setContentsMargins(8, 8, 8, 8)
        overlay.addWidget(self.status_label)
        overlay.addStretch(1)

    def set_status_marker(
        self, marker: str | None, icon_name: str | None = None
    ) -> None:
        self.status_label.setText(marker or "")
        self.status_label.set_status_icon(icon_name)
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

    def set_cover_frame(self, enabled: bool) -> None:
        """Fill the assembled stage while preserving the frame's aspect ratio."""
        self._cover_frame = enabled
        self._update_pixmap()

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._update_pixmap()

    def _should_paint_checkerboard(self) -> bool:
        """Whether this canvas paints a checkerboard behind its frame.

        Subclasses override to add cases the ``checkerboard`` property alone
        cannot express (e.g. a player actively holding frames to show).
        """
        return self.property("checkerboard") is True

    def _reserved_bottom_space(self) -> float:
        """Widget-space height, in px, kept clear at the bottom of the media.

        Zero by default -- the media fills the full inset viewport. A canvas
        that overlays a control at the bottom (``ResultPlayerCanvas``'s play
        button) overrides this so the media, and any crop geometry built
        against it, never sits under that control (#25).
        """
        return 0.0

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._should_paint_checkerboard():
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
        reserved = max(0, round(self._reserved_bottom_space()))
        content = self.contentsRect().adjusted(
            margins.left(),
            margins.top(),
            -margins.right(),
            -margins.bottom() - reserved,
        )
        size = QSize(max(1, content.width()), max(1, content.height()))
        pixmap = QPixmap.fromImage(self._frame).scaled(
            size,
            (
                Qt.AspectRatioMode.KeepAspectRatioByExpanding
                if self._cover_frame
                else Qt.AspectRatioMode.KeepAspectRatio
            ),
            Qt.TransformationMode.SmoothTransformation,
        )
        if self._cover_frame and pixmap.size() != size:
            pixmap = pixmap.copy(
                max(0, (pixmap.width() - size.width()) // 2),
                max(0, (pixmap.height() - size.height()) // 2),
                size.width(),
                size.height(),
            )
        if reserved <= 0:
            self.setPixmap(pixmap)
            return
        # Reserved space is asymmetric (bottom-only), so the media can no
        # longer rely on QLabel's own AlignCenter: that centres within the
        # full widget, which would centre the shrunk pixmap over the reserved
        # strip too instead of leaving it clear. Compose it into a
        # widget-sized transparent pixmap at the position it belongs, so
        # QLabel has nothing left to re-centre.
        positioned = QPixmap(self.size())
        positioned.fill(Qt.GlobalColor.transparent)
        x = content.x() + max(0, (content.width() - pixmap.width()) // 2)
        y = content.y() + max(0, (content.height() - pixmap.height()) // 2)
        painter = QPainter(positioned)
        painter.drawPixmap(x, y, pixmap)
        painter.end()
        self.setPixmap(positioned)


class PreviewStage(QFrame):
    def __init__(
        self, parent: QWidget | None = None, *, runtime_root: Path | None = None
    ) -> None:
        super().__init__(parent)
        from matteloop.ui.crop_canvas import CropCanvas
        from matteloop.ui.result_player import ResultPlayerCanvas

        self.setObjectName("preview_stage")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        self.original_canvas = CropCanvas(
            title=QCoreApplication.translate("PreviewCanvas", "Original")
        )
        self.result_canvas = ResultPlayerCanvas(runtime_root=runtime_root)
        self.original_canvas.set_cover_frame(False)
        self.result_canvas.set_cover_frame(True)
        self.original_canvas.setText(
            QCoreApplication.translate("PreviewStage", "Original")
        )
        self.result_canvas.setProperty("status", "none")
        self.result_canvas.setProperty("checkerboard", False)
        layout.addWidget(self.original_canvas, 1)
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setObjectName("preview_divider")
        layout.addWidget(divider)
        layout.addWidget(self.result_canvas, 1)
