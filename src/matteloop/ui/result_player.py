"""Looping player for a finished cut's transformed frames (Stage C, D7).

Reuses ``CropCanvas``'s crop-editing machinery -- its two ``_constrain`` /
``_crop_event`` hooks (task C1) -- for the "Edit crop" overlay, so a drag or
an arrow-key nudge on this canvas dispatches ``TransformChanged`` and never
the source ``CropChanged`` a plain ``CropCanvas`` emits (edge case E30).

Playback never calls segmentation or the encoder (AC 9): both frame caches
are plain Pillow images produced by ``core.geometry.apply_framing`` and
``core.transform.apply_transform`` -- the same two functions the encoder
calls, on the encoder's inputs (design rule, ``matteloop-desktop-app.md:33``)
-- run on a worker thread owned by ``TransformStageController`` and handed to
this canvas as display-scaled ``QImage``s. Memory is bounded by
``PLAYER_CACHE_BUDGET_BYTES``: the display size shrinks first: only once the
64 px floor is hit does the cache truncate to the first N stored frames
(E32), which this canvas surfaces as a status marker.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Protocol

from PIL import Image, UnidentifiedImageError
from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QPushButton, QWidget

from matteloop.core.crop import fit_crop_aspect
from matteloop.core.geometry import _MEDIA_INSET, FramingPlan, apply_framing
from matteloop.core.parameters import TransformChanged
from matteloop.core.specs import CropSpec, TransformSpec
from matteloop.core.transform import apply_transform
from matteloop.jobs.workspace import CutFrame, CutManifest, CutWorkspace
from matteloop.ui.crop_canvas import CropCanvas
from matteloop.ui.crop_presentation import CropPresentation

PLAYER_CACHE_BUDGET_BYTES = 128 * 1024 * 1024
PLAYER_MIN_DISPLAY_SIDE = 64
_PLAY_BUTTON_CLEARANCE_GAP = 8.0

_PLAY_TEXT = "Play"
_PLAY_ACCESSIBLE_NAME = "Play the result loop"
_PAUSE_TEXT = "Pause"
_PAUSE_ACCESSIBLE_NAME = "Pause the result loop"


class _FrameLoadCancelled(Exception):
    """Raised internally to unwind ``_load_player_frames`` once superseded."""


def _no_cancellation() -> bool:
    return False


class FrameReader(Protocol):
    """Read one stored cut frame directly by manifest filename."""

    def read(self, workspace: CutWorkspace, frame: CutFrame) -> Image.Image: ...


@dataclass(frozen=True)
class PlayerFrames:
    """Two display-scaled frame caches sharing one identity key."""

    key: object
    framed: tuple[QImage, ...]
    transformed: tuple[QImage, ...] | None
    delays_ms: tuple[int, ...]
    cached: int
    frame_count: int


class ResultPlayerCanvas(CropCanvas):
    """Loop a cut's framed/transformed frames; edit its output crop in place."""

    playhead_changed = Signal(int)

    def __init__(
        self, parent: QWidget | None = None, *, runtime_root: Path | None = None
    ) -> None:
        super().__init__(
            parent,
            title="Result",
            object_name="result_canvas",
            runtime_root=runtime_root,
        )
        self._frames: PlayerFrames | None = None
        self._kept: range | None = None
        self._crop_edit = False
        self._aspect_lock: Fraction | None = None
        self._transform = TransformSpec()
        self._playhead_index: int | None = None
        self._playing = False
        self._cover_before_session: bool | None = None
        self._last_preview_image: QImage | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._advance)
        self.play_button = QPushButton("Play", self)
        self.play_button.setObjectName("result_play")
        self.play_button.setAccessibleName(_PLAY_ACCESSIBLE_NAME)
        self.play_button.setCheckable(True)
        self.play_button.hide()
        self.play_button.toggled.connect(self._play_toggled)
        layout = self.layout()
        assert layout is not None
        layout.addWidget(self.play_button)
        layout.setAlignment(
            self.play_button, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight
        )

    # -- public surface ----------------------------------------------------

    def set_frames(self, frames: PlayerFrames | None) -> None:
        """Replace the cached frames; ``None`` closes the session (E31).

        A crop/resize edit rebuilds the cache while the session stays open;
        pausing unconditionally here silently stopped a loop that was
        already playing, since nothing ever resumes it but the Play button.
        Only closing the session (``frames is None``) forces a pause. The
        kept range is preserved across such a swap too -- resetting it would
        drop a trim ``set_kept_range`` just applied moments earlier.
        """
        had_session = self._frames is not None
        if frames is None:
            self.pause()
            if had_session and self._cover_before_session is not None:
                self.set_cover_frame(self._cover_before_session)
                self._cover_before_session = None
            self._frames = None
            self._kept = None
            self.play_button.hide()
            self._playhead_index = None
            self.set_status_marker(None)
            self.set_frame(None)
            self._rebuild_geometry()
            self.update()
            return
        if not had_session:
            self._cover_before_session = self._cover_frame
            self.set_cover_frame(False)
            self._kept = None
        self._frames = frames
        self.play_button.show()
        self._rebuild_geometry()
        playable = self._playable()
        self._playhead_index = playable.start if playable else None
        self._update_truncation_marker(frames)
        self._show_current_frame()
        self.update()

    def set_kept_range(self, kept: range) -> None:
        """Re-slice playback to *kept* without reloading either cache."""
        self._kept = kept
        if self._frames is None:
            return
        playable = self._playable()
        if self._playhead_index is None or self._playhead_index not in playable:
            self._playhead_index = playable.start if playable else None
        self._show_current_frame()

    def set_crop_edit(
        self,
        enabled: bool,
        presentation: CropPresentation | None,
        transform: TransformSpec | None = None,
    ) -> None:
        """Toggle between the crop overlay (framed frames) and the result loop.

        ``_reserved_bottom_space`` depends on ``_crop_edit`` (#25: the
        reservation only buys anything while the crop handles exist to be
        covered), so every toggle can change it and both the crop geometry
        and the displayed media need recomputing either way -- not just when
        entering, where ``apply_presentation`` already calls
        ``_rebuild_geometry`` for the fresh presentation. Leaving passes
        ``presentation=None``, which ``apply_presentation`` handles by
        clearing ``_geometry`` directly rather than through
        ``_rebuild_geometry``, so it is re-run explicitly here on both edges.
        """
        self._crop_edit = enabled
        self._transform = transform if transform is not None else TransformSpec()
        self.apply_presentation(presentation, active=enabled, editable=enabled)
        self._rebuild_geometry()
        self._show_current_frame()
        self.update()

    def set_aspect_lock(self, ratio: Fraction | None) -> None:
        self._aspect_lock = ratio

    def play(self) -> None:
        if self._frames is None or not self._playable():
            return
        self._playing = True
        self._sync_play_button(True)
        self._schedule_next()

    def pause(self) -> None:
        self._playing = False
        self._timer.stop()
        self._sync_play_button(False)

    def _sync_play_button(self, playing: bool) -> None:
        """Keep the button's checked state, text, and accessible name in step.

        Text is the reliable signal that the loop is running: the theme has
        no ``QPushButton:checked`` rule, so the checked visual alone is easy
        to miss on a flat style.
        """
        if self.play_button.isChecked() != playing:
            self.play_button.setChecked(playing)
        self.play_button.setText(_PAUSE_TEXT if playing else _PLAY_TEXT)
        self.play_button.setAccessibleName(
            _PAUSE_ACCESSIBLE_NAME if playing else _PLAY_ACCESSIBLE_NAME
        )

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def current_frame(self) -> int | None:
        """The stored-cut frame index currently displayed, if any."""
        return self._playhead_index

    def set_presented_frame(self, image: object, placeholder: str) -> None:
        """Yield to a genuinely new preview image; ignore a repeated one (E26)."""
        if (
            self._frames is not None
            and isinstance(image, QImage)
            and not image.isNull()
        ):
            if image is self._last_preview_image:
                return
            self._last_preview_image = image
            self.pause()
            super().set_presented_frame(image, placeholder)
            return
        self._last_preview_image = image if isinstance(image, QImage) else None
        super().set_presented_frame(image, placeholder)

    # -- CropCanvas reuse hooks (C1) -----------------------------------------

    def _constrain(self, crop: CropSpec, target: str) -> CropSpec:
        if self._aspect_lock is None or self._presentation is None:
            return crop
        return fit_crop_aspect(
            crop,
            self._aspect_lock,
            target,
            source_width=self._presentation.width,
            source_height=self._presentation.height,
        )

    def _crop_event(self, crop: CropSpec) -> object:
        return TransformChanged(replace(self._transform, crop=crop))

    def _should_paint_checkerboard(self) -> bool:
        """Show the checkerboard whenever a player session is holding frames.

        A Render can open the transform stage without a Preview ever having
        run, so ``checkerboard`` (a proxy for ``PreviewState``) can be false
        while the loop plays a transparent cut-out over flat grey. The player
        itself always knows whether it has frames to show, independent of
        preview state.
        """
        return super()._should_paint_checkerboard() or self._frames is not None

    def _reserved_bottom_space(self) -> float:
        """Keep the play button's footprint clear of the crop handles (#25)
        while crop-edit is active: the button is a plain layout child
        aligned to the bottom-right corner, so without this the crop
        geometry built against the media is centred straight through it, and
        a drag starting on the covered half of the bottom-right handle never
        reaches ``mousePressEvent`` -- the button consumes the click first.

        Gated on ``_crop_edit``, not just button visibility: the handles
        this protects only exist while the crop overlay is active
        (``CropCanvas.paintEvent`` returns early otherwise, and there is
        nothing to hit-test), so outside crop-edit the reservation would
        only shrink the visible loop for nothing.

        The button sits ``_MEDIA_INSET`` above the widget's bottom edge (the
        layout margin ``_rebuild_geometry`` applies), on top of its own
        height. Pointer/touch hit targets are expanded right up to whatever
        viewport they are given (by design -- it is what keeps a crop
        dragged flush with the true canvas edge reachable), so reserving
        only the button's height would still let an expanded hit target
        reach past the button's top edge; folding the same inset in here
        keeps that expansion inside the button's actual top edge too.
        """
        if not self._crop_edit or not self.play_button.isVisible():
            return 0.0
        return (
            _MEDIA_INSET
            + self.play_button.sizeHint().height()
            + _PLAY_BUTTON_CLEARANCE_GAP
        )

    # -- playback internals ---------------------------------------------

    def _play_toggled(self, checked: bool) -> None:
        if checked:
            self.play()
        else:
            self.pause()

    def _playable(self) -> range:
        frames = self._frames
        if frames is None:
            return range(0)
        cached = frames.cached
        kept = self._kept
        start = 0 if kept is None else max(kept.start, 0)
        stop = cached if kept is None else min(kept.stop, cached)
        return range(start, stop) if start < stop else range(0)

    def _current_images(self) -> tuple[QImage, ...]:
        frames = self._frames
        assert frames is not None
        if self._crop_edit or frames.transformed is None:
            return frames.framed
        return frames.transformed

    def _update_truncation_marker(self, frames: PlayerFrames) -> None:
        if frames.cached < frames.frame_count:
            self.set_status_marker(
                f"Previewing the first {frames.cached} of {frames.frame_count} frames"
            )
        else:
            self.set_status_marker(None)

    def _show_current_frame(self) -> None:
        index = self._playhead_index
        if self._frames is None or index is None:
            self.set_frame(None)
            return
        images = self._current_images()
        if 0 <= index < len(images):
            self.set_frame(images[index])
        self.playhead_changed.emit(index)

    def _schedule_next(self) -> None:
        frames = self._frames
        index = self._playhead_index
        if not self._playing or frames is None or index is None:
            return
        delay = frames.delays_ms[index] if index < len(frames.delays_ms) else 100
        self._timer.start(max(1, delay))

    def _advance(self) -> None:
        playable = self._playable()
        if not playable or self._playhead_index is None:
            self.pause()
            return
        next_index = self._playhead_index + 1
        if next_index >= playable.stop:
            next_index = playable.start
        self._playhead_index = next_index
        self._show_current_frame()
        self._schedule_next()


