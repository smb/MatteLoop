"""Validated immutable render specifications with no GUI or process dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from pathlib import Path

from rembggui.core.errors import ErrorCode, ValidationError

MIN_FINAL_DIMENSION = 128
MAX_FINAL_DIMENSION = 16_383
MIN_FPS = 1
MAX_FPS = 240
_MIB_BYTES = Decimal(1024 * 1024)
_SUPPORTED_SOURCE_SUFFIXES = frozenset({".mp4", ".mov", ".webm", ".mkv"})


class EdgeMode(StrEnum):
    STANDARD = "standard"
    DECONTAMINATE_COLORS = "decontaminate_colors"
    ALPHA_MATTING = "alpha_matting"


class CollisionPolicy(StrEnum):
    REPLACE = "replace"
    CHOOSE_ANOTHER_NAME = "choose_another_name"
    CANCEL = "cancel"


@dataclass(frozen=True)
class SamplingSpec:
    """Timestamp-based output sampling over a half-open [start, end) interval."""

    end: Fraction = field(default_factory=lambda: Fraction(1))
    start: Fraction = field(default_factory=lambda: Fraction(0))
    fps: int = 15

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.start, Fraction) or not isinstance(self.end, Fraction):
            raise ValidationError(
                ErrorCode.INVALID_SAMPLING,
                "sampling",
                "start and end must be Fraction values",
            )
        if self.start < 0 or self.end <= self.start:
            raise ValidationError(
                ErrorCode.INVALID_SAMPLING,
                "sampling",
                "sampling interval must satisfy 0 <= start < end",
            )
        if (
            not isinstance(self.fps, int)
            or isinstance(self.fps, bool)
            or not MIN_FPS <= self.fps <= MAX_FPS
        ):
            raise ValidationError(
                ErrorCode.INVALID_SAMPLING,
                "sampling",
                "fps must be an integer between 1 and 240",
            )

    def contains(self, timestamp: Fraction) -> bool:
        """Whether *timestamp* belongs to this Start-inclusive, End-exclusive range."""
        return self.start <= timestamp < self.end


@dataclass(frozen=True)
class CropSpec:
    """A crop rectangle expressed in already oriented source pixels."""

    x: int
    y: int
    width: int
    height: int

    def validate(self) -> None:
        fields = (self.x, self.y, self.width, self.height)
        if any(
            not isinstance(value, int) or isinstance(value, bool) for value in fields
        ):
            raise ValidationError(
                ErrorCode.INVALID_CROP,
                "crop",
                "crop coordinates and dimensions must be integers",
            )
        if self.x < 0 or self.y < 0 or self.width < 1 or self.height < 1:
            raise ValidationError(
                ErrorCode.INVALID_CROP,
                "crop",
                "crop origin must be non-negative and dimensions must be positive",
            )

    def validate_for(self, source_width: int, source_height: int) -> None:
        """Validate this crop against the dimensions of the oriented source frame."""
        self.validate()
        if (
            not isinstance(source_width, int)
            or not isinstance(source_height, int)
            or isinstance(source_width, bool)
            or isinstance(source_height, bool)
            or source_width < 1
            or source_height < 1
            or self.x + self.width > source_width
            or self.y + self.height > source_height
        ):
            raise ValidationError(
                ErrorCode.INVALID_CROP,
                "crop",
                "crop must be fully contained by the oriented source dimensions",
            )


@dataclass(frozen=True)
class SegmentationSpec:
    """Model and edge-treatment selections for a segmentation worker."""

    model_id: str = "birefnet-portrait"
    edge_mode: EdgeMode = EdgeMode.STANDARD

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValidationError(
                ErrorCode.INVALID_SEGMENTATION,
                "segmentation",
                "model_id must be a non-empty string",
            )
        if not isinstance(self.edge_mode, EdgeMode):
            raise ValidationError(
                ErrorCode.INVALID_SEGMENTATION,
                "segmentation",
                "edge_mode must be an EdgeMode",
            )


@dataclass(frozen=True)
class FramingSpec:
    """Post-segmentation trim, padding, and horizontal stretch settings."""

    trim: bool = False
    alpha_threshold: Decimal = Decimal("2.0")
    padding: int = 0
    stretch_x: Decimal = Decimal("1.0")

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.trim, bool):
            raise ValidationError(
                ErrorCode.INVALID_FRAMING, "framing", "trim must be a bool"
            )
        if (
            not isinstance(self.alpha_threshold, Decimal)
            or not Decimal(0) <= self.alpha_threshold <= Decimal(100)
        ):
            raise ValidationError(
                ErrorCode.INVALID_FRAMING,
                "framing",
                "alpha threshold must be a Decimal percentage from 0 to 100",
            )
        if (
            not isinstance(self.padding, int)
            or isinstance(self.padding, bool)
            or self.padding < 0
        ):
            raise ValidationError(
                ErrorCode.INVALID_FRAMING,
                "framing",
                "padding must be an integer greater than or equal to zero",
            )
        if not isinstance(self.stretch_x, Decimal) or self.stretch_x <= 0:
            raise ValidationError(
                ErrorCode.INVALID_FRAMING,
                "framing",
                "horizontal stretch must be a positive Decimal",
            )

    def validate_final_dimensions(self, width: int, height: int) -> tuple[int, int]:
        """Validate fixed WebP dimensions before allocation or output replacement."""
        if (
            not isinstance(width, int)
            or not isinstance(height, int)
            or isinstance(width, bool)
            or isinstance(height, bool)
            or not MIN_FINAL_DIMENSION <= width <= MAX_FINAL_DIMENSION
            or not MIN_FINAL_DIMENSION <= height <= MAX_FINAL_DIMENSION
        ):
            raise ValidationError(
                ErrorCode.INVALID_FINAL_DIMENSIONS,
                "framing",
                "final dimensions must each be between 128 and 16383 pixels",
            )
        return width, height

    def dimensions_after_padding_and_stretch(
        self, width: int, height: int
    ) -> tuple[int, int]:
        """Return no-trim worst-case dimensions using script-compatible rounding."""
        if width < 1 or height < 1:
            raise ValidationError(
                ErrorCode.INVALID_FINAL_DIMENSIONS,
                "framing",
                "source dimensions must be positive",
            )
        padded_width = width + 2 * self.padding
        padded_height = height + 2 * self.padding
        stretched_width = int(Decimal(padded_width) * self.stretch_x + Decimal("0.5"))
        return stretched_width, padded_height


@dataclass(frozen=True)
class OutputSpec:
    """Output naming, collision choice, and optional final-size limit."""

    directory: Path
    filename: str
    max_bytes: int | None = None
    collision_policy: CollisionPolicy = CollisionPolicy.CANCEL

    def __post_init__(self) -> None:
        self.validate()

    @property
    def path(self) -> Path:
        return self.directory / self.filename

    @classmethod
    def from_mib(
        cls,
        directory: Path,
        filename: str,
        max_mib: Decimal,
        collision_policy: CollisionPolicy = CollisionPolicy.CANCEL,
    ) -> OutputSpec:
        """Create output settings from a GUI MiB value without binary float drift."""
        if not isinstance(max_mib, Decimal) or max_mib < 0:
            raise ValidationError(
                ErrorCode.INVALID_OUTPUT,
                "output",
                "maximum size must be a Decimal value greater than or equal to zero",
            )
        max_bytes = None if max_mib == 0 else int(max_mib * _MIB_BYTES)
        return cls(directory, filename, max_bytes, collision_policy)

    def validate(self) -> None:
        if not isinstance(self.directory, Path) or self.directory == Path(""):
            raise ValidationError(
                ErrorCode.INVALID_OUTPUT,
                "output",
                "output directory must be a Path",
            )
        if (
            not isinstance(self.filename, str)
            or not self.filename
            or self.filename in {".", "..", ".webp"}
            or "/" in self.filename
            or "\\" in self.filename
            or Path(self.filename).suffix.lower() != ".webp"
        ):
            raise ValidationError(
                ErrorCode.INVALID_OUTPUT,
                "output",
                "filename must be a single non-empty .webp filename",
            )
        if (
            self.max_bytes is not None
            and (
                not isinstance(self.max_bytes, int)
                or isinstance(self.max_bytes, bool)
                or self.max_bytes < 0
            )
        ):
            raise ValidationError(
                ErrorCode.INVALID_OUTPUT,
                "output",
                "max_bytes must be an integer greater than or equal to zero",
            )
        if not isinstance(self.collision_policy, CollisionPolicy):
            raise ValidationError(
                ErrorCode.INVALID_OUTPUT,
                "output",
                "collision_policy must be a CollisionPolicy",
            )


@dataclass(frozen=True)
class RenderRequest:
    """Complete immutable input to a preview, render, rebuild, or regeneration job."""

    source: Path
    sampling: SamplingSpec
    crop: CropSpec
    segmentation: SegmentationSpec
    framing: FramingSpec
    output: OutputSpec
    rebuild: bool = False
    regenerate: bool = False

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if (
            not isinstance(self.source, Path)
            or self.source == Path("")
            or self.source.suffix.lower() not in _SUPPORTED_SOURCE_SUFFIXES
        ):
            raise ValidationError(
                ErrorCode.INVALID_RENDER_REQUEST,
                "request",
                "source must be a local MP4, MOV, WebM, or MKV Path",
            )
        self.sampling.validate()
        self.crop.validate()
        self.segmentation.validate()
        self.framing.validate()
        self.output.validate()
        if not isinstance(self.rebuild, bool) or not isinstance(self.regenerate, bool):
            raise ValidationError(
                ErrorCode.INVALID_RENDER_REQUEST,
                "request",
                "rebuild and regenerate must be bool values",
            )
        if self.rebuild and self.regenerate:
            raise ValidationError(
                ErrorCode.INVALID_RENDER_REQUEST,
                "request",
                "rebuild and regenerate are mutually exclusive",
            )

    def validate_for_source(self, width: int, height: int) -> tuple[int, int]:
        """Validate source-specific crop and conservative post-process dimensions."""
        self.crop.validate_for(width, height)
        final_dimensions = self.framing.dimensions_after_padding_and_stretch(
            self.crop.width, self.crop.height
        )
        return self.framing.validate_final_dimensions(*final_dimensions)
