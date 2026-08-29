"""Immutable, Qt-free interaction geometry and RGBA framing algorithms.

Source rectangles use oriented source coordinates. Pixel bounds are integral and
half-open: ``left <= x < right`` and ``top <= y < bottom``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Context, Decimal, localcontext
from fractions import Fraction
from types import MappingProxyType
from typing import Protocol, Self

from PIL import Image

from rembggui.core.errors import ErrorCode, ValidationError
from rembggui.core.specs import FramingSpec

_ROTATIONS = frozenset({0, 90, 180, 270})
_MAX_IMAGE_ALLOCATION_BYTES = 1024 * 1024 * 1024
_CROP_HANDLES = (
    "north_west",
    "north",
    "north_east",
    "east",
    "south_east",
    "south",
    "south_west",
    "west",
)


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be a finite number")
    return converted


@dataclass(frozen=True)
class PointF:
    x: float
    y: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _finite(self.x, "x"))
        object.__setattr__(self, "y", _finite(self.y, "y"))


@dataclass(frozen=True)
class SizeF:
    width: float
    height: float

    def __post_init__(self) -> None:
        width = _finite(self.width, "width")
        height = _finite(self.height, "height")
        if width <= 0 or height <= 0:
            raise ValueError("width and height must be positive")
        object.__setattr__(self, "width", width)
        object.__setattr__(self, "height", height)


@dataclass(frozen=True)
class RectF:
    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        x = _finite(self.x, "x")
        y = _finite(self.y, "y")
        width = _finite(self.width, "width")
        height = _finite(self.height, "height")
        if width < 0 or height < 0:
            raise ValueError("rectangle dimensions must be non-negative")
        object.__setattr__(self, "x", x)
        object.__setattr__(self, "y", y)
        object.__setattr__(self, "width", width)
        object.__setattr__(self, "height", height)

    @property
    def left(self) -> float:
        return self.x

    @property
    def top(self) -> float:
        return self.y

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    def center(self) -> PointF:
        return PointF(self.x + self.width / 2, self.y + self.height / 2)

    def size(self) -> SizeF:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("an empty rectangle has no positive SizeF")
        return SizeF(self.width, self.height)

    def contains(self, value: PointF | RectF) -> bool:
        if isinstance(value, PointF):
            return (
                self.left <= value.x <= self.right
                and self.top <= value.y <= self.bottom
            )
        return (
            self.left <= value.left
            and self.top <= value.top
            and value.right <= self.right
            and value.bottom <= self.bottom
        )

    def expanded_to_minimum(self, width: float, height: float) -> RectF:
        wanted_width = max(self.width, _finite(width, "width"))
        wanted_height = max(self.height, _finite(height, "height"))
        center = self.center()
        return RectF(
            center.x - wanted_width / 2,
            center.y - wanted_height / 2,
            wanted_width,
            wanted_height,
        )


@dataclass(frozen=True)
class PixelBounds:
    """Exact, non-empty integral bounds for Pillow/numpy pixel slicing."""

    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        values = (self.left, self.top, self.right, self.bottom)
        if any(
            not isinstance(value, int) or isinstance(value, bool) for value in values
        ):
            raise ValueError("pixel bounds must be integers")
        if self.left < 0 or self.top < 0:
            raise ValueError("pixel bounds origin must be non-negative")
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("pixel bounds must be non-empty and half-open")

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @classmethod
    def from_xywh(cls, x: int, y: int, width: int, height: int) -> Self:
        if (
            not isinstance(width, int)
            or not isinstance(height, int)
            or isinstance(width, bool)
            or isinstance(height, bool)
        ):
            raise ValueError("pixel dimensions must be integers")
        return cls(x, y, x + width, y + height)


@dataclass(frozen=True)
class FramingPlan:
    """Validated immutable instructions for processing one RGBA frame at a time."""

    source_size: tuple[int, int]
    global_bounds: PixelBounds | None = None
    padding: int = 0
    stretch_x: Fraction | Decimal | float | int = Fraction(1)
    output_size: tuple[int, int] = field(init=False)

    def __post_init__(self) -> None:
        source_size = _integer_size(self.source_size, "source canvas")
        _validate_allocation_budget(source_size, 4, "source canvas")
        if not isinstance(self.padding, int) or isinstance(self.padding, bool):
            raise ValidationError(
                ErrorCode.INVALID_FRAMING,
                "framing",
                "padding must be a non-negative integer",
            )
        if self.padding < 0:
            raise ValidationError(
                ErrorCode.INVALID_FRAMING,
                "framing",
                "padding must be a non-negative integer",
            )
        stretch = _positive_fraction(self.stretch_x, "horizontal stretch")
        if self.global_bounds is not None:
            _validate_contained_bounds(self.global_bounds, source_size)
            content_width = self.global_bounds.width
            content_height = self.global_bounds.height
        else:
            content_width, content_height = source_size
        padded_width = content_width + 2 * self.padding
        padded_height = content_height + 2 * self.padding
        _validate_allocation_budget(
            (content_width, content_height), 4, "cropped/premultiplied working image"
        )
        _validate_allocation_budget(
            (padded_width, padded_height), 4, "padded working image"
        )
        output_width = _round_positive_fraction(padded_width * stretch)
        output_size = FramingSpec().validate_final_dimensions(
            output_width, padded_height
        )
        _validate_allocation_budget(output_size, 4, "framed output")
        object.__setattr__(self, "source_size", source_size)
        object.__setattr__(self, "stretch_x", stretch)
        object.__setattr__(self, "output_size", output_size)

    @property
    def content_bounds(self) -> PixelBounds:
        if self.global_bounds is not None:
            return self.global_bounds
        return PixelBounds(0, 0, self.source_size[0], self.source_size[1])

    @property
    def padded_size(self) -> tuple[int, int]:
        bounds = self.content_bounds
        return (
            bounds.width + 2 * self.padding,
            bounds.height + 2 * self.padding,
        )


class CoordinateTransform(Protocol):
    @property
    def dpr(self) -> float: ...

    def source_to_widget(self, point: PointF) -> PointF: ...

    def widget_to_source(self, point: PointF) -> PointF: ...

    def widget_to_screen(self, point: PointF) -> PointF: ...

    def screen_to_widget(self, point: PointF) -> PointF: ...

    def widget_rect_to_screen(self, rect: RectF) -> RectF: ...


@dataclass(frozen=True)
class MediaTransform:
    """One reversible oriented-source to logical-widget/screen transform."""

    source_size: SizeF
    viewport: SizeF
    rotation: int = 0
    pixel_aspect: float = 1.0
    zoom: float = 1.0
    pan: PointF = field(default_factory=lambda: PointF(0, 0))
    screen_origin: PointF = field(default_factory=lambda: PointF(0, 0))
    dpr: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.source_size, SizeF) or not isinstance(
            self.viewport, SizeF
        ):
            raise ValueError("source_size and viewport must be SizeF values")
        if self.rotation not in _ROTATIONS:
            raise ValueError("rotation must be 0, 90, 180, or 270")
        pixel_aspect = _finite(self.pixel_aspect, "pixel_aspect")
        zoom = _finite(self.zoom, "zoom")
        dpr = _finite(self.dpr, "dpr")
        if pixel_aspect <= 0:
            raise ValueError("pixel_aspect must be positive")
        if zoom <= 0:
            raise ValueError("zoom must be positive")
        if dpr <= 0:
            raise ValueError("dpr must be positive")
        if not isinstance(self.pan, PointF) or not isinstance(
            self.screen_origin, PointF
        ):
            raise ValueError("pan and screen_origin must be PointF values")
        object.__setattr__(self, "pixel_aspect", pixel_aspect)
        object.__setattr__(self, "zoom", zoom)
        object.__setattr__(self, "dpr", dpr)

    @property
    def oriented_display_size(self) -> SizeF:
        if self.rotation in {0, 180}:
            return SizeF(
                self.source_size.width * self.pixel_aspect,
                self.source_size.height,
            )
        return SizeF(
            self.source_size.height,
            self.source_size.width * self.pixel_aspect,
        )

    @property
    def scale(self) -> float:
        display = self.oriented_display_size
        fit = min(
            self.viewport.width / display.width,
            self.viewport.height / display.height,
        )
        return fit * self.zoom

    @property
    def content_rect(self) -> RectF:
        display = self.oriented_display_size
        width = display.width * self.scale
        height = display.height * self.scale
        return RectF(
            (self.viewport.width - width) / 2 + self.pan.x,
            (self.viewport.height - height) / 2 + self.pan.y,
            width,
            height,
        )

    def source_to_widget(self, point: PointF) -> PointF:
        oriented = self._rotate(point)
        content = self.content_rect
        return PointF(
            content.x + oriented.x * self.scale,
            content.y + oriented.y * self.scale,
        )

    def widget_to_source(self, point: PointF) -> PointF:
        content = self.content_rect
        oriented = PointF(
            (point.x - content.x) / self.scale,
            (point.y - content.y) / self.scale,
        )
        return self._unrotate(oriented)

    def source_rect_to_widget(self, rect: RectF) -> RectF:
        return _map_rect(rect, self.source_to_widget)

    def widget_rect_to_source(self, rect: RectF) -> RectF:
        return _map_rect(rect, self.widget_to_source)

    def widget_to_screen(self, point: PointF) -> PointF:
        return PointF(
            (point.x + self.screen_origin.x) * self.dpr,
            (point.y + self.screen_origin.y) * self.dpr,
        )

    def screen_to_widget(self, point: PointF) -> PointF:
        return PointF(
            point.x / self.dpr - self.screen_origin.x,
            point.y / self.dpr - self.screen_origin.y,
        )

    def widget_rect_to_screen(self, rect: RectF) -> RectF:
        return _map_rect(rect, self.widget_to_screen)

    def source_to_screen(self, point: PointF) -> PointF:
        return self.widget_to_screen(self.source_to_widget(point))

    def screen_to_source(self, point: PointF) -> PointF:
        return self.widget_to_source(self.screen_to_widget(point))

    def clamp_source_point(self, point: PointF) -> PointF:
        return PointF(
            min(max(point.x, 0), self.source_size.width),
            min(max(point.y, 0), self.source_size.height),
        )

    def clamp_source_rect(self, rect: RectF) -> RectF:
        width = min(rect.width, self.source_size.width)
        height = min(rect.height, self.source_size.height)
        return RectF(
            min(max(rect.x, 0), self.source_size.width - width),
            min(max(rect.y, 0), self.source_size.height - height),
            width,
            height,
        )

    def _rotate(self, point: PointF) -> PointF:
        width = self.source_size.width
        height = self.source_size.height
        aspect = self.pixel_aspect
        if self.rotation == 0:
            return PointF(point.x * aspect, point.y)
        if self.rotation == 90:
            return PointF(height - point.y, point.x * aspect)
        if self.rotation == 180:
            return PointF((width - point.x) * aspect, height - point.y)
        return PointF(point.y, (width - point.x) * aspect)

    def _unrotate(self, point: PointF) -> PointF:
        width = self.source_size.width
        height = self.source_size.height
        aspect = self.pixel_aspect
        if self.rotation == 0:
            return PointF(point.x / aspect, point.y)
        if self.rotation == 90:
            return PointF(point.y / aspect, height - point.x)
        if self.rotation == 180:
            return PointF(width - point.x / aspect, height - point.y)
        return PointF(width - point.y / aspect, point.x)


@dataclass(frozen=True)
class CropGeometryState:
    source_size: SizeF
    crop: RectF
    rotation: int = 0
    pixel_aspect: float = 1.0
    zoom: float = 1.0
    pan: PointF = field(default_factory=lambda: PointF(0, 0))
    screen_origin: PointF = field(default_factory=lambda: PointF(0, 0))
    focused: str | None = None
    dragged: str | None = None


@dataclass(frozen=True)
class TimelineGeometryState:
    duration: Fraction | Decimal
    start: Fraction | Decimal
    end: Fraction | Decimal
    playhead: Fraction | Decimal
    fps: int = 15
    screen_origin: PointF = field(default_factory=lambda: PointF(0, 0))
    focused: str | None = None
    dragged: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.screen_origin, PointF):
            raise ValueError("screen_origin must be a frozen PointF")
        if (
            not isinstance(self.fps, int)
            or isinstance(self.fps, bool)
            or not 1 <= self.fps <= 240
        ):
            raise ValueError("fps must be an integer between 1 and 240")
        duration = _rational_time(self.duration)
        start = _rational_time(self.start)
        end = _rational_time(self.end)
        playhead = _rational_time(self.playhead)
        if not (
            duration > 0
            and Fraction(0) <= start < end <= duration
            and Fraction(0) <= playhead <= duration
        ):
            raise ValueError(
                "timeline values must satisfy 0 <= start < end <= duration"
            )
        if end - start < Fraction(1, self.fps):
            raise ValueError("timeline range must retain at least one output-frame")
        object.__setattr__(self, "duration", duration)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "playhead", playhead)


@dataclass(frozen=True, init=False, slots=True)
class FrozenRectMap(Mapping[str, RectF]):
    """A small immutable mapping used by frozen interaction snapshots."""

    _mapping: Mapping[str, RectF]

    def __init__(self, values: Mapping[str, RectF]) -> None:
        if not isinstance(values, Mapping):
            raise ValueError("rectangle collection must be a mapping")
        copied = dict(values)
        if any(
            not isinstance(name, str) or not isinstance(rect, RectF)
            for name, rect in copied.items()
        ):
            raise ValueError("rectangle mappings require string keys and RectF values")
        object.__setattr__(self, "_mapping", MappingProxyType(copied))

    def __getitem__(self, key: str) -> RectF:
        return self._mapping[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._mapping)

    def __len__(self) -> int:
        return len(self._mapping)


@dataclass(frozen=True)
class InteractionGeometry:
    """Single geometry fan-out for paint, input, focus, and accessibility."""

    visual: FrozenRectMap
    pointer_hit: FrozenRectMap
    touch_hit: FrozenRectMap
    focus: FrozenRectMap
    accessible_screen: FrozenRectMap
    transform: CoordinateTransform
    priority: tuple[str, ...]
    focused: str | None = None
    dragged: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "visual",
            "pointer_hit",
            "touch_hit",
            "focus",
            "accessible_screen",
        ):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise ValueError(f"{name} must be a rectangle mapping")
            object.__setattr__(self, name, FrozenRectMap(value))
        if type(self.transform) not in {MediaTransform, _TimelineTransform}:
            raise ValueError("transform must be a frozen core coordinate transform")
        if isinstance(self.priority, str):
            raise ValueError("priority must be a sequence of target names")
        priority = tuple(self.priority)
        if any(not isinstance(name, str) for name in priority):
            raise ValueError("priority must contain only target names")
        if self.focused is not None and not isinstance(self.focused, str):
            raise ValueError("focused must be a target name or None")
        if self.dragged is not None and not isinstance(self.dragged, str):
            raise ValueError("dragged must be a target name or None")
        object.__setattr__(self, "priority", priority)

    def source_to_widget(self, point: PointF) -> PointF:
        return self.transform.source_to_widget(point)

    def widget_to_source(self, point: PointF) -> PointF:
        return self.transform.widget_to_source(point)

    def widget_to_screen(self, point: PointF) -> PointF:
        return self.transform.widget_to_screen(point)

    def screen_to_widget(self, point: PointF) -> PointF:
        return self.transform.screen_to_widget(point)

    def source_to_screen(self, point: PointF) -> PointF:
        return self.widget_to_screen(self.source_to_widget(point))

    def screen_to_source(self, point: PointF) -> PointF:
        return self.widget_to_source(self.screen_to_widget(point))

    def hit_test(self, point: PointF, *, touch: bool = False) -> str | None:
        regions = self.touch_hit if touch else self.pointer_hit
        order = _unique_names((self.dragged, self.focused, *self.priority))
        for name in order:
            region = regions.get(name)
            if region is not None and region.contains(point):
                return name
        return None


def build_crop_geometry(
    *, state: CropGeometryState, viewport: SizeF, dpr: float
) -> InteractionGeometry:
    """Build all crop presentation and interaction rectangles from one transform."""
    if not isinstance(state, CropGeometryState):
        raise ValueError("state must be a CropGeometryState")
    transform = MediaTransform(
        source_size=state.source_size,
        viewport=viewport,
        rotation=state.rotation,
        pixel_aspect=state.pixel_aspect,
        zoom=state.zoom,
        pan=state.pan,
        screen_origin=state.screen_origin,
        dpr=dpr,
    )
    crop = transform.source_rect_to_widget(transform.clamp_source_rect(state.crop))
    centers = {
        "north_west": PointF(crop.left, crop.top),
        "north": PointF(crop.center().x, crop.top),
        "north_east": PointF(crop.right, crop.top),
        "east": PointF(crop.right, crop.center().y),
        "south_east": PointF(crop.right, crop.bottom),
        "south": PointF(crop.center().x, crop.bottom),
        "south_west": PointF(crop.left, crop.bottom),
        "west": PointF(crop.left, crop.center().y),
    }
    visual_values = {"crop": crop}
    visual_values.update(
        {name: _centered_rect(center, 8, 8) for name, center in centers.items()}
    )
    visual = FrozenRectMap(visual_values)
    pointer = FrozenRectMap(
        {
            name: _effective_target(rect, viewport, 24)
            for name, rect in visual.items()
        }
    )
    touch = FrozenRectMap(
        {
            name: _effective_target(rect, viewport, 44)
            for name, rect in visual.items()
        }
    )
    return InteractionGeometry(
        visual=visual,
        pointer_hit=pointer,
        touch_hit=touch,
        focus=visual,
        accessible_screen=FrozenRectMap(
            {
                name: transform.widget_rect_to_screen(rect)
                for name, rect in touch.items()
            }
        ),
        transform=transform,
        priority=(*_CROP_HANDLES, "crop"),
        focused=state.focused,
        dragged=state.dragged,
    )


@dataclass(frozen=True)
class _TimelineTransform:
    duration: float
    viewport: SizeF
    screen_origin: PointF
    dpr: float
    inset: float = 20.0

    def __post_init__(self) -> None:
        if not isinstance(self.viewport, SizeF):
            raise ValueError("viewport must be a frozen SizeF")
        if not isinstance(self.screen_origin, PointF):
            raise ValueError("screen_origin must be a frozen PointF")
        duration = _finite(self.duration, "duration")
        dpr = _finite(self.dpr, "dpr")
        if duration <= 0 or dpr <= 0:
            raise ValueError("timeline transform dimensions must be positive")
        inset = min(self.inset, self.viewport.width / 4)
        object.__setattr__(self, "duration", duration)
        object.__setattr__(self, "dpr", dpr)
        object.__setattr__(self, "inset", inset)

    @property
    def width(self) -> float:
        return self.viewport.width - 2 * self.inset

    def source_to_widget(self, point: PointF) -> PointF:
        return PointF(self.inset + point.x / self.duration * self.width, point.y)

    def widget_to_source(self, point: PointF) -> PointF:
        value = (point.x - self.inset) / self.width * self.duration
        return PointF(min(max(value, 0), self.duration), point.y)

    def widget_to_screen(self, point: PointF) -> PointF:
        return PointF(
            (point.x + self.screen_origin.x) * self.dpr,
            (point.y + self.screen_origin.y) * self.dpr,
        )

    def screen_to_widget(self, point: PointF) -> PointF:
        return PointF(
            point.x / self.dpr - self.screen_origin.x,
            point.y / self.dpr - self.screen_origin.y,
        )

    def widget_rect_to_screen(self, rect: RectF) -> RectF:
        return _map_rect(rect, self.widget_to_screen)


def build_timeline_geometry(
    *, state: TimelineGeometryState, viewport: SizeF, dpr: float
) -> InteractionGeometry:
    """Build range handles, playhead, and containing timeline geometry."""
    if not isinstance(state, TimelineGeometryState):
        raise ValueError("state must be a TimelineGeometryState")
    transform = _TimelineTransform(
        duration=float(state.duration),
        viewport=viewport,
        screen_origin=state.screen_origin,
        dpr=dpr,
    )
    start_x = transform.source_to_widget(PointF(float(state.start), 0)).x
    end_x = transform.source_to_widget(PointF(float(state.end), 0)).x
    playhead_x = transform.source_to_widget(PointF(float(state.playhead), 0)).x
    visual = FrozenRectMap(
        {
            "timeline": RectF(0, 0, viewport.width, viewport.height),
            "range": RectF(start_x, 0, end_x - start_x, viewport.height),
            "start_handle": _centered_rect(
                PointF(start_x, viewport.height / 2), 12, 32
            ),
            "end_handle": _centered_rect(
                PointF(end_x, viewport.height / 2), 12, 32
            ),
            "playhead": _centered_rect(
                PointF(playhead_x, viewport.height / 2), 2, viewport.height
            ),
        }
    )
    pointer = FrozenRectMap(
        {
            name: _effective_target(rect, viewport, 24)
            for name, rect in visual.items()
        }
    )
    touch = FrozenRectMap(
        {
            name: _effective_target(rect, viewport, 44)
            for name, rect in visual.items()
        }
    )
    return InteractionGeometry(
        visual=visual,
        pointer_hit=pointer,
        touch_hit=touch,
        focus=visual,
        accessible_screen=FrozenRectMap(
            {
                name: transform.widget_rect_to_screen(rect)
                for name, rect in touch.items()
            }
        ),
        transform=transform,
        priority=("start_handle", "end_handle", "playhead", "range", "timeline"),
        focused=state.focused,
        dragged=state.dragged,
    )


def apply_source_crop(image: Image.Image, bounds: PixelBounds) -> Image.Image:
    """Crop one RGBA frame using exact half-open oriented-source bounds."""
    _validate_rgba(image)
    _validate_contained_bounds(bounds, image.size)
    _validate_allocation_budget(image.size, 4, "source image")
    _validate_allocation_budget((bounds.width, bounds.height), 4, "crop output")
    return image.crop((bounds.left, bounds.top, bounds.right, bounds.bottom))


def alpha_bounds(
    image: Image.Image, threshold_percent: Decimal | float | int
) -> PixelBounds | None:
    """Return strict-threshold alpha bounds, or ``None`` for an empty mask."""
    _validate_rgba(image)
    _validate_allocation_budget(image.size, 4, "alpha source image")
    _validate_allocation_budget(image.size, 1, "alpha channel/mask")
    threshold = _percentage_fraction(threshold_percent)
    mask = image.getchannel("A").point(_alpha_threshold_table(threshold))
    bounds = mask.getbbox()
    if bounds is None:
        return None
    left, top, right, bottom = bounds
    return PixelBounds(left, top, right, bottom)


def union_alpha_bounds(
    frames: Iterable[Image.Image], threshold_percent: Decimal | float | int
) -> PixelBounds:
    """Incrementally return one union bound for an equal-canvas frame range."""
    try:
        iterator = iter(frames)
    except TypeError:
        raise ValidationError(
            ErrorCode.INVALID_FRAMING,
            "framing",
            "framing requires an iterable of RGBA frames",
        ) from None
    threshold = _percentage_fraction(threshold_percent)
    table = _alpha_threshold_table(threshold)
    union: PixelBounds | None = None
    expected_size: tuple[int, int] | None = None
    for image in iterator:
        _validate_rgba(image)
        _validate_allocation_budget(image.size, 4, "alpha-union source image")
        _validate_allocation_budget(image.size, 1, "alpha-union channel/mask")
        if expected_size is None:
            expected_size = image.size
        elif image.size != expected_size:
            raise ValidationError(
                ErrorCode.INVALID_FRAMING,
                "framing",
                "all frames must have identical source canvas dimensions",
            )
        raw_bounds = image.getchannel("A").point(table).getbbox()
        bounds = PixelBounds(*raw_bounds) if raw_bounds is not None else None
        if bounds is None:
            continue
        if union is None:
            union = bounds
        else:
            union = PixelBounds(
                min(union.left, bounds.left),
                min(union.top, bounds.top),
                max(union.right, bounds.right),
                max(union.bottom, bounds.bottom),
            )
    if union is None:
        raise ValidationError(
            ErrorCode.INVALID_FRAMING,
            "framing",
            "range-wide alpha union contains no visible pixels at this threshold",
        )
    return union


def apply_framing(image: Image.Image, plan: FramingPlan) -> Image.Image:
    """Apply one isolated, premultiplied crop/padding/stretch plan."""
    _validate_rgba(image)
    if not isinstance(plan, FramingPlan):
        raise ValidationError(
            ErrorCode.INVALID_FRAMING,
            "framing",
            "framing requires an immutable FramingPlan",
        )
    if image.size != plan.source_size:
        raise ValidationError(
            ErrorCode.INVALID_FRAMING,
            "framing",
            "frame does not match the plan source canvas",
        )
    bounds = plan.content_bounds
    cropped = image.crop((bounds.left, bounds.top, bounds.right, bounds.bottom))

    if plan.output_size == plan.padded_size:
        if not plan.padding:
            return cropped
        padded_rgba = Image.new("RGBA", plan.padded_size, (0, 0, 0, 0))
        padded_rgba.paste(cropped, (plan.padding, plan.padding))
        return padded_rgba

    working = cropped.convert("RGBa")
    del cropped

    if plan.padding:
        padded = Image.new("RGBa", plan.padded_size, (0, 0, 0, 0))
        padded.paste(working, (plan.padding, plan.padding))
        del working
        working = padded

    # Pillow resize uses center-aligned sampling:
    # input_x = (output_x + 0.5) * padded_width / output_width - 0.5.
    resized = working.resize(plan.output_size, Image.Resampling.BICUBIC)
    del working
    result = resized.convert("RGBA")
    return result


def solve_proportional_scale(
    source_width: int,
    source_height: int,
    *,
    current_scale: Decimal,
    target_bytes: int,
    current_bytes: int,
    min_dimension: int = 128,
) -> Decimal:
    """Return the next cumulative auto-fit scale with exact Decimal clamping."""
    dimensions = (source_width, source_height, min_dimension)
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in dimensions
    ):
        raise ValidationError(
            ErrorCode.IMPOSSIBLE_SIZE,
            "auto-fit",
            "source and minimum dimensions must be positive integers",
        )
    if source_width < min_dimension or source_height < min_dimension:
        raise ValidationError(
            ErrorCode.IMPOSSIBLE_SIZE,
            "auto-fit",
            "source dimensions are already below the minimum dimension",
        )
    if (
        not isinstance(current_scale, Decimal)
        or not current_scale.is_finite()
        or current_scale <= 0
    ):
        raise ValidationError(
            ErrorCode.IMPOSSIBLE_SIZE,
            "auto-fit",
            "current scale must be a positive finite Decimal",
        )
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in (target_bytes, current_bytes)
    ):
        raise ValidationError(
            ErrorCode.IMPOSSIBLE_SIZE,
            "auto-fit",
            "target and current byte counts must be positive integers",
        )
    current_fraction = Fraction(current_scale)
    ratio = Fraction(target_bytes * 94, current_bytes * 100)
    minimum_fraction = max(
        Fraction(min_dimension, source_width),
        Fraction(min_dimension, source_height),
    )
    with localcontext(Context(prec=80)):
        step = (
            Decimal(ratio.numerator) / Decimal(ratio.denominator)
        ).sqrt()
        step = min(Decimal("0.97"), step)
        current = Decimal(current_fraction.numerator) / Decimal(
            current_fraction.denominator
        )
        minimum_scale = Decimal(minimum_fraction.numerator) / Decimal(
            minimum_fraction.denominator
        )
        return max(minimum_scale, current * step)


def _map_rect(rect: RectF, converter: Callable[[PointF], PointF]) -> RectF:
    points = (
        converter(PointF(rect.left, rect.top)),
        converter(PointF(rect.right, rect.top)),
        converter(PointF(rect.right, rect.bottom)),
        converter(PointF(rect.left, rect.bottom)),
    )
    xs = [point.x for point in points]
    ys = [point.y for point in points]
    return RectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def _centered_rect(center: PointF, width: float, height: float) -> RectF:
    return RectF(center.x - width / 2, center.y - height / 2, width, height)


def _effective_target(rect: RectF, viewport: SizeF, minimum: float) -> RectF:
    width = min(viewport.width, max(rect.width, minimum))
    height = min(viewport.height, max(rect.height, minimum))
    center = rect.center()
    x = min(max(center.x - width / 2, 0), viewport.width - width)
    y = min(max(center.y - height / 2, 0), viewport.height - height)
    return RectF(x, y, width, height)


def _unique_names(values: Sequence[str | None]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value is not None))


def _validate_rgba(image: Image.Image) -> None:
    if not isinstance(image, Image.Image) or image.mode != "RGBA":
        raise ValidationError(
            ErrorCode.INVALID_FRAMING,
            "framing",
            "frames must be real Pillow RGBA images",
        )


def _validate_contained_bounds(
    bounds: PixelBounds, image_size: tuple[int, int]
) -> None:
    if not isinstance(bounds, PixelBounds):
        raise ValidationError(
            ErrorCode.INVALID_CROP,
            "crop",
            "crop must use exact half-open PixelBounds",
        )
    if bounds.right > image_size[0] or bounds.bottom > image_size[1]:
        raise ValidationError(
            ErrorCode.INVALID_CROP,
            "crop",
            "pixel bounds must be contained by the image canvas",
        )


def _validate_allocation_budget(
    image_size: tuple[int, int], bytes_per_pixel: int, detail: str
) -> None:
    allocation_bytes = image_size[0] * image_size[1] * bytes_per_pixel
    if allocation_bytes > _MAX_IMAGE_ALLOCATION_BYTES:
        raise ValidationError(
            ErrorCode.INVALID_FINAL_DIMENSIONS,
            "framing",
            f"{detail} exceeds the 1073741824-byte allocation byte budget",
        )


def _number_fraction(value: Fraction | Decimal | float | int, name: str) -> Fraction:
    if isinstance(value, bool):
        raise ValidationError(
            ErrorCode.INVALID_FRAMING, "framing", f"{name} must be finite"
        )
    try:
        if isinstance(value, Fraction):
            return value
        converted = value if isinstance(value, Decimal) else Decimal(str(value))
        if not converted.is_finite():
            raise ValueError
        return Fraction(converted)
    except (ValueError, TypeError, ArithmeticError):
        raise ValidationError(
            ErrorCode.INVALID_FRAMING, "framing", f"{name} must be finite"
        ) from None


def _positive_fraction(
    value: Fraction | Decimal | float | int, name: str
) -> Fraction:
    converted = _number_fraction(value, name)
    if converted <= 0:
        raise ValidationError(
            ErrorCode.INVALID_FRAMING, "framing", f"{name} must be positive"
        )
    return converted


def _percentage_fraction(value: Decimal | float | int) -> Fraction:
    converted = _number_fraction(value, "alpha threshold")
    if not Fraction(0) <= converted <= Fraction(100):
        raise ValidationError(
            ErrorCode.INVALID_FRAMING,
            "framing",
            "alpha threshold must be between 0 and 100 percent",
        )
    return converted


def _alpha_threshold_table(threshold: Fraction) -> list[int]:
    return [
        255
        if alpha * 100 * threshold.denominator > 255 * threshold.numerator
        else 0
        for alpha in range(256)
    ]


def _integer_size(value: object, name: str) -> tuple[int, int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValidationError(
            ErrorCode.INVALID_FRAMING, "framing", f"{name} must contain two integers"
        )
    copied = tuple(value)
    if (
        len(copied) != 2
        or any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0
            for item in copied
        )
    ):
        raise ValidationError(
            ErrorCode.INVALID_FRAMING, "framing", f"{name} must contain two integers"
        )
    return copied[0], copied[1]


def _round_positive_fraction(value: Fraction) -> int:
    rounded = (2 * value.numerator + value.denominator) // (2 * value.denominator)
    return rounded


def _rational_time(value: Fraction | Decimal) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, Decimal) and value.is_finite():
        return Fraction(value)
    raise ValueError("timeline values must be finite Fraction or Decimal values")