class FrameLoadWorker(QObject):
    """Build one ``PlayerFrames`` off the GUI thread (decoding is not free)."""

    succeeded = Signal(object, int)
    failed = Signal(str, int)
    finished = Signal()

    def __init__(
        self,
        workspace: CutWorkspace,
        manifest: CutManifest,
        frame_reader: FrameReader,
        plan: FramingPlan,
        transform: TransformSpec,
        delays: tuple[int, ...],
        generation: int,
        *,
        budget: int = PLAYER_CACHE_BUDGET_BYTES,
        cancelled: Callable[[], bool] = _no_cancellation,
    ) -> None:
        super().__init__()
        self._workspace = workspace
        self._manifest = manifest
        self._frame_reader = frame_reader
        self._plan = plan
        self._transform = transform
        self._delays = delays
        self._generation = generation
        self._budget = budget
        self._cancelled = cancelled

    @Slot()
    def run(self) -> None:
        try:
            frames = _load_player_frames(
                self._workspace,
                self._manifest,
                self._frame_reader,
                self._plan,
                self._transform,
                self._delays,
                self._budget,
                self._cancelled,
            )
        except _FrameLoadCancelled:
            pass
        except (OSError, UnidentifiedImageError):
            self.failed.emit("Cut frames could not be read", self._generation)
        else:
            self.succeeded.emit(frames, self._generation)
        finally:
            self.finished.emit()


