"""Custom-painted timeline and its cancellable thumbnail worker."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QThread, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from rembggui.core.geometry import (
    InteractionGeometry,
    PointF,
    RectF,
    SizeF,
    TimelineGeometryState,
    build_timeline_geometry,
)
from rembggui.core.timeline import (
    EndChanged,
    PlayheadChanged,
    SetEndToPlayhead,
    SetStartToPlayhead,
    StartChanged,
    StepFrame,
    absolute_frame_number,
    format_timecode,
)
from rembggui.jobs.cache import PixmapCache, ThumbnailCacheKey, ThumbnailDiskCache
from rembggui.jobs.source import SourceRevision, SourceValidationProof, decode_frame
from rembggui.jobs.thumbnails import (
    ThumbnailRequest,
    ThumbnailResult,
    filmstrip_timestamps,
    generate_thumbnail,
)
from rembggui.ui.timeline_presentation import TimelinePresentation

_FILMSTRIP_HEIGHT = 78
_TELEMETRY_HEIGHT = 42
_SCRUB_DEBOUNCE_MS = 125
_LARGE_FRAME_STEP = 10
_THREAD_SHUTDOWN_TIMEOUT_MS = 5000


@dataclass(frozen=True, slots=True)
class _ThumbnailBatch:
    source_id: str
    source: Path
    duration: Fraction
    logical_size: tuple[float, float]
    dpr: float
    generation: int
    revision: SourceRevision
    proof: SourceValidationProof | None
    timestamps: tuple[Fraction, ...]


class ThumbnailWorker(QObject):
    """Decode complete-source thumbnails away from the Qt GUI thread."""

    result = Signal(object)
    finished = Signal()

    def __init__(self, batch: _ThumbnailBatch) -> None:
        super().__init__()
        self._batch = batch

    @Slot()
    def run(self) -> None:
        try:
            disk_cache = _thumbnail_disk_cache()
            fingerprint = _provisional_fingerprint(self._batch.source)
            for timestamp in self._batch.timestamps:
                if self._cancelled():
                    return
                try:
                    result = self._thumbnail_result(
                        timestamp, fingerprint, disk_cache
                    )
                except BaseException:
                    if self._cancelled():
                        return
                    continue
                if result is not None:
                    self.result.emit(result)
        except BaseException:
            # A missing source or one bad sample must not prevent the timeline
            # from remaining usable; the source-load path owns user-facing errors.
            return
        finally:
            self.finished.emit()

    @staticmethod
    def _cancelled() -> bool:
        return QThread.currentThread().isInterruptionRequested()

    def _thumbnail_result(
        self,
        timestamp: Fraction,
        fingerprint: str,
        disk_cache: ThumbnailDiskCache | None,
    ) -> ThumbnailResult:
        request = ThumbnailRequest(
            self._batch.source_id,
            timestamp,
            self._batch.logical_size,
            self._batch.dpr,
            self._batch.generation,
            source_fingerprint=fingerprint,
            source_revision=self._batch.revision,
            validation_proof=self._batch.proof,
        )
        cache_request = _cache_request(request)
        key = _thumbnail_cache_key(cache_request)
        image = _cached_thumbnail(disk_cache, key, cache_request)
        result = (
            ThumbnailResult(request, image)
            if image is not None
            else generate_thumbnail(
                self._batch.source,
                request,
                is_cancelled=self._cancelled,
            )
        )
        if disk_cache is not None and image is None:
            _cache_thumbnail(
                disk_cache, key, ThumbnailResult(cache_request, result.image)
            )
        return result


class SourceFrameWorker(QObject):
    """Decode one exact playhead frame without constructing a QPixmap."""

    result = Signal(object)
    finished = Signal()

    def __init__(
        self,
        source_id: str,
        generation: int,
        source: Path,
        timestamp: Fraction,
        revision: SourceRevision,
        proof: SourceValidationProof | None,
    ) -> None:
        super().__init__()
        self._source_id = source_id
        self._generation = generation
        self._source = source
        self._timestamp = timestamp
        self._revision = revision
        self._proof = proof

    @Slot()
    def run(self) -> None:
        try:
            decoded = decode_frame(
                self._source,
                self._timestamp,
                self._generation,
                expected_revision=self._revision,
                validation_proof=self._proof,
                is_cancelled=self._cancelled,
            )
            try:
                image = _qimage_from_pillow(decoded.image)
            finally:
                decoded.image.close()
            self.result.emit((self._source_id, self._generation, image))
        except BaseException:
            return
        finally:
            self.finished.emit()

    @staticmethod
    def _cancelled() -> bool:
        return QThread.currentThread().isInterruptionRequested()


class TimelineWidget(QFrame):
    """Filmstrip, range handles, playhead, and exact time/frame telemetry."""

    command_requested = Signal(object)

    def __init__(self, parent: QFrame | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("timeline")
        self.setAccessibleName("Video timeline")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(176)
        self.setMouseTracking(True)
        self.setProperty("mono", True)
        self._presentation: TimelinePresentation | None = None
        self._geometry: InteractionGeometry | None = None
        self._dragged: str | None = None
        self._thumbnail_thread: QThread | None = None
        self._thumbnail_worker: ThumbnailWorker | None = None
        self._thumbnail_threads: list[tuple[QThread, ThumbnailWorker]] = []
        self._thumbnail_generation = 0
        self._thumbnail_signature: tuple[object, ...] | None = None
        self._thumbnail_times: tuple[Fraction, ...] = ()
        self._pixmaps: dict[Fraction, QPixmap] = {}
        self._pixmap_cache = PixmapCache()
        self._build_telemetry()
        self._rebuild_geometry()

    def _build_telemetry(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 132, 20, 10)
        layout.setSpacing(12)
        self.timecode_label = QLabel("00:00:00.000")
        self.timecode_label.setObjectName("timeline_timecode")
        self.frame_label = QLabel("Frame —")
        self.frame_label.setObjectName("timeline_frame")
        self.range_label = QLabel("IN —   OUT —")
        self.range_label.setObjectName("timeline_range")
        self.range_status_label = QLabel()
        self.range_status_label.setObjectName("timeline_range_status")
        self.range_status_label.setProperty("secondary", True)
        self.set_start_button = QPushButton("Set IN")
        self.set_start_button.setAccessibleName("Set export start to playhead")
        self.set_end_button = QPushButton("Set OUT")
        self.set_end_button.setAccessibleName("Set export end to playhead")
        for label in (
            self.timecode_label,
            self.frame_label,
            self.range_label,
            self.range_status_label,
        ):
            label.setFont(QFont("IBM Plex Mono", 9))
            label.setProperty("mono", True)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(label)
        layout.addStretch(1)
        layout.addWidget(self.set_start_button)
        layout.addWidget(self.set_end_button)
        self.set_start_button.clicked.connect(
            lambda: self.command_requested.emit(SetStartToPlayhead())
        )
        self.set_end_button.clicked.connect(
            lambda: self.command_requested.emit(SetEndToPlayhead())
        )

    def set_presentation(self, presentation: TimelinePresentation | None) -> None:
        """Render a presenter snapshot and schedule only missing thumbnails."""
        previous = self._presentation
        self._presentation = presentation
        self._rebuild_geometry()
        self._update_telemetry()
        if presentation is None:
            self._cancel_thumbnails()
            self._thumbnail_times = ()
            self._pixmaps.clear()
            self.update()
            return
        source_changed = (
            previous is None or presentation.source_id != previous.source_id
        )
        layout_signature = self._thumbnail_layout_signature(presentation)
        if source_changed:
            self._thumbnail_generation += 1
            self._pixmaps.clear()
            self._thumbnail_signature = None
        if layout_signature != self._thumbnail_signature:
            self._start_thumbnails(presentation, layout_signature)
        self.update()

    def set_timeline(self, presentation: TimelinePresentation | None) -> None:
        """Compatibility spelling for widget-level callers."""
        self.set_presentation(presentation)

    def apply_presentation(
        self, presentation: TimelinePresentation | None, editable: bool
    ) -> None:
        """Apply presenter values and the reducer-derived editability flag."""
        self.setEnabled(editable)
        self.set_presentation(presentation)

    def focusInEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._rebuild_geometry(focused="playhead")
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._rebuild_geometry()
        super().focusOutEvent(event)
        self.update()

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._rebuild_geometry()
        if self._presentation is not None:
            signature = self._thumbnail_layout_signature(self._presentation)
            if signature != self._thumbnail_signature:
                self._start_thumbnails(self._presentation, signature)

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#171B1E"))
        geometry = self._geometry
        if geometry is None:
            painter.setPen(QColor("#8A949B"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Timeline")
            return
        timeline = _qt_rect(geometry.visual["timeline"])
        painter.fillRect(timeline, QColor("#22282C"))
        self._paint_thumbnails(painter, geometry, timeline)
        self._paint_excluded_ranges(painter, geometry, timeline)
        self._paint_range_controls(painter, geometry)
        self._paint_ruler(painter, geometry)
        if self.hasFocus():
            self._paint_focus(painter, geometry)

    def _paint_thumbnails(
        self, painter: QPainter, geometry: InteractionGeometry, timeline: QRectF
    ) -> None:
        if self._presentation is None:
            return
        for index, timestamp in enumerate(self._thumbnail_times):
            left_source = _thumbnail_edge(
                self._thumbnail_times,
                index,
                self._presentation.state.duration,
                left=True,
            )
            right_source = _thumbnail_edge(
                self._thumbnail_times,
                index,
                self._presentation.state.duration,
                left=False,
            )
            left = geometry.source_to_widget(PointF(float(left_source), 0)).x
            right = geometry.source_to_widget(PointF(float(right_source), 0)).x
            tile = QRectF(
                left + 1,
                timeline.top() + 10,
                max(1, right - left - 2),
                _FILMSTRIP_HEIGHT,
            )
            pixmap = self._pixmaps.get(timestamp)
            if pixmap is None:
                painter.fillRect(tile, QColor("#30383D"))
            else:
                painter.drawPixmap(tile.toRect(), pixmap)

    def _paint_excluded_ranges(
        self, painter: QPainter, geometry: InteractionGeometry, timeline: QRectF
    ) -> None:
        selected = _qt_rect(geometry.visual["range"])
        painter.fillRect(
            QRectF(
                timeline.left(),
                timeline.top(),
                selected.left() - timeline.left(),
                timeline.height(),
            ),
            QColor(0, 0, 0, 105),
        )
        painter.fillRect(
            QRectF(
                selected.right(),
                timeline.top(),
                timeline.right() - selected.right(),
                timeline.height(),
            ),
            QColor(0, 0, 0, 105),
        )

    def _paint_range_controls(
        self, painter: QPainter, geometry: InteractionGeometry
    ) -> None:
        selected = _qt_rect(geometry.visual["range"])
        painter.setPen(QPen(QColor("#8AE6A2"), 2))
        painter.drawRect(selected.adjusted(0, 1, 0, -1))
        self._paint_handle(painter, geometry.visual["start_handle"], "IN")
        self._paint_handle(painter, geometry.visual["end_handle"], "OUT")
        painter.fillRect(_qt_rect(geometry.visual["playhead"]), QColor("#F5D76E"))

    def _paint_focus(
        self, painter: QPainter, geometry: InteractionGeometry
    ) -> None:
        painter.setPen(QPen(QColor("#8AE6A2"), 2))
        painter.drawRect(
            _qt_rect(geometry.focus["timeline"]).adjusted(2, 2, -2, -2)
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        target = self._hit(event.position())
        if target in {"start_handle", "end_handle", "playhead", "range", "timeline"}:
            self._dragged = target
            self._rebuild_geometry(dragged=target)
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._emit_position(event.position(), target)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragged is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self._emit_position(event.position(), self._dragged)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragged is not None:
            self._dragged = None
            self._rebuild_geometry()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self.hasFocus():
            super().keyPressEvent(event)
            return
        modifiers = event.modifiers()
        if event.key() in {Qt.Key.Key_Left, Qt.Key.Key_Right}:
            delta = -1 if event.key() == Qt.Key.Key_Left else 1
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                delta *= _LARGE_FRAME_STEP
            self.command_requested.emit(StepFrame(delta))
            event.accept()
            return
        if not modifiers & Qt.KeyboardModifier.ControlModifier:
            if event.key() == Qt.Key.Key_I:
                self.command_requested.emit(SetStartToPlayhead())
                event.accept()
                return
            if event.key() == Qt.Key.Key_O:
                self.command_requested.emit(SetEndToPlayhead())
                event.accept()
                return
        super().keyPressEvent(event)

    def shutdown(self) -> None:
        self._cancel_thumbnails()
        for thread, _worker in tuple(self._thumbnail_threads):
            thread.wait(_THREAD_SHUTDOWN_TIMEOUT_MS)
        self._thumbnail_threads.clear()
        self._thumbnail_thread = None
        self._thumbnail_worker = None

    def _emit_position(self, position: QPointF, target: str) -> None:
        geometry = self._geometry
        if geometry is None:
            return
        point = geometry.widget_to_source(PointF(position.x(), position.y()))
        timestamp = Fraction(point.x).limit_denominator(1_000_000)
        if target == "start_handle":
            self.command_requested.emit(StartChanged(timestamp))
        elif target == "end_handle":
            self.command_requested.emit(EndChanged(timestamp))
        else:
            self.command_requested.emit(PlayheadChanged(timestamp))

    def _hit(self, position: QPointF) -> str | None:
        if self._geometry is None:
            return None
        return self._geometry.hit_test(PointF(position.x(), position.y()))

    def _rebuild_geometry(
        self, *, focused: str | None = None, dragged: str | None = None
    ) -> None:
        if self._presentation is None:
            self._geometry = None
            return
        state = self._presentation.state
        try:
            self._geometry = build_timeline_geometry(
                state=TimelineGeometryState(
                    state.duration,
                    state.start,
                    state.end,
                    state.playhead,
                    fps=state.fps,
                    focused=(
                        focused
                        if focused is not None
                        else ("playhead" if self.hasFocus() else None)
                    ),
                    dragged=dragged if dragged is not None else self._dragged,
                ),
                viewport=SizeF(max(1, self.width()), max(1, self.height())),
                dpr=float(self.devicePixelRatioF()),
            )
        except ValueError:
            self._geometry = None

    def _update_telemetry(self) -> None:
        presentation = self._presentation
        if presentation is None:
            self.timecode_label.setText("00:00:00.000")
            self.frame_label.setText("Frame —")
            self.range_label.setText("IN —   OUT —")
            self.range_status_label.clear()
            return
        state = presentation.state
        self.timecode_label.setText(format_timecode(state.playhead))
        self.frame_label.setText(
            f"Frame {absolute_frame_number(state.playhead, state.source_fps):06d}"
        )
        self.range_label.setText(
            f"IN {format_timecode(state.start)}   OUT {format_timecode(state.end)}"
        )
        outside = not state.start <= state.playhead < state.end
        self.range_status_label.setText("Outside export range" if outside else "")
        self.setAccessibleDescription(
            f"Preview frame playhead {self.timecode_label.text()}, "
            f"{self.frame_label.text()}; {self.range_label.text()}"
            + ("; Outside export range" if outside else "")
        )

    def _thumbnail_layout_signature(
        self, presentation: TimelinePresentation
    ) -> tuple[object, ...]:
        count = len(
            filmstrip_timestamps(presentation.state.duration, max(1, self.width()))
        )
        dpr = round(float(self.devicePixelRatioF()), 4)
        return (presentation.source_id, self.width(), count, dpr)

    def _start_thumbnails(
        self, presentation: TimelinePresentation, signature: tuple[object, ...]
    ) -> None:
        if not isinstance(presentation.source_revision, SourceRevision):
            self._thumbnail_signature = signature
            return
        self._cancel_thumbnails()
        self._thumbnail_generation += 1
        self._pixmaps.clear()
        times = filmstrip_timestamps(presentation.state.duration, max(1, self.width()))
        count = max(1, len(times))
        batch = _ThumbnailBatch(
            presentation.source_id,
            presentation.source,
            presentation.state.duration,
            (max(1.0, self.width() / count), float(_FILMSTRIP_HEIGHT)),
            float(self.devicePixelRatioF()),
            self._thumbnail_generation,
            presentation.source_revision,
            presentation.validation_proof
            if isinstance(presentation.validation_proof, SourceValidationProof)
            else None,
            times,
        )
        self._thumbnail_times = times
        self._thumbnail_signature = signature
        worker = ThumbnailWorker(batch)
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.result.connect(self._thumbnail_ready)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(
            lambda thread=thread: self._thumbnail_thread_finished(thread)
        )
        self._thumbnail_threads.append((thread, worker))
        self._thumbnail_worker = worker
        self._thumbnail_thread = thread
        thread.started.connect(worker.run)
        thread.start()

    @Slot(object)
    def _thumbnail_ready(self, value: object) -> None:
        if not isinstance(value, ThumbnailResult) or self._presentation is None:
            return
        request = value.request
        current = self._presentation
        if (
            request.source_id != current.source_id
            or request.generation != self._thumbnail_generation
            or request.source_revision != current.source_revision
            or request.timestamp not in self._thumbnail_times
        ):
            return
        key = ThumbnailCacheKey(
            request.source_id,
            request.source_fingerprint,
            request.timestamp,
            request.physical_dimensions,
            request.generation,
            request.source_revision,
        )
        pixmap = self._pixmap_cache.put(
            key,
            value,
            current_source_id=current.source_id,
            current_generation=self._thumbnail_generation,
            current_fingerprint=request.source_fingerprint,
            current_revision=request.source_revision,
        )
        if pixmap is not None:
            self._pixmaps[request.timestamp] = pixmap
            self.update()

    def _cancel_thumbnails(self) -> None:
        for thread, _worker in self._thumbnail_threads:
            thread.requestInterruption()
            thread.quit()

    def _thumbnail_thread_finished(self, thread: QThread) -> None:
        self._thumbnail_threads = [
            (current, worker)
            for current, worker in self._thumbnail_threads
            if current is not thread
        ]
        if self._thumbnail_thread is thread:
            self._thumbnail_thread = None
            self._thumbnail_worker = None

    def _paint_handle(self, painter: QPainter, rect: RectF, label: str) -> None:
        painter.fillRect(_qt_rect(rect), QColor("#8AE6A2"))
        painter.setPen(QColor("#122018"))
        painter.drawText(_qt_rect(rect), Qt.AlignmentFlag.AlignCenter, label)

    def _paint_ruler(self, painter: QPainter, geometry: InteractionGeometry) -> None:
        if self._presentation is None:
            return
        painter.setPen(QPen(QColor("#68747B"), 1))
        duration = self._presentation.state.duration
        for index in range(5):
            timestamp = duration * index / 4
            x = geometry.source_to_widget(PointF(float(timestamp), 0)).x
            painter.drawLine(int(x), 104, int(x), 110)

def _qt_rect(rect: RectF) -> QRectF:
    return QRectF(rect.x, rect.y, rect.width, rect.height)


def _provisional_fingerprint(path: Path) -> str:
    from rembggui.core.fingerprints import provisional_source_fingerprint

    return provisional_source_fingerprint(path)


def _thumbnail_disk_cache() -> ThumbnailDiskCache | None:
    try:
        return ThumbnailDiskCache()
    except BaseException:
        return None


def _cache_thumbnail(
    cache: ThumbnailDiskCache, key: ThumbnailCacheKey, result: ThumbnailResult
) -> None:
    try:
        cache.put(
            key,
            result,
            current_source_id=key.source_id,
            current_generation=key.generation,
            current_fingerprint=key.source_fingerprint,
            current_revision=key.source_revision,
        )
    except BaseException:
        # A cache write failure must not hide an otherwise usable thumbnail.
        return


def _cache_request(request: ThumbnailRequest) -> ThumbnailRequest:
    """Make a stable disk identity while retaining the live request identity."""
    return ThumbnailRequest(
        request.source_fingerprint,
        request.timestamp,
        request.logical_size,
        request.dpr,
        0,
        source_fingerprint=request.source_fingerprint,
        source_revision=request.source_revision,
        validation_proof=request.validation_proof,
    )


def _thumbnail_cache_key(request: ThumbnailRequest) -> ThumbnailCacheKey:
    return ThumbnailCacheKey(
        request.source_id,
        request.source_fingerprint,
        request.timestamp,
        request.physical_dimensions,
        request.generation,
        request.source_revision,
    )


def _cached_thumbnail(
    cache: ThumbnailDiskCache | None,
    key: ThumbnailCacheKey,
    request: ThumbnailRequest,
) -> QImage | None:
    if cache is None:
        return None
    try:
        return cache.get(
            key,
            current_source_id=request.source_id,
            current_generation=request.generation,
            current_fingerprint=request.source_fingerprint,
            current_revision=request.source_revision,
        )
    except BaseException:
        return None


def _thumbnail_edge(
    timestamps: tuple[Fraction, ...],
    index: int,
    duration: Fraction,
    *,
    left: bool,
) -> Fraction:
    timestamp = timestamps[index]
    if left:
        return (
            Fraction(0)
            if index == 0
            else (timestamps[index - 1] + timestamp) / 2
        )
    return (
        duration
        if index + 1 == len(timestamps)
        else (timestamp + timestamps[index + 1]) / 2
    )


def _qimage_from_pillow(image: Image.Image) -> QImage:
    rgba = image.convert("RGBA")
    raw = rgba.tobytes()
    return QImage(
        raw,
        rgba.width,
        rgba.height,
        rgba.width * 4,
        QImage.Format.Format_RGBA8888,
    ).copy()
