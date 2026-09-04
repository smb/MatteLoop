"""Custom-painted visual crop editor driven by one immutable geometry snapshot."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from matteloop.core.crop import (
    crop_from_drag,
    nudge_crop,
    oriented_point_from_widget,
    oriented_rect_to_source_rect,
)
from matteloop.core.crop_state import CropChanged
from matteloop.core.geometry import (
    _MEDIA_INSET,
    CropGeometryState,
    InteractionGeometry,
    MediaTransform,
    PointF,
    RectF,
    SizeF,
    build_crop_geometry,
)
from matteloop.core.specs import CropSpec
from matteloop.ui.crop_presentation import CropPresentation
from matteloop.ui.preview_canvas import PreviewCanvas
from matteloop.ui.theme import ACCENT_COLOR, CANVAS_COLOR, TEXT_COLOR

_HANDLE_NAMES = (
    "north_west",
    "north",
    "north_east",
    "east",
    "south_east",
    "south",
    "south_west",
    "west",
)


class CropCanvas(PreviewCanvas):
    """Display the source frame and edit its crop in oriented source pixels."""

    command_requested = Signal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str = "Original",
        object_name: str = "original_canvas",
        runtime_root: Path | None = None,
    ) -> None:
        super().__init__(title, object_name, parent, runtime_root=runtime_root)
        self.layout().setContentsMargins(0, 0, 0, 0)  # type: ignore[union-attr]
        self.status_label.hide()
        self.setMouseTracking(True)
        self._presentation: CropPresentation | None = None
        self._geometry: InteractionGeometry | None = None
        self._active = False
        self._editable = False
        self._focused_target = "crop"
        self._dragged: str | None = None
        self._drag_start = PointF(0, 0)
        self._drag_crop: CropPresentation | None = None

    def set_presentation(
        self,
        presentation: CropPresentation | None,
        active: bool = True,
        editable: bool = True,
    ) -> None:
        """Render a presenter snapshot and apply the reducer-derived editability."""
        self.apply_presentation(presentation, active=active, editable=editable)

    def apply_presentation(
        self,
        presentation: CropPresentation | None,
        *,
        active: bool,
        editable: bool,
    ) -> None:
        self._presentation = presentation
        self._active = active
        self._editable = editable
        if presentation is None:
            self._geometry = None
            self.setAccessibleDescription("Original video frame")
        else:
            self._rebuild_geometry()
            self._announce_crop()
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().paintEvent(event)
        if not self._active or self._geometry is None:
            return
        geometry = self._geometry
        transform = geometry.transform
        if not isinstance(transform, MediaTransform):
            return
        content = transform.content_rect
        crop = _qt_rect(geometry.visual["crop"])
        overlay = QColor(CANVAS_COLOR)
        overlay.setAlpha(105)
        painter = QPainter(self)
        painter.fillRect(
            QRectF(content.x, content.y, content.width, crop.top() - content.y),
            overlay,
        )
        painter.fillRect(
            QRectF(
                content.x,
                crop.bottom(),
                content.width,
                content.bottom - crop.bottom(),
            ),
            overlay,
        )
        painter.fillRect(
            QRectF(content.x, crop.top(), crop.left() - content.x, crop.height()),
            overlay,
        )
        painter.fillRect(
            QRectF(
                crop.right(),
                crop.top(),
                content.right - crop.right(),
                crop.height(),
            ),
            overlay,
        )
        painter.setPen(QPen(QColor(ACCENT_COLOR), 2))
        painter.drawRect(crop)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(ACCENT_COLOR))
        for name in _HANDLE_NAMES:
            painter.drawRect(_qt_rect(geometry.visual[name]))
        if self.hasFocus():
            focused = geometry.focus.get(self._focused_target)
            if focused is not None:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(TEXT_COLOR), 1, Qt.PenStyle.DashLine))
                painter.drawRect(_qt_rect(focused).adjusted(-3, -3, 3, 3))
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            not self._editable
            or not self._active
            or event.button() != Qt.MouseButton.LeftButton
        ):
            super().mousePressEvent(event)
            return
        geometry = self._geometry
        if geometry is None:
            super().mousePressEvent(event)
            return
        position = PointF(event.position().x(), event.position().y())
        target = geometry.hit_test(position)
        if target not in {"crop", *_HANDLE_NAMES} or self._presentation is None:
            super().mousePressEvent(event)
            return
        self._focused_target = target
        self._dragged = target
        self._drag_crop = self._presentation
        self._drag_start = oriented_point_from_widget(geometry, position)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._rebuild_geometry(focused=target, dragged=target)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if (
            self._dragged is not None
            and self._editable
            and self._active
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            self._emit_drag(event.position())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragged is not None:
            self._dragged = None
            self._drag_crop = None
            self._rebuild_geometry()
            self._announce_crop()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._editable or not self._active or not self.hasFocus():
            super().keyPressEvent(event)
            return
        delta = _key_delta(event)
        if delta is None or self._presentation is None:
            super().keyPressEvent(event)
            return
        step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
        try:
            crop = nudge_crop(
                self._presentation.crop,
                self._focused_target,
                dx=delta[0] * step,
                dy=delta[1] * step,
                source_width=self._presentation.width,
                source_height=self._presentation.height,
            )
        except ValueError:
            super().keyPressEvent(event)
            return
        crop = self._constrain(crop, self._focused_target)
        if crop != self._presentation.crop:
            self.command_requested.emit(self._crop_event(crop))
            self._announce_crop()
        event.accept()

    def focusInEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().focusInEvent(event)
        self._rebuild_geometry(focused=self._focused_target)
        self.update()

    def focusOutEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._rebuild_geometry()
        super().focusOutEvent(event)
        self.update()

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._rebuild_geometry()

    def _emit_drag(self, position: QPointF) -> None:
        geometry = self._geometry
        presentation = self._drag_crop
        if geometry is None or presentation is None or self._dragged is None:
            return
        current = oriented_point_from_widget(
            geometry, PointF(position.x(), position.y())
        )
        crop = crop_from_drag(
            presentation.crop,
            self._dragged,
            self._drag_start,
            current,
            source_width=presentation.width,
            source_height=presentation.height,
        )
        crop = self._constrain(crop, self._dragged)
        if self._presentation is None or crop != self._presentation.crop:
            self.command_requested.emit(self._crop_event(crop))

    def _constrain(self, crop: CropSpec, target: str) -> CropSpec:
        """Re-fit a candidate crop before it is compared/emitted.

        Identity by default; ``ResultPlayerCanvas`` overrides this to apply
        an aspect lock.
        """
        return crop

    def _crop_event(self, crop: CropSpec) -> object:
        """Build the command to dispatch for a changed crop.

        Default: ``CropChanged`` (the source crop). ``ResultPlayerCanvas``
        overrides this to dispatch ``TransformChanged`` instead -- routing
        both the drag site and ``keyPressEvent`` through this hook is what
        keeps an arrow-key nudge on the result canvas from dispatching a
        *source* crop change (edge case E30).
        """
        return CropChanged(crop)

    def _rebuild_geometry(
        self, *, focused: str | None = None, dragged: str | None = None
    ) -> None:
        presentation = self._presentation
        if presentation is None:
            self._geometry = None
            return
        try:
            raw_crop = oriented_rect_to_source_rect(
                presentation.crop,
                source_width=presentation.coded_width,
                source_height=presentation.coded_height,
                rotation=presentation.rotation,
                pixel_aspect=presentation.pixel_aspect,
            )
            reserved_bottom = max(0.0, self._reserved_bottom_space())
            self._geometry = build_crop_geometry(
                state=CropGeometryState(
                    source_size=SizeF(
                        presentation.coded_width, presentation.coded_height
                    ),
                    crop=raw_crop,
                    rotation=presentation.rotation,
                    pixel_aspect=presentation.pixel_aspect,
                    inset=_MEDIA_INSET,
                    focused=focused
                    if focused is not None
                    else (self._focused_target if self.hasFocus() else None),
                    dragged=dragged if dragged is not None else self._dragged,
                ),
                viewport=SizeF(
                    max(1, self.width()),
                    max(1.0, self.height() - reserved_bottom),
                ),
                dpr=float(self.devicePixelRatioF()),
            )
            transform = self._geometry.transform
            layout = self.layout()
            assert isinstance(transform, MediaTransform)
            assert layout is not None
            inset = int(transform.inset)
            if layout.contentsMargins().left() != inset:
                layout.setContentsMargins(inset, inset, inset, inset)
                self._update_pixmap()
        except (TypeError, ValueError):
            self._geometry = None

    def _announce_crop(self) -> None:
        presentation = self._presentation
        if presentation is None:
            return
        crop = presentation.crop
        self.setAccessibleDescription(
            f"Crop bounds: x {crop.x}, y {crop.y}, width {crop.width}, "
            f"height {crop.height} source pixels; source {presentation.width} × "
            f"{presentation.height}"
        )


def _key_delta(event: QKeyEvent) -> tuple[int, int] | None:
    return {
        int(Qt.Key.Key_Left): (-1, 0),
        int(Qt.Key.Key_Right): (1, 0),
        int(Qt.Key.Key_Up): (0, -1),
        int(Qt.Key.Key_Down): (0, 1),
    }.get(event.key())


def _qt_rect(rect: RectF) -> QRectF:
    return QRectF(rect.x, rect.y, rect.width, rect.height)