def _load_player_frames(
    workspace: CutWorkspace,
    manifest: CutManifest,
    frame_reader: FrameReader,
    plan: FramingPlan,
    transform: TransformSpec,
    delays: tuple[int, ...],
    budget: int,
    cancelled: Callable[[], bool] = _no_cancellation,
) -> PlayerFrames:
    frame_count = manifest.frame_count
    display_size, cached = _fit_budget(frame_count, plan.output_size, budget)
    key = (
        manifest.cache_key,
        plan.output_size,
        transform.crop,
        transform.resize,
        display_size,
    )
    framed_images: list[QImage] = []
    transformed_images: list[QImage] = []
    for index in range(cached):
        if cancelled():
            raise _FrameLoadCancelled
        framed_qimage, transformed_qimage = _load_one_frame(
            workspace,
            manifest.frames[index],
            frame_reader,
            plan,
            transform,
            display_size,
        )
        framed_images.append(framed_qimage)
        transformed_images.append(transformed_qimage)
    return PlayerFrames(
        key=key,
        framed=tuple(framed_images),
        transformed=tuple(transformed_images),
        delays_ms=delays,
        cached=cached,
        frame_count=frame_count,
    )


def _load_one_frame(
    workspace: CutWorkspace,
    frame: CutFrame,
    frame_reader: FrameReader,
    plan: FramingPlan,
    transform: TransformSpec,
    display_size: tuple[int, int],
) -> tuple[QImage, QImage]:
    cut = frame_reader.read(workspace, frame)
    try:
        framed = apply_framing(cut, plan)
    finally:
        cut.close()
    try:
        framed_qimage = _scaled_qimage(framed, display_size)
        transformed = apply_transform(framed, transform)
        try:
            transformed_qimage = _scaled_qimage(transformed, display_size)
        finally:
            if transformed is not framed:
                transformed.close()
    finally:
        framed.close()
    return framed_qimage, transformed_qimage


