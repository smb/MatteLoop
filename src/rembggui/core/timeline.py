"""Pure timeline state and editor events."""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from math import ceil


@dataclass(frozen=True, slots=True)
class TimelineState:
    """The rational source timeline and its half-open export interval."""

    duration: Fraction
    start: Fraction
    end: Fraction
    playhead: Fraction
    fps: int = 15
    source_fps: Fraction = Fraction(15)
    generation: int = 0

    def __post_init__(self) -> None:
        values = (self.duration, self.start, self.end, self.playhead, self.source_fps)
        if any(not isinstance(value, Fraction) for value in values):
            raise ValueError("timeline values must be Fraction values")
        if self.duration <= 0 or not 0 <= self.start < self.end <= self.duration:
            raise ValueError("timeline range must satisfy 0 <= start < end <= duration")
        if not 0 <= self.playhead <= self.duration:
            raise ValueError("timeline playhead must be within the source duration")
        if (
            not isinstance(self.fps, int)
            or isinstance(self.fps, bool)
            or not 1 <= self.fps <= 240
        ):
            raise ValueError("timeline fps must be between 1 and 240")
        if self.source_fps <= 0:
            raise ValueError("source fps must be positive")
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 0
        ):
            raise ValueError("timeline generation must be non-negative")
        if self.end - self.start < self.output_frame_interval:
            raise ValueError("timeline range must retain at least one output frame")

    @property
    def output_frame_interval(self) -> Fraction:
        return Fraction(1, self.fps)

    @property
    def source_frame_interval(self) -> Fraction:
        return Fraction(1, 1) / self.source_fps

    def move_playhead(self, timestamp: Fraction) -> TimelineState:
        latest_frame = max(Fraction(0), self.duration - self.source_frame_interval)
        timestamp = _clamp(timestamp, Fraction(0), latest_frame)
        return replace(self, playhead=timestamp, generation=self.generation + 1)

    def step(self, delta: int) -> TimelineState:
        if not isinstance(delta, int) or isinstance(delta, bool):
            raise ValueError("frame step must be an integer")
        return self.move_playhead(self.playhead + delta * self.source_frame_interval)

    def set_start(self, timestamp: Fraction) -> TimelineState:
        timestamp = _clamp(
            timestamp, Fraction(0), self.end - self.output_frame_interval
        )
        return replace(self, start=timestamp)

    def set_end(self, timestamp: Fraction) -> TimelineState:
        timestamp = _clamp(
            timestamp,
            self.start + self.output_frame_interval,
            self.duration,
        )
        return replace(self, end=timestamp)


@dataclass(frozen=True, slots=True)
class PlayheadChanged:
    timestamp: Fraction


@dataclass(frozen=True, slots=True)
class StepFrame:
    delta: int


@dataclass(frozen=True, slots=True)
class StartChanged:
    timestamp: Fraction


@dataclass(frozen=True, slots=True)
class EndChanged:
    timestamp: Fraction


@dataclass(frozen=True, slots=True)
class SetStartToPlayhead:
    pass


@dataclass(frozen=True, slots=True)
class SetEndToPlayhead:
    pass


@dataclass(frozen=True, slots=True)
class SourceFrameDecoded:
    source_id: str
    generation: int
    frame: object


SetStart = StartChanged
SetEnd = EndChanged

TimelineEvent = (
    PlayheadChanged
    | StepFrame
    | StartChanged
    | EndChanged
    | SetStartToPlayhead
    | SetEndToPlayhead
)


def update_timeline(
    timeline: TimelineState, event: TimelineEvent
) -> tuple[TimelineState, str] | None:
    """Apply one editor event, returning its stale-preview category."""
    try:
        if isinstance(event, PlayheadChanged):
            updated, category = timeline.move_playhead(event.timestamp), "Playhead"
        elif isinstance(event, StepFrame):
            updated, category = timeline.step(event.delta), "Playhead"
        elif isinstance(event, StartChanged):
            updated, category = timeline.set_start(event.timestamp), "Export range"
        elif isinstance(event, EndChanged):
            updated, category = timeline.set_end(event.timestamp), "Export range"
        elif isinstance(event, SetStartToPlayhead):
            updated, category = timeline.set_start(timeline.playhead), "Export range"
        elif isinstance(event, SetEndToPlayhead):
            updated, category = timeline.set_end(timeline.playhead), "Export range"
        else:
            return None
    except ValueError:
        return None
    if (
        updated.playhead == timeline.playhead
        and updated.start == timeline.start
        and updated.end == timeline.end
    ):
        return None
    return updated, category


def timeline_from_metadata(metadata: object) -> TimelineState | None:
    """Build the default editor range from source metadata when available."""
    duration = getattr(metadata, "duration", None)
    if not isinstance(duration, Fraction) or duration <= 0:
        return None
    source_fps = _metadata_rate(metadata)
    fps = 15
    if duration < Fraction(1, fps):
        fps = min(240, max(1, ceil(1 / float(duration))))
    try:
        return TimelineState(
            duration, Fraction(0), duration, Fraction(0), fps, source_fps
        )
    except ValueError:
        return None


def _metadata_rate(metadata: object) -> Fraction:
    for name in ("average_rate", "base_rate", "guessed_rate", "peak_rate"):
        value = getattr(metadata, name, None)
        if isinstance(value, Fraction) and value > 0:
            return value
    return Fraction(15)


def _clamp(value: Fraction, lower: Fraction, upper: Fraction) -> Fraction:
    if not isinstance(value, Fraction):
        raise ValueError("timeline values must be Fraction values")
    return min(max(value, lower), upper)


def format_timecode(timestamp: Fraction) -> str:
    """Format a timestamp at millisecond precision without float maths."""
    if not isinstance(timestamp, Fraction) or timestamp < 0:
        return ""
    milliseconds = (timestamp * 1000 + Fraction(1, 2)).numerator // (
        timestamp * 1000 + Fraction(1, 2)
    ).denominator
    total_seconds, milliseconds = divmod(milliseconds, 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def absolute_frame_number(timestamp: Fraction, source_fps: Fraction) -> int:
    """Return the informational one-based frame number at a timestamp."""
    if (
        not isinstance(timestamp, Fraction)
        or not isinstance(source_fps, Fraction)
        or timestamp < 0
        or source_fps <= 0
    ):
        return 0
    return int(timestamp * source_fps) + 1