def _fit_budget(
    frame_count: int, base_size: tuple[int, int], budget: int
) -> tuple[tuple[int, int], int]:
    """Shrink the display size (aspect kept) to fit *budget*; below the
    minimum short side, truncate the cached frame count instead (E32)."""
    width, height = base_size
    if frame_count <= 0 or width <= 0 or height <= 0:
        return (max(1, width), max(1, height)), 0
    per_frame_limit = budget / (2 * frame_count * 4)
    scale = min(1.0, math.sqrt(per_frame_limit / (width * height)))
    display_width = max(1, math.floor(width * scale))
    display_height = max(1, math.floor(height * scale))
    if min(display_width, display_height) >= PLAYER_MIN_DISPLAY_SIDE:
        return (display_width, display_height), frame_count
    if width <= height:
        display_width = PLAYER_MIN_DISPLAY_SIDE
        display_height = max(1, round(height * display_width / width))
    else:
        display_height = PLAYER_MIN_DISPLAY_SIDE
        display_width = max(1, round(width * display_height / height))
    per_frame_bytes = 2 * display_width * display_height * 4
    cached = max(1, min(frame_count, budget // per_frame_bytes))
    return (display_width, display_height), cached


def _scaled_qimage(image: Image.Image, display_size: tuple[int, int]) -> QImage:
    return _qimage_from_pillow(image).scaled(
        QSize(*display_size),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _qimage_from_pillow(image: Image.Image) -> QImage:
    rgba = image.convert("RGBA")
    raw = rgba.tobytes()
    return QImage(
        raw, rgba.width, rgba.height, rgba.width * 4, QImage.Format.Format_RGBA8888
    ).copy()
