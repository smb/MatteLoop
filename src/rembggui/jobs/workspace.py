"""Durable editable cut sets and private rebuild snapshots.

The durable namespace is intentionally separate from disposable job scratch::

    <output>/.rembggui-work/
        cuts/<authoritative-cache-key>/   # explicit deletion only
        scratch/<job-id>/                 # bounded, explicit cleanup

Every public operation revalidates the canonical local root and opens files
without following links.  POSIX file access is relative to bound directory
descriptors.  Directory replacement uses a native atomic exchange where the
platform provides one; the portable fallback is journaled so an interrupted
two-rename replacement is recovered before the cache is observed again.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path, PurePath, PureWindowsPath
from threading import Lock
from types import TracebackType
from typing import BinaryIO, Final, Self, cast, overload

from PIL import Image, UnidentifiedImageError

from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.fingerprints import (
    FINGERPRINT_SCHEMA,
    FINGERPRINT_SCHEMA_VERSION,
)
from rembggui.core.rgba import RgbaOwnershipTracker

MANIFEST_FILENAME: Final = "manifest.json"
MANIFEST_SCHEMA: Final = "rembggui-cut-manifest"
MANIFEST_SCHEMA_VERSION: Final = 1
MAX_MANIFEST_BYTES: Final = 64 * 1024 * 1024
MAX_FRAME_COUNT: Final = 100_000
MAX_FRAME_FILE_BYTES: Final = 1024 * 1024 * 1024
MAX_CUT_DIMENSION: Final = 16_383
MAX_CUT_PIXELS: Final = 3840 * 2160
MAX_PATH_CHARS: Final = 4096
MAX_SOURCE_PATH_CHARS: Final = 4096
MAX_TEXT_CHARS: Final = 256
MAX_JOB_ID_CHARS: Final = 128
MAX_WORKSPACE_ENTRIES: Final = 10_000
MAX_SCRATCH_ENTRIES: Final = 10_000
WORKSPACE_WARNING_BYTES: Final = 20 * 1024**3
ABANDONED_SCRATCH_AGE_NS: Final = 24 * 60 * 60 * 1_000_000_000
COPY_CHUNK_BYTES: Final = 1024 * 1024

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CACHE_KEY_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_JOB_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_FRAME_RE = re.compile(r"frame-([0-9]{6})\.png\Z")
_STAGE_RE = re.compile(r"\.stage-([0-9a-f]{64})-([A-Za-z0-9._-]+)\Z")
_MARKER_RE = re.compile(r"\.replace-([0-9a-f]{64})\.json\Z")
_BACKUP_RE = re.compile(r"\.backup-([0-9a-f]{64})-([0-9a-f]{32})\Z")
_MAX_INT64 = 2**63 - 1

type JsonScalar = str | int | bool | None
type FrozenJsonValue = JsonScalar | tuple["FrozenJsonValue", ...] | "FrozenJsonMap"
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
type CancellationCheck = Callable[[], bool]


class FrozenJsonMap(Mapping[str, FrozenJsonValue]):
    """Small recursively immutable mapping used by frozen manifests."""

    __slots__ = ("_items", "_lookup")

    def __init__(self, items: Sequence[tuple[str, FrozenJsonValue]]) -> None:
        ordered = tuple(sorted(items, key=lambda item: item[0]))
        self._items = ordered
        self._lookup = {key: value for key, value in ordered}

    def __getitem__(self, key: str) -> FrozenJsonValue:
        return self._lookup[key]

    def __iter__(self) -> Iterator[str]:
        return (key for key, _value in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"FrozenJsonMap({self._items!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self.items()) == dict(other.items())
        return NotImplemented


@dataclass(frozen=True, slots=True)
class CutFrame:
    """Stable record for one independently readable RGBA PNG."""

    index: int
    filename: str
    width: int
    height: int
    size_bytes: int
    mtime_ns: int
    sha256: str

    def __post_init__(self) -> None:
        _validate_frame_index(self.index)
        if self.filename != _frame_filename(self.index):
            canonical_name = _frame_filename(self.index)
            raise _manifest_error(
                f"frame {self.index} must use canonical name {canonical_name!r}"
            )
        _validate_dimensions(self.width, self.height)
        _bounded_int(
            self.size_bytes, "frame size", minimum=1, maximum=MAX_FRAME_FILE_BYTES
        )
        _bounded_int(self.mtime_ns, "frame mtime", minimum=0, maximum=_MAX_INT64)
        _validate_sha256(self.sha256, "frame sha256")


@dataclass(frozen=True, slots=True)
class CutUnionMetadata:
    """Derived range-wide alpha union, invalidated by every external edit."""

    bounds: tuple[int, int, int, int]
    alpha_threshold: str
    fingerprint: str

    def __post_init__(self) -> None:
        if (
            type(self.bounds) is not tuple
            or len(self.bounds) != 4
            or any(type(value) is not int for value in self.bounds)
        ):
            raise _manifest_error("union bounds must contain four integers")
        left, top, right, bottom = self.bounds
        if left < 0 or top < 0 or right <= left or bottom <= top:
            raise _manifest_error("union bounds must be a positive half-open rectangle")
        if not isinstance(self.alpha_threshold, str) or not self.alpha_threshold:
            raise _manifest_error("union alpha threshold must be a decimal string")
        try:
            threshold = Decimal(self.alpha_threshold)
        except InvalidOperation as error:
            raise _manifest_error("union alpha threshold must be decimal") from error
        if not threshold.is_finite() or not Decimal(0) <= threshold <= Decimal(100):
            raise _manifest_error("union alpha threshold must be between 0 and 100")
        _validate_sha256(self.fingerprint, "union fingerprint")


@dataclass(frozen=True, slots=True)
class CutManifest:
    """Strict, versioned, deeply immutable description of a complete cut set."""

    cache_key: str
    cache_key_inputs: FrozenJsonMap
    source_path: str
    source_size_bytes: int
    source_mtime_ns: int
    width: int
    height: int
    frames: tuple[CutFrame, ...]
    union_metadata: CutUnionMetadata | None
    edited: bool
    pinned: bool
    created_at_ns: int
    last_used_at_ns: int
    schema: str = MANIFEST_SCHEMA
    schema_version: int = MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            self.schema != MANIFEST_SCHEMA
            or self.schema_version != MANIFEST_SCHEMA_VERSION
        ):
            raise _manifest_error("unsupported cut manifest schema")
        if not isinstance(self.cache_key_inputs, FrozenJsonMap):
            frozen = _freeze_json(self.cache_key_inputs, field="cache_key_inputs")
            if not isinstance(frozen, FrozenJsonMap):
                raise _manifest_error("cache_key_inputs must be an object")
            object.__setattr__(self, "cache_key_inputs", frozen)
        _validate_cache_inputs(self.cache_key_inputs)
        expected_key = self.cache_key_for(self.cache_key_inputs)
        _validate_cache_key(self.cache_key)
        if self.cache_key != expected_key:
            raise _manifest_error("cache key does not match its authoritative inputs")
        if (
            not isinstance(self.source_path, str)
            or not self.source_path
            or "\x00" in self.source_path
            or len(self.source_path) > MAX_SOURCE_PATH_CHARS
        ):
            raise _manifest_error("source path metadata is invalid")
        _bounded_int(
            self.source_size_bytes,
            "source size",
            minimum=0,
            maximum=_MAX_INT64,
        )
        _bounded_int(
            self.source_mtime_ns,
            "source mtime",
            minimum=0,
            maximum=_MAX_INT64,
        )
        _validate_dimensions(self.width, self.height)
        if type(self.frames) is not tuple:
            object.__setattr__(self, "frames", tuple(self.frames))
        if not 1 <= len(self.frames) <= MAX_FRAME_COUNT:
            raise _manifest_error(
                f"frame count must be between 1 and {MAX_FRAME_COUNT}"
            )
        for index, frame in enumerate(self.frames):
            if type(frame) is not CutFrame or frame.index != index:
                raise _manifest_error("manifest frame entries must be sequential")
            if (frame.width, frame.height) != (self.width, self.height):
                raise _manifest_error("manifest frame dimensions must be identical")
        if self.union_metadata is not None:
            if type(self.union_metadata) is not CutUnionMetadata:
                raise _manifest_error("union metadata has an invalid type")
            left, top, right, bottom = self.union_metadata.bounds
            if right > self.width or bottom > self.height:
                raise _manifest_error("union bounds exceed cut dimensions")
        if type(self.edited) is not bool or type(self.pinned) is not bool:
            raise _manifest_error("edited and pinned must be booleans")
        _bounded_int(
            self.created_at_ns,
            "created timestamp",
            minimum=0,
            maximum=_MAX_INT64,
        )
        _bounded_int(
            self.last_used_at_ns,
            "last-use timestamp",
            minimum=self.created_at_ns,
            maximum=_MAX_INT64,
        )

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def source_sha256(self) -> str:
        return cast(str, self.cache_key_inputs["source_sha256"])

    @property
    def model_id(self) -> str:
        model = cast(FrozenJsonMap, self.cache_key_inputs["model"])
        return cast(str, model["id"])

    @property
    def model_weight_sha256(self) -> str:
        model = cast(FrozenJsonMap, self.cache_key_inputs["model"])
        return cast(str, model["weight_sha256"])

    @property
    def rembg_version(self) -> str:
        return cast(str, self.cache_key_inputs["rembg_version"])

    @property
    def pipeline_schema_version(self) -> str:
        return cast(str, self.cache_key_inputs["pipeline_schema_version"])

    @property
    def orientation_color_version(self) -> str:
        return cast(str, self.cache_key_inputs["orientation_color_version"])

    @classmethod
    def create(
        cls,
        *,
        cache_key_inputs: Mapping[str, object],
        source_path: str,
        source_size_bytes: int,
        source_mtime_ns: int,
        frames: Sequence[CutFrame],
        union_metadata: CutUnionMetadata | None = None,
        edited: bool = False,
        pinned: bool = False,
        now_ns: int | None = None,
    ) -> Self:
        frozen = _freeze_json(cache_key_inputs, field="cache_key_inputs")
        if not isinstance(frozen, FrozenJsonMap):
            raise _manifest_error("cache_key_inputs must be an object")
        frame_tuple = tuple(frames)
        if not frame_tuple:
            raise _manifest_error("a cut manifest needs at least one frame")
        timestamp = time.time_ns() if now_ns is None else now_ns
        return cls(
            cache_key=cls.cache_key_for(frozen),
            cache_key_inputs=frozen,
            source_path=source_path,
            source_size_bytes=source_size_bytes,
            source_mtime_ns=source_mtime_ns,
            width=frame_tuple[0].width,
            height=frame_tuple[0].height,
            frames=frame_tuple,
            union_metadata=union_metadata,
            edited=edited,
            pinned=pinned,
            created_at_ns=timestamp,
            last_used_at_ns=timestamp,
        )

    @staticmethod
    def cache_key_for(cache_key_inputs: Mapping[str, object]) -> str:
        frozen = _freeze_json(cache_key_inputs, field="cache_key_inputs")
        if not isinstance(frozen, FrozenJsonMap):
            raise _manifest_error("cache_key_inputs must be an object")
        _validate_cache_inputs(frozen)
        payload: dict[str, object] = {
            "fingerprint_schema": FINGERPRINT_SCHEMA,
            "fingerprint_schema_version": FINGERPRINT_SCHEMA_VERSION,
            "kind": "cut-cache-key",
        }
        payload.update(cast(dict[str, object], _thaw_json(frozen)))
        return hashlib.sha256(_canonical_json(payload)).hexdigest()

    def to_json_bytes(self) -> bytes:
        return _canonical_json(self._to_payload()) + b"\n"

    @classmethod
    def from_json_bytes(cls, encoded: bytes) -> Self:
        if not isinstance(encoded, bytes) or len(encoded) > MAX_MANIFEST_BYTES:
            raise _manifest_error("manifest exceeds the bounded byte limit")
        try:
            payload = json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=_strict_object,
                parse_constant=_reject_json_constant,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            RecursionError,
        ) as error:
            raise _manifest_error("manifest is not strict UTF-8 JSON") from error
        if not isinstance(payload, dict):
            raise _manifest_error("manifest root must be an object")
        _exact_keys(
            payload,
            {
                "cache_key",
                "cache_key_inputs",
                "created_at_ns",
                "dimensions",
                "edited",
                "frame_count",
                "frames",
                "last_used_at_ns",
                "pinned",
                "schema",
                "schema_version",
                "source",
                "union_metadata",
            },
            "manifest",
        )
        dimensions = _object(payload["dimensions"], "dimensions")
        _exact_keys(dimensions, {"height", "width"}, "dimensions")
        source = _object(payload["source"], "source")
        _exact_keys(source, {"mtime_ns", "path", "size_bytes"}, "source")
        raw_frames = payload["frames"]
        if not isinstance(raw_frames, list) or len(raw_frames) > MAX_FRAME_COUNT:
            raise _manifest_error("frames must be a bounded array")
        declared_count = _int(payload["frame_count"], "frame_count")
        if declared_count != len(raw_frames):
            raise _manifest_error("declared frame count does not match frame entries")
        frames = tuple(_frame_from_payload(item) for item in raw_frames)
        raw_union = payload["union_metadata"]
        union: CutUnionMetadata | None
        if raw_union is None:
            union = None
        else:
            union_payload = _object(raw_union, "union_metadata")
            _exact_keys(
                union_payload,
                {"alpha_threshold", "bounds", "fingerprint"},
                "union_metadata",
            )
            raw_bounds = union_payload["bounds"]
            if not isinstance(raw_bounds, list) or len(raw_bounds) != 4:
                raise _manifest_error("union bounds must contain four integers")
            union = CutUnionMetadata(
                bounds=cast(tuple[int, int, int, int], tuple(raw_bounds)),
                alpha_threshold=_string(
                    union_payload["alpha_threshold"], "union alpha threshold"
                ),
                fingerprint=_string(union_payload["fingerprint"], "union fingerprint"),
            )
        raw_inputs = _object(payload["cache_key_inputs"], "cache_key_inputs")
        frozen_inputs = _freeze_json(raw_inputs, field="cache_key_inputs")
        assert isinstance(frozen_inputs, FrozenJsonMap)
        return cls(
            cache_key=_string(payload["cache_key"], "cache_key"),
            cache_key_inputs=frozen_inputs,
            source_path=_string(source["path"], "source path"),
            source_size_bytes=_int(source["size_bytes"], "source size"),
            source_mtime_ns=_int(source["mtime_ns"], "source mtime"),
            width=_int(dimensions["width"], "width"),
            height=_int(dimensions["height"], "height"),
            frames=frames,
            union_metadata=union,
            edited=_bool(payload["edited"], "edited"),
            pinned=_bool(payload["pinned"], "pinned"),
            created_at_ns=_int(payload["created_at_ns"], "created_at_ns"),
            last_used_at_ns=_int(payload["last_used_at_ns"], "last_used_at_ns"),
            schema=_string(payload["schema"], "schema"),
            schema_version=_int(payload["schema_version"], "schema_version"),
        )

    def _to_payload(self) -> dict[str, object]:
        union: dict[str, object] | None = None
        if self.union_metadata is not None:
            union = {
                "alpha_threshold": self.union_metadata.alpha_threshold,
                "bounds": list(self.union_metadata.bounds),
                "fingerprint": self.union_metadata.fingerprint,
            }
        return {
            "cache_key": self.cache_key,
            "cache_key_inputs": _thaw_json(self.cache_key_inputs),
            "created_at_ns": self.created_at_ns,
            "dimensions": {"height": self.height, "width": self.width},
            "edited": self.edited,
            "frame_count": self.frame_count,
            "frames": [
                {
                    "filename": frame.filename,
                    "height": frame.height,
                    "index": frame.index,
                    "mtime_ns": frame.mtime_ns,
                    "sha256": frame.sha256,
                    "size_bytes": frame.size_bytes,
                    "width": frame.width,
                }
                for frame in self.frames
            ],
            "last_used_at_ns": self.last_used_at_ns,
            "pinned": self.pinned,
            "schema": self.schema,
            "schema_version": self.schema_version,
            "source": {
                "mtime_ns": self.source_mtime_ns,
                "path": self.source_path,
                "size_bytes": self.source_size_bytes,
            },
            "union_metadata": union,
        }


class WorkspaceLifecycle(StrEnum):
    STAGING = "staging"
    PROMOTED = "promoted"
    SNAPSHOT = "snapshot"


@dataclass(frozen=True, slots=True)
class CutWorkspace:
    """One staged, durable, or private-snapshot cut directory."""

    output_directory: Path
    workspace_root: Path
    cuts_root: Path
    scratch_root: Path
    cache_key: str
    path: Path
    lifecycle: WorkspaceLifecycle

    def __post_init__(self) -> None:
        _validate_cache_key(self.cache_key)
        for value in (
            self.output_directory,
            self.workspace_root,
            self.cuts_root,
            self.scratch_root,
            self.path,
        ):
            _validate_path_value(value)
        expected_root = self.output_directory / ".rembggui-work"
        if not _same_lexical_path(self.workspace_root, expected_root):
            raise _unsafe_error("workspace root is not bound to the output directory")
        if not _same_lexical_path(self.cuts_root, self.workspace_root / "cuts"):
            raise _unsafe_error("cuts root is not canonical")
        if not _same_lexical_path(self.scratch_root, self.workspace_root / "scratch"):
            raise _unsafe_error("scratch root is not canonical")
        if self.lifecycle is WorkspaceLifecycle.PROMOTED:
            expected = self.cuts_root / self.cache_key
            if not _same_lexical_path(self.path, expected):
                raise _unsafe_error("promoted workspace path is not canonical")
        elif self.lifecycle is WorkspaceLifecycle.STAGING:
            if (
                self.path.parent != self.cuts_root
                or _STAGE_RE.fullmatch(self.path.name) is None
            ):
                raise _unsafe_error("staging workspace path is not canonical")
            if not self.path.name.startswith(f".stage-{self.cache_key}-"):
                raise _unsafe_error("staging workspace key does not match its path")
        elif self.lifecycle is WorkspaceLifecycle.SNAPSHOT:
            if (
                self.path.name != "cuts-snapshot"
                or self.path.parent.parent != self.scratch_root
            ):
                raise _unsafe_error("snapshot workspace path is not canonical")
        else:  # pragma: no cover - enum exhaustiveness
            raise _unsafe_error("unknown workspace lifecycle")

    @classmethod
    def create_staging(
        cls, output_directory: Path, cache_key: str, job_id: str
    ) -> Self:
        _validate_cache_key(cache_key)
        _validate_job_id(job_id)
        output, root, cuts, scratch = _workspace_layout(output_directory, create=True)
        _recover_promotion(cuts, cache_key)
        stage = cuts / f".stage-{cache_key}-{job_id}"
        try:
            os.mkdir(stage, 0o700)
            _assert_safe_directory(stage)
        except FileExistsError as error:
            raise _stage_error(
                f"staging directory already exists for job {job_id!r}"
            ) from error
        except AppError:
            raise
        except OSError as error:
            raise _stage_error(
                f"cannot create staged cut directory: {error}"
            ) from error
        return cls(
            output, root, cuts, scratch, cache_key, stage, WorkspaceLifecycle.STAGING
        )

    @classmethod
    def open(cls, output_directory: Path, cache_key: str) -> Self:
        _validate_cache_key(cache_key)
        output, root, cuts, scratch = _workspace_layout(output_directory, create=False)
        if cuts.exists():
            _recover_promotion(cuts, cache_key)
        return cls(
            output,
            root,
            cuts,
            scratch,
            cache_key,
            cuts / cache_key,
            WorkspaceLifecycle.PROMOTED,
        )

    def read_promoted_cut(
        self,
        index: int,
        *,
        rgba_ownership_tracker: RgbaOwnershipTracker | None = None,
    ) -> Image.Image:
        """Load exactly one owned RGBA image; never retain the animation."""
        if self.lifecycle is WorkspaceLifecycle.STAGING:
            raise _set_error("staged cuts are not a promoted read source")
        if self.lifecycle is WorkspaceLifecycle.PROMOTED:
            manifest = detect_external_edits(self)
        else:
            manifest, _manifest_identity = _read_manifest(self.path)
        if manifest.cache_key != self.cache_key:
            raise _set_error("workspace cache key does not match its manifest")
        _validate_frame_index(index)
        if index >= manifest.frame_count:
            raise _set_error(f"frame index {index} is outside the promoted cut set")
        expected = manifest.frames[index]
        _inspect_frame(self.path, expected, compare_recorded=True, load_pixels=False)
        try:
            with _BoundDirectory.open(self.path) as bound:
                descriptor = bound.open_read(expected.filename)
                with os.fdopen(descriptor, "rb") as source:
                    with Image.open(source) as opened:
                        opened.load()
                        result = opened.copy()
                bound.assert_still_named()
        except AppError:
            raise
        except (OSError, UnidentifiedImageError, ValueError) as error:
            raise _set_error(
                f"cannot read promoted frame {expected.filename}: {error}"
            ) from error
        if result.mode != "RGBA" or result.size != (manifest.width, manifest.height):
            result.close()
            raise _set_error(f"promoted frame {expected.filename} is not exact RGBA")
        if rgba_ownership_tracker is not None:
            rgba_ownership_tracker.register(result)
        return result

    def set_pinned(self, pinned: bool, *, now_ns: int | None = None) -> CutManifest:
        if type(pinned) is not bool:
            raise TypeError("pinned must be a bool")
        manifest = detect_external_edits(self, now_ns=now_ns)
        updated = replace(manifest, pinned=pinned)
        _write_manifest_atomic(self.path, updated)
        return validate_cut_set(self)


@dataclass(frozen=True, slots=True)
class WorkspaceSummary:
    workspace: CutWorkspace
    manifest: CutManifest
    size_bytes: int

    @property
    def source_path(self) -> str:
        return self.manifest.source_path

    @property
    def last_used_at_ns(self) -> int:
        return self.manifest.last_used_at_ns

    @property
    def edited(self) -> bool:
        return self.manifest.edited

    @property
    def pinned(self) -> bool:
        return self.manifest.pinned


@dataclass(frozen=True, slots=True)
class WorkspaceListing(Sequence[WorkspaceSummary]):
    entries: tuple[WorkspaceSummary, ...]
    total_size_bytes: int
    warning_threshold_bytes: int
    warning_required: bool

    @overload
    def __getitem__(self, index: int) -> WorkspaceSummary: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[WorkspaceSummary, ...]: ...

    def __getitem__(
        self, index: int | slice
    ) -> WorkspaceSummary | tuple[WorkspaceSummary, ...]:
        return self.entries[index]

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True, slots=True)
class ScratchCleanupResult:
    removed_count: int
    removed_bytes: int
    has_more: bool


def stage_cut(workspace: CutWorkspace, index: int, image: Image.Image) -> CutFrame:
    """Persist one sequential RGBA PNG into a private sibling stage."""
    _require_workspace(workspace)
    if workspace.lifecycle is not WorkspaceLifecycle.STAGING:
        raise _stage_error("stage_cut requires a staging workspace")
    _validate_frame_index(index)
    if not isinstance(image, Image.Image) or image.mode != "RGBA":
        raise _stage_error("staged cut must be a real Pillow RGBA image")
    _validate_dimensions(*image.size)
    filename = _frame_filename(index)
    try:
        with _BoundDirectory.open(workspace.path) as bound:
            existing_names: set[str] = set()
            for name, info in bound.iter_entries():
                if name == MANIFEST_FILENAME or name.startswith(".manifest-"):
                    continue
                match = _FRAME_RE.fullmatch(name)
                if match is None or not stat.S_ISREG(info.st_mode):
                    raise _stage_error(f"unexpected staged entry {name!r}")
                existing_names.add(name)
                if len(existing_names) > MAX_FRAME_COUNT:
                    raise _stage_error("staged frame count exceeds the bound")
            expected_names = {_frame_filename(value) for value in range(index)}
            if existing_names != expected_names:
                detail = (
                    f"staged frame {index} is not sequential; existing canonical "
                    "names contain a gap"
                )
                raise _stage_error(detail)
            output = bound.open_new(filename)
            try:
                image.save(output, format="PNG")
                output.flush()
                os.fsync(output.fileno())
            finally:
                output.close()
            bound.fsync()
            bound.assert_still_named()
    except AppError:
        raise
    except (OSError, ValueError) as error:
        raise _stage_error(f"cannot persist frame {filename}: {error}") from error
    frame, _identity = _inspect_frame(
        workspace.path,
        CutFrame(index, filename, image.width, image.height, 1, 0, "0" * 64),
        compare_recorded=False,
        load_pixels=True,
    )
    return frame


def promote_cut_set(
    workspace: CutWorkspace, manifest: CutManifest | None = None
) -> CutWorkspace:
    """Validate and atomically publish a sibling stage without losing old cuts."""
    _require_workspace(workspace)
    if workspace.lifecycle is not WorkspaceLifecycle.STAGING:
        raise _promotion_error("promotion requires a staging workspace")
    try:
        if manifest is not None:
            if (
                type(manifest) is not CutManifest
                or manifest.cache_key != workspace.cache_key
            ):
                raise _manifest_error(
                    "promotion manifest does not match staging cache key"
                )
            _write_manifest_atomic(workspace.path, manifest)
        candidate = validate_cut_set(workspace)
    except AppError as error:
        try:
            if workspace.path.exists():
                _remove_tree(workspace.path)
        except OSError as cleanup_error:
            error.add_note(f"additional staged-cut cleanup failure: {cleanup_error}")
        raise
    target = workspace.cuts_root / workspace.cache_key
    marker = workspace.cuts_root / f".replace-{workspace.cache_key}.json"
    token = uuid.uuid4().hex
    backup = workspace.cuts_root / f".backup-{workspace.cache_key}-{token}"
    lock = _promotion_lock(str(target))
    with lock:
        _recover_promotion(workspace.cuts_root, workspace.cache_key)
        previous_hash: str | None = None
        target_exists = _entry_exists_no_follow(target)
        if target_exists:
            previous, _identity = _read_manifest(target)
            previous_hash = hashlib.sha256(previous.to_json_bytes()).hexdigest()
        journal: dict[str, object] = {
            "backup_name": backup.name,
            "cache_key": workspace.cache_key,
            "candidate_manifest_sha256": hashlib.sha256(
                candidate.to_json_bytes()
            ).hexdigest(),
            "phase": "prepared",
            "previous_manifest_sha256": previous_hash,
            "stage_name": workspace.path.name,
            "used_exchange": False,
            "version": 1,
        }
        _write_journal(marker, journal)
        old_location: Path | None = None
        try:
            if target_exists:
                exchanged = _atomic_directory_exchange(workspace.path, target)
                if exchanged:
                    old_location = workspace.path
                    journal["phase"] = "new-active"
                    journal["used_exchange"] = True
                    _write_journal(marker, journal)
                else:
                    os.replace(target, backup)
                    old_location = backup
                    journal["phase"] = "old-moved"
                    _write_journal(marker, journal)
                    os.replace(workspace.path, target)
                    journal["phase"] = "new-active"
                    _write_journal(marker, journal)
            else:
                os.replace(workspace.path, target)
                journal["phase"] = "new-active"
                _write_journal(marker, journal)
            # Do not call ``open`` while this operation owns the journal: open()
            # performs crash recovery for observers and would correctly consume
            # the still-live marker before this transaction has finished.
            promoted = CutWorkspace(
                workspace.output_directory,
                workspace.workspace_root,
                workspace.cuts_root,
                workspace.scratch_root,
                workspace.cache_key,
                target,
                WorkspaceLifecycle.PROMOTED,
            )
            validated = validate_cut_set(promoted)
            if validated.to_json_bytes() != candidate.to_json_bytes():
                raise _promotion_error("promoted manifest changed during replacement")
            _fsync_directory(workspace.cuts_root)
            if old_location is not None and old_location.exists():
                _remove_tree(old_location)
            _unlink_regular(marker)
            _fsync_directory(workspace.cuts_root)
            return promoted
        except AppError:
            raise
        except OSError as error:
            raise _promotion_error(f"atomic cut promotion failed: {error}") from error
        finally:
            if marker.exists():
                try:
                    _recover_promotion(workspace.cuts_root, workspace.cache_key)
                except AppError:
                    pass
                except OSError:
                    pass


def validate_cut_set(workspace: CutWorkspace) -> CutManifest:
    """Validate manifest, namespace, frame bytes, metadata, and exact hashes."""
    _require_workspace(workspace)
    try:
        manifest, manifest_identity = _read_manifest(workspace.path)
        if manifest.cache_key != workspace.cache_key:
            raise _set_error("manifest cache key does not match workspace")
        _scan_cut_set(
            workspace.path,
            manifest,
            manifest_identity,
            compare_recorded=True,
        )
        return manifest
    except AppError:
        raise
    except OSError as error:
        raise _set_error(f"cannot validate cut workspace: {error}") from error


def detect_external_edits(
    workspace: CutWorkspace, *, now_ns: int | None = None
) -> CutManifest:
    """Rescan valid cuts, persist current hashes, and invalidate derived union data."""
    _require_workspace(workspace)
    if workspace.lifecycle is WorkspaceLifecycle.STAGING:
        raise _set_error("external edit detection requires durable cuts")
    manifest, manifest_identity = _read_manifest(workspace.path)
    frames, _identities = _scan_cut_set(
        workspace.path,
        manifest,
        manifest_identity,
        compare_recorded=False,
    )
    timestamp = time.time_ns() if now_ns is None else now_ns
    _bounded_int(timestamp, "last-use timestamp", minimum=0, maximum=_MAX_INT64)
    changed = frames != manifest.frames
    updated = replace(
        manifest,
        frames=frames,
        edited=manifest.edited or changed,
        union_metadata=None if changed else manifest.union_metadata,
        last_used_at_ns=max(timestamp, manifest.created_at_ns),
    )
    if updated != manifest:
        _write_manifest_atomic(workspace.path, updated)
    return validate_cut_set(workspace)


def snapshot_for_rebuild(
    workspace: CutWorkspace,
    scratch_directory: Path,
    *,
    cancelled: CancellationCheck | None = None,
    prefer_reflink: bool = True,
) -> CutWorkspace:
    """Create one private, immutable frame set with a stable rescan boundary."""
    _require_workspace(workspace)
    if workspace.lifecycle is WorkspaceLifecycle.STAGING:
        raise _snapshot_error("cannot snapshot an unpromoted cut set")
    if not isinstance(scratch_directory, Path):
        raise _unsafe_error("scratch directory must be a Path")
    _validate_path_value(scratch_directory)
    if scratch_directory.parent != workspace.scratch_root:
        raise _unsafe_error("snapshot must use scratch/<job-id> under its workspace")
    _validate_job_id(scratch_directory.name)
    check_cancelled = cancelled if cancelled is not None else _not_cancelled
    if not callable(check_cancelled):
        raise TypeError("cancelled must be callable")
    snapshot_path = scratch_directory / "cuts-snapshot"
    started = False
    try:
        _raise_if_cancelled(check_cancelled)
        # Validate before allocating scratch so pre-existing corruption keeps
        # its precise CUT_SET_INVALID diagnosis. A change after this baseline
        # is instead the retryable snapshot race.
        current = detect_external_edits(workspace)
        baseline, manifest_identity = _read_manifest(workspace.path)
        if baseline != current:
            raise _set_error("manifest changed before snapshot copying")
        _frames, baseline_identities = _scan_cut_set(
            workspace.path,
            baseline,
            manifest_identity,
            compare_recorded=True,
        )
        _raise_if_cancelled(check_cancelled)
        _assert_safe_directory(workspace.scratch_root)
        os.mkdir(scratch_directory, 0o700)
        started = True
        os.mkdir(snapshot_path, 0o700)
        for frame in baseline.frames:
            _raise_if_cancelled(check_cancelled)
            _copy_frame_descriptor_bound(
                workspace.path,
                snapshot_path,
                frame,
                prefer_reflink=prefer_reflink,
            )
            _raise_if_cancelled(check_cancelled)
        _write_manifest_atomic(snapshot_path, baseline)
        _raise_if_cancelled(check_cancelled)
        after, after_manifest_identity = _read_manifest(workspace.path)
        after_frames, after_identities = _scan_cut_set(
            workspace.path,
            after,
            after_manifest_identity,
            compare_recorded=True,
        )
        if (
            after != baseline
            or after_frames != baseline.frames
            or after_identities != baseline_identities
        ):
            raise _cuts_changed("cut frames changed during the full snapshot operation")
        snapshot = CutWorkspace(
            workspace.output_directory,
            workspace.workspace_root,
            workspace.cuts_root,
            workspace.scratch_root,
            workspace.cache_key,
            snapshot_path,
            WorkspaceLifecycle.SNAPSHOT,
        )
        validate_cut_set(snapshot)
        return snapshot
    except AppError as error:
        if started:
            _cleanup_snapshot(scratch_directory, error)
        if (
            error.code
            in {
                ErrorCode.CUT_SET_INVALID,
                ErrorCode.CUT_MANIFEST_INVALID,
                ErrorCode.CUT_WORKSPACE_UNSAFE,
            }
            and started
        ):
            raise _cuts_changed(error.technical_detail) from error
        raise
    except OSError as error:
        failure = _snapshot_error(f"cannot create rebuild snapshot: {error}")
        if started:
            _cleanup_snapshot(scratch_directory, failure)
        raise failure from error


def list_workspaces(
    output_directory: Path,
    *,
    warning_threshold_bytes: int = WORKSPACE_WARNING_BYTES,
) -> WorkspaceListing:
    """Return immutable durable-cache summaries; never remove a cut directory."""
    _bounded_int(
        warning_threshold_bytes,
        "warning threshold",
        minimum=0,
        maximum=_MAX_INT64,
    )
    output, root, cuts, scratch = _workspace_layout(output_directory, create=False)
    if not cuts.exists():
        return WorkspaceListing((), 0, warning_threshold_bytes, False)
    _recover_all_promotions(cuts)
    summaries: list[WorkspaceSummary] = []
    with _BoundDirectory.open(cuts) as bound:
        seen = 0
        entries = list(bound.iter_entries())
        if len(entries) > MAX_WORKSPACE_ENTRIES * 3:
            raise _unsafe_error("workspace namespace exceeds the listing bound")
        for name, info in entries:
            if _CACHE_KEY_RE.fullmatch(name) is None:
                continue
            seen += 1
            if seen > MAX_WORKSPACE_ENTRIES:
                raise _unsafe_error("workspace count exceeds the listing bound")
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _unsafe_error(f"workspace entry {name!r} is redirected")
            workspace = CutWorkspace(
                output,
                root,
                cuts,
                scratch,
                name,
                cuts / name,
                WorkspaceLifecycle.PROMOTED,
            )
            manifest = validate_cut_set(workspace)
            size_bytes = sum(frame.size_bytes for frame in manifest.frames)
            size_bytes += (workspace.path / MANIFEST_FILENAME).stat().st_size
            summaries.append(WorkspaceSummary(workspace, manifest, size_bytes))
        bound.assert_still_named()
    summaries.sort(key=lambda item: (-item.last_used_at_ns, item.workspace.cache_key))
    total = sum(item.size_bytes for item in summaries)
    return WorkspaceListing(
        tuple(summaries),
        total,
        warning_threshold_bytes,
        total > warning_threshold_bytes,
    )


def delete_workspace(workspace: CutWorkspace, *, allow_pinned: bool = False) -> None:
    """Explicitly delete exactly one durable cut directory."""
    _require_workspace(workspace)
    if workspace.lifecycle is not WorkspaceLifecycle.PROMOTED:
        raise _delete_error("only a durable promoted workspace can be deleted")
    if type(allow_pinned) is not bool:
        raise TypeError("allow_pinned must be a bool")
    lock = _promotion_lock(str(workspace.path))
    with lock:
        _recover_promotion(workspace.cuts_root, workspace.cache_key)
        manifest = validate_cut_set(workspace)
        if manifest.pinned and not allow_pinned:
            raise AppError(
                ErrorCode.CUT_WORKSPACE_PINNED,
                "cut-workspace-delete",
                "error.cuts.pinned",
                "pinned cut workspace requires an explicit delete override",
                "confirm-delete-pinned-workspace",
            )
        try:
            _remove_tree(workspace.path)
            _fsync_directory(workspace.cuts_root)
        except OSError as error:
            raise _delete_error(f"cannot delete cut workspace: {error}") from error


def cleanup_abandoned_scratch(
    output_directory: Path,
    *,
    older_than_ns: int = ABANDONED_SCRATCH_AGE_NS,
    now_ns: int | None = None,
    max_entries: int = 256,
) -> ScratchCleanupResult:
    """Explicitly remove at most *max_entries* scratch jobs older than 24 hours."""
    _bounded_int(
        older_than_ns,
        "scratch abandonment age",
        minimum=ABANDONED_SCRATCH_AGE_NS,
        maximum=_MAX_INT64,
    )
    _bounded_int(max_entries, "scratch cleanup count", minimum=1, maximum=1024)
    timestamp = time.time_ns() if now_ns is None else now_ns
    _bounded_int(timestamp, "current timestamp", minimum=0, maximum=_MAX_INT64)
    _output, _root, _cuts, scratch = _workspace_layout(output_directory, create=False)
    if not scratch.exists():
        return ScratchCleanupResult(0, 0, False)
    candidates: list[tuple[int, Path, int]] = []
    with _BoundDirectory.open(scratch) as bound:
        scanned = 0
        for name, info in bound.iter_entries():
            scanned += 1
            if scanned > MAX_SCRATCH_ENTRIES:
                raise _unsafe_error("scratch namespace exceeds the cleanup bound")
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _unsafe_error(f"scratch entry {name!r} is not a safe directory")
            if timestamp - info.st_mtime_ns > older_than_ns:
                candidates.append(
                    (
                        info.st_mtime_ns,
                        scratch / name,
                        _bounded_tree_size(scratch / name),
                    )
                )
        bound.assert_still_named()
    candidates.sort(key=lambda item: (item[0], item[1].name))
    selected = candidates[:max_entries]
    removed_bytes = 0
    for _mtime, path, size_bytes in selected:
        try:
            _remove_tree(path)
        except OSError as error:
            raise _snapshot_error(
                f"cannot clean abandoned scratch {path.name!r}: {error}"
            ) from error
        removed_bytes += size_bytes
    if selected:
        _fsync_directory(scratch)
    return ScratchCleanupResult(
        len(selected), removed_bytes, len(candidates) > len(selected)
    )


def _scan_cut_set(
    path: Path,
    manifest: CutManifest,
    manifest_identity: tuple[int, int, int, int, int],
    *,
    compare_recorded: bool,
) -> tuple[tuple[CutFrame, ...], tuple[tuple[int, int, int, int, int], ...]]:
    expected_names = {MANIFEST_FILENAME, *(frame.filename for frame in manifest.frames)}
    try:
        with _BoundDirectory.open(path) as bound:
            actual_names: set[str] = set()
            for name, info in bound.iter_entries():
                if len(actual_names) > MAX_FRAME_COUNT + 1:
                    raise _set_error("cut directory entry count exceeds the bound")
                actual_names.add(name)
                if stat.S_ISLNK(info.st_mode):
                    raise _unsafe_error(f"cut entry {name!r} is a symbolic link")
                if not stat.S_ISREG(info.st_mode):
                    raise _set_error(f"cut entry {name!r} is not a regular file")
            if actual_names != expected_names:
                missing = sorted(expected_names - actual_names)
                unexpected = sorted(actual_names - expected_names)
                detail = "cut frame names/count are not sequential"
                if missing:
                    detail += f"; missing {missing[0]!r}"
                if unexpected:
                    detail += f"; unexpected {unexpected[0]!r}"
                raise _set_error(detail)
            current_manifest = bound.lstat(MANIFEST_FILENAME)
            if _stat_identity(current_manifest) != manifest_identity:
                raise _set_error("manifest changed during cut validation")
            bound.assert_still_named()
    except AppError:
        raise
    except OSError as error:
        raise _set_error(f"cannot inspect cut directory: {error}") from error
    frames: list[CutFrame] = []
    identities: list[tuple[int, int, int, int, int]] = []
    for expected in manifest.frames:
        frame, identity = _inspect_frame(
            path,
            expected,
            compare_recorded=compare_recorded,
            load_pixels=True,
        )
        if (frame.width, frame.height) != (manifest.width, manifest.height):
            raise _set_error(f"frame {frame.filename} dimensions do not match manifest")
        frames.append(frame)
        identities.append(identity)
    try:
        with _BoundDirectory.open(path) as bound:
            after_manifest = bound.lstat(MANIFEST_FILENAME)
            if _stat_identity(after_manifest) != manifest_identity:
                raise _set_error("manifest changed during frame validation")
            bound.assert_still_named()
    except AppError:
        raise
    except OSError as error:
        raise _set_error(f"manifest became unavailable: {error}") from error
    return tuple(frames), tuple(identities)


def _inspect_frame(
    directory: Path,
    expected: CutFrame,
    *,
    compare_recorded: bool,
    load_pixels: bool,
) -> tuple[CutFrame, tuple[int, int, int, int, int]]:
    try:
        with _BoundDirectory.open(directory) as bound:
            descriptor = bound.open_read(expected.filename)
            with os.fdopen(descriptor, "rb") as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise _unsafe_error(f"frame {expected.filename} is not regular")
                if not 1 <= before.st_size <= MAX_FRAME_FILE_BYTES:
                    raise _set_error(
                        f"frame {expected.filename} has an invalid byte size"
                    )
                header = source.read(33)
                width, height = _parse_png_header(header, expected.filename)
                _validate_dimensions(width, height)
                source.seek(0)
                with Image.open(source) as image:
                    if (
                        image.format != "PNG"
                        or image.mode != "RGBA"
                        or image.size != (width, height)
                        or getattr(image, "n_frames", 1) != 1
                    ):
                        raise _set_error(
                            f"frame {expected.filename} must be one exact RGBA PNG"
                        )
                    if load_pixels:
                        image.load()
                    else:
                        image.verify()
                source.seek(0)
                digest = _hash_file(source)
                after = os.fstat(source.fileno())
            named = bound.lstat(expected.filename)
            bound.assert_still_named()
    except AppError:
        raise
    except (OSError, UnidentifiedImageError, SyntaxError, ValueError) as error:
        raise _set_error(
            f"frame {expected.filename} is not a readable PNG: {error}"
        ) from error
    identity = _stat_identity(before)
    if identity != _stat_identity(after) or identity != _stat_identity(named):
        raise _set_error(f"frame {expected.filename} changed while it was read")
    current = CutFrame(
        expected.index,
        expected.filename,
        width,
        height,
        before.st_size,
        before.st_mtime_ns,
        digest,
    )
    if compare_recorded:
        if (current.width, current.height) != (expected.width, expected.height):
            raise _set_error(f"frame {expected.filename} dimensions changed")
        if current.size_bytes != expected.size_bytes:
            raise _set_error(f"frame {expected.filename} byte size changed")
        if current.mtime_ns != expected.mtime_ns:
            raise _set_error(f"frame {expected.filename} metadata changed")
        if current.sha256 != expected.sha256:
            raise _set_error(f"frame {expected.filename} content hash changed")
    return current, identity


def _copy_frame_descriptor_bound(
    source_directory: Path,
    destination_directory: Path,
    frame: CutFrame,
    *,
    prefer_reflink: bool,
) -> None:
    try:
        with (
            _BoundDirectory.open(source_directory) as source_bound,
            _BoundDirectory.open(destination_directory) as destination_bound,
        ):
            source_fd = source_bound.open_read(frame.filename)
            try:
                before = os.fstat(source_fd)
                if _stat_identity(source_bound.lstat(frame.filename)) != _stat_identity(
                    before
                ):
                    raise _cuts_changed(
                        f"frame {frame.filename} was redirected before copy"
                    )
                if (
                    before.st_size != frame.size_bytes
                    or before.st_mtime_ns != frame.mtime_ns
                ):
                    raise _cuts_changed(
                        f"frame {frame.filename} metadata changed before copy"
                    )
                before_hash = _sha256_fd(source_fd)
                if before_hash != frame.sha256:
                    raise _cuts_changed(f"frame {frame.filename} changed before copy")
                destination_fd: int | None = None
                if prefer_reflink:
                    destination_fd = _try_reflink(
                        source_fd, destination_bound, frame.filename
                    )
                if destination_fd is None:
                    destination_fd = destination_bound.open_new_fd(frame.filename)
                    try:
                        os.lseek(source_fd, 0, os.SEEK_SET)
                        while True:
                            chunk = os.read(source_fd, COPY_CHUNK_BYTES)
                            if not chunk:
                                break
                            _write_all(destination_fd, chunk)
                    except BaseException:
                        os.close(destination_fd)
                        destination_fd = None
                        raise
                assert destination_fd is not None
                try:
                    os.fsync(destination_fd)
                    os.utime(
                        destination_fd,
                        ns=(before.st_atime_ns, before.st_mtime_ns),
                    )
                    destination_hash = _sha256_fd(destination_fd)
                    destination_info = os.fstat(destination_fd)
                finally:
                    os.close(destination_fd)
                after_hash = _sha256_fd(source_fd)
                after = os.fstat(source_fd)
            finally:
                os.close(source_fd)
            named_after = source_bound.lstat(frame.filename)
            source_bound.assert_still_named()
            destination_bound.assert_still_named()
    except AppError:
        raise
    except OSError as error:
        raise _snapshot_error(f"cannot copy frame {frame.filename}: {error}") from error
    if (
        _stat_identity(before) != _stat_identity(after)
        or _stat_identity(before) != _stat_identity(named_after)
        or before_hash != after_hash
        or after_hash != frame.sha256
    ):
        raise _cuts_changed(f"frame {frame.filename} changed during copy")
    if (
        destination_hash != frame.sha256
        or destination_info.st_size != frame.size_bytes
        or destination_info.st_mtime_ns != frame.mtime_ns
    ):
        raise _snapshot_error(f"private copy of {frame.filename} failed verification")


def _try_reflink(
    source_fd: int, destination: _BoundDirectory, filename: str
) -> int | None:
    unsupported = {
        errno.EINVAL,
        errno.ENOTSUP,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
        errno.EXDEV,
        errno.ENOSYS,
    }
    if sys.platform.startswith("linux"):
        destination_fd = destination.open_new_fd(filename)
        try:
            try:
                fcntl.ioctl(destination_fd, 0x40049409, source_fd)
            except OSError as error:
                if error.errno not in unsupported:
                    raise
                os.close(destination_fd)
                destination_fd = -1
                destination.unlink(filename)
                return None
            return destination_fd
        except BaseException:
            if destination_fd >= 0:
                os.close(destination_fd)
            try:
                destination.unlink(filename)
            except OSError:
                pass
            raise
    if sys.platform == "darwin" and destination.descriptor is not None:
        libc = ctypes.CDLL(None, use_errno=True)
        clone = getattr(libc, "fclonefileat", None)
        if clone is None:
            return None
        clone.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        clone.restype = ctypes.c_int
        if clone(source_fd, destination.descriptor, os.fsencode(filename), 0) == 0:
            return destination.open_read_write(filename)
        code = ctypes.get_errno()
        if code in unsupported:
            return None
        raise OSError(code, os.strerror(code), filename)
    return None


class _BoundDirectory:
    __slots__ = ("descriptor", "path")

    def __init__(self, path: Path, descriptor: int | None) -> None:
        self.path = path
        self.descriptor = descriptor

    @classmethod
    def open(cls, path: Path) -> Self:
        _assert_safe_directory(path)
        descriptor: int | None = None
        if os.name != "nt":
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            named = path.lstat()
            if _directory_identity(opened) != _directory_identity(named):
                os.close(descriptor)
                raise _unsafe_error("workspace directory changed while binding")
        return cls(path, descriptor)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        descriptor = self.descriptor
        self.descriptor = None
        if descriptor is not None:
            os.close(descriptor)

    def assert_still_named(self) -> None:
        _assert_safe_directory(self.path)
        if self.descriptor is not None:
            opened = os.fstat(self.descriptor)
            named = self.path.lstat()
            if _directory_identity(opened) != _directory_identity(named):
                raise _unsafe_error("bound workspace directory was redirected")

    def lstat(self, name: str) -> os.stat_result:
        _validate_component(name)
        if self.descriptor is not None:
            return os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        return (self.path / name).lstat()

    def open_read(self, name: str) -> int:
        _validate_component(name)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            if self.descriptor is not None:
                return os.open(name, flags, dir_fd=self.descriptor)
            return os.open(self.path / name, flags)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise _unsafe_error(
                    f"workspace entry {name!r} is redirected"
                ) from error
            raise

    def open_read_write(self, name: str) -> int:
        _validate_component(name)
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if self.descriptor is not None:
            return os.open(name, flags, dir_fd=self.descriptor)
        return os.open(self.path / name, flags)

    def open_new_fd(self, name: str) -> int:
        _validate_component(name)
        # Every staged/snapshot output is hashed again through this same bound
        # descriptor before it is trusted, so the descriptor must be readable.
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if self.descriptor is not None:
            return os.open(name, flags, 0o600, dir_fd=self.descriptor)
        return os.open(self.path / name, flags, 0o600)

    def open_new(self, name: str) -> BinaryIO:
        descriptor = self.open_new_fd(name)
        try:
            return os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise

    def replace(self, source: str, destination: str) -> None:
        _validate_component(source)
        _validate_component(destination)
        if self.descriptor is not None:
            os.replace(
                source,
                destination,
                src_dir_fd=self.descriptor,
                dst_dir_fd=self.descriptor,
            )
        else:
            os.replace(self.path / source, self.path / destination)

    def unlink(self, name: str) -> None:
        _validate_component(name)
        if self.descriptor is not None:
            os.unlink(name, dir_fd=self.descriptor)
        else:
            (self.path / name).unlink()

    def iter_entries(self) -> Iterator[tuple[str, os.stat_result]]:
        target: int | Path = (
            self.descriptor if self.descriptor is not None else self.path
        )
        with os.scandir(target) as entries:
            for entry in entries:
                yield entry.name, entry.stat(follow_symlinks=False)

    def fsync(self) -> None:
        if self.descriptor is not None:
            _fsync_fd(self.descriptor)


def _workspace_layout(
    output_directory: Path, *, create: bool
) -> tuple[Path, Path, Path, Path]:
    output = _canonical_output_directory(output_directory)
    root = output / ".rembggui-work"
    cuts = root / "cuts"
    scratch = root / "scratch"
    if create:
        for path in (root, cuts, scratch):
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError as error:
                raise _unsafe_error(
                    f"cannot create workspace directory: {error}"
                ) from error
            _assert_safe_directory(path)
        _assert_local_filesystem(root)
    elif root.exists():
        _assert_safe_directory(root)
        _assert_local_filesystem(root)
        if cuts.exists():
            _assert_safe_directory(cuts)
        if scratch.exists():
            _assert_safe_directory(scratch)
    return output, root, cuts, scratch


def _canonical_output_directory(path: Path) -> Path:
    if not isinstance(path, Path):
        raise _unsafe_error("output directory must be a Path")
    _validate_path_value(path)
    if ".." in path.parts:
        raise _unsafe_error("output directory traversal is not allowed")
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise _unsafe_error("output directory must exist locally") from error
    if not _same_lexical_path(absolute, resolved):
        raise _unsafe_error("output directory may not contain symbolic links")
    _assert_safe_directory(absolute)
    _assert_local_filesystem(absolute)
    return absolute


def _assert_safe_directory(path: Path) -> None:
    _validate_path_value(path)
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    try:
        for component in absolute.parts[1:]:
            _validate_component(component)
            current /= component
            info = current.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or current.is_symlink()
                or current.is_junction()
                or not stat.S_ISDIR(info.st_mode)
            ):
                raise _unsafe_error(f"workspace component {component!r} is redirected")
    except AppError:
        raise
    except OSError as error:
        raise _unsafe_error(f"workspace directory is unavailable: {error}") from error


def _assert_local_filesystem(path: Path) -> None:
    if os.name == "nt":
        text = str(path)
        if text.startswith(("\\\\", "//")):
            raise _unsafe_error(
                "network workspaces are outside the local-filesystem contract"
            )
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            drive = Path(path.anchor)
            drive_type = int(kernel32.GetDriveTypeW(str(drive)))
            if drive_type == 4:
                raise _unsafe_error(
                    "network workspaces are outside the local-filesystem contract"
                )
        except AttributeError:
            return
    else:
        flags = os.statvfs(path).f_flag
        local_flag = getattr(os, "ST_LOCAL", None)
        if local_flag is not None and not flags & local_flag:
            raise _unsafe_error("workspace filesystem is not local")


def _read_manifest(path: Path) -> tuple[CutManifest, tuple[int, int, int, int, int]]:
    try:
        with _BoundDirectory.open(path) as bound:
            descriptor = bound.open_read(MANIFEST_FILENAME)
            with os.fdopen(descriptor, "rb") as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise _unsafe_error("manifest is not a regular file")
                if not 1 <= before.st_size <= MAX_MANIFEST_BYTES:
                    raise _manifest_error("manifest exceeds the bounded byte limit")
                encoded = source.read(MAX_MANIFEST_BYTES + 1)
                after = os.fstat(source.fileno())
            named = bound.lstat(MANIFEST_FILENAME)
            bound.assert_still_named()
    except AppError:
        raise
    except OSError as error:
        raise _manifest_error(f"cannot read manifest: {error}") from error
    identity = _stat_identity(before)
    if (
        len(encoded) != before.st_size
        or identity != _stat_identity(after)
        or identity != _stat_identity(named)
    ):
        raise _manifest_error("manifest changed while it was read")
    return CutManifest.from_json_bytes(encoded), identity


def _write_manifest_atomic(path: Path, manifest: CutManifest) -> None:
    if type(manifest) is not CutManifest:
        raise _manifest_error("manifest must be an exact CutManifest")
    encoded = manifest.to_json_bytes()
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise _manifest_error("manifest exceeds the bounded byte limit")
    temporary = f".manifest-{uuid.uuid4().hex}.tmp"
    try:
        with _BoundDirectory.open(path) as bound:
            output = bound.open_new(temporary)
            try:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            finally:
                output.close()
            bound.replace(temporary, MANIFEST_FILENAME)
            bound.fsync()
            bound.assert_still_named()
    except AppError:
        raise
    except OSError as error:
        raise _manifest_error(f"cannot atomically write manifest: {error}") from error
    finally:
        temporary_path = path / temporary
        if temporary_path.exists():
            try:
                _unlink_regular(temporary_path)
            except OSError:
                pass


def _write_journal(path: Path, payload: Mapping[str, object]) -> None:
    encoded = _canonical_json(dict(payload)) + b"\n"
    if len(encoded) > 16 * 1024:
        raise _promotion_error("promotion journal exceeds its byte bound")
    temporary = f".{path.name}-{uuid.uuid4().hex}.tmp"
    with _BoundDirectory.open(path.parent) as bound:
        output = bound.open_new(temporary)
        try:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        finally:
            output.close()
        bound.replace(temporary, path.name)
        bound.fsync()


def _recover_all_promotions(cuts_root: Path) -> None:
    with _BoundDirectory.open(cuts_root) as bound:
        keys: list[str] = []
        for name, info in bound.iter_entries():
            match = _MARKER_RE.fullmatch(name)
            if match is None:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise _unsafe_error("promotion recovery marker is redirected")
            keys.append(match.group(1))
            if len(keys) > MAX_WORKSPACE_ENTRIES:
                raise _unsafe_error("promotion recovery marker count is unbounded")
    for key in keys:
        _recover_promotion(cuts_root, key)


def _recover_promotion(cuts_root: Path, cache_key: str) -> None:
    marker = cuts_root / f".replace-{cache_key}.json"
    try:
        try:
            with _BoundDirectory.open(cuts_root) as bound:
                marker_info = bound.lstat(marker.name)
                if stat.S_ISLNK(marker_info.st_mode) or not stat.S_ISREG(
                    marker_info.st_mode
                ):
                    raise _unsafe_error("promotion recovery marker is redirected")
                descriptor = bound.open_read(marker.name)
                with os.fdopen(descriptor, "rb") as source:
                    opened_before = os.fstat(source.fileno())
                    if not 1 <= opened_before.st_size <= 16 * 1024:
                        raise _promotion_error(
                            "promotion journal exceeds its byte bound"
                        )
                    data = source.read(16 * 1024 + 1)
                    opened_after = os.fstat(source.fileno())
                named_after = bound.lstat(marker.name)
                bound.assert_still_named()
        except FileNotFoundError:
            return
        marker_identity = _stat_identity(marker_info)
        if (
            len(data) != marker_info.st_size
            or marker_identity != _stat_identity(opened_before)
            or marker_identity != _stat_identity(opened_after)
            or marker_identity != _stat_identity(named_after)
        ):
            raise _promotion_error("promotion journal changed while it was read")
        payload = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(payload, dict):
            raise _promotion_error("promotion journal is not an object")
        _exact_keys(
            payload,
            {
                "backup_name",
                "cache_key",
                "candidate_manifest_sha256",
                "phase",
                "previous_manifest_sha256",
                "stage_name",
                "used_exchange",
                "version",
            },
            "promotion journal",
        )
        if payload["version"] != 1 or payload["cache_key"] != cache_key:
            raise _promotion_error("promotion journal identity is invalid")
        stage_name = _string(payload["stage_name"], "stage name")
        backup_name = _string(payload["backup_name"], "backup name")
        if _STAGE_RE.fullmatch(stage_name) is None or not stage_name.startswith(
            f".stage-{cache_key}-"
        ):
            raise _promotion_error("promotion journal stage is unsafe")
        if _BACKUP_RE.fullmatch(backup_name) is None or not backup_name.startswith(
            f".backup-{cache_key}-"
        ):
            raise _promotion_error("promotion journal backup is unsafe")
        candidate_hash = _string(
            payload["candidate_manifest_sha256"], "candidate manifest hash"
        )
        _validate_sha256(candidate_hash, "candidate manifest hash")
        previous_hash = payload["previous_manifest_sha256"]
        if previous_hash is not None:
            previous_hash = _string(previous_hash, "previous manifest hash")
            _validate_sha256(previous_hash, "previous manifest hash")
        target = cuts_root / cache_key
        stage = cuts_root / stage_name
        backup = cuts_root / backup_name
        target_hash = _manifest_hash_if_valid(target, cache_key)
        stage_hash = _manifest_hash_if_valid(stage, cache_key)
        backup_hash = _manifest_hash_if_valid(backup, cache_key)
        if target_hash == candidate_hash:
            if stage.exists():
                _remove_tree(stage)
            if backup.exists():
                _remove_tree(backup)
            _unlink_regular(marker)
            _fsync_directory(cuts_root)
            return
        old_location: Path | None = None
        if previous_hash is not None:
            if stage_hash == previous_hash:
                old_location = stage
            elif backup_hash == previous_hash:
                old_location = backup
            elif target_hash == previous_hash:
                old_location = target
        if old_location is not None and old_location != target:
            if target.exists():
                _remove_tree(target)
            os.replace(old_location, target)
        if stage.exists() and stage != old_location:
            _remove_tree(stage)
        if backup.exists() and backup != old_location:
            _remove_tree(backup)
        _unlink_regular(marker)
        _fsync_directory(cuts_root)
    except AppError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise _promotion_error(
            f"cannot recover interrupted promotion: {error}"
        ) from error


def _manifest_hash_if_valid(path: Path, cache_key: str) -> str | None:
    if not _entry_exists_no_follow(path):
        return None
    try:
        manifest, manifest_identity = _read_manifest(path)
        if manifest.cache_key != cache_key:
            return None
        _scan_cut_set(
            path,
            manifest,
            manifest_identity,
            compare_recorded=True,
        )
    except AppError:
        return None
    return hashlib.sha256(manifest.to_json_bytes()).hexdigest()


def _atomic_directory_exchange(left: Path, right: Path) -> bool:
    """Exchange two named sibling directories atomically when supported."""
    if left.parent != right.parent:
        raise OSError(errno.EXDEV, "directory exchange requires siblings")
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        renamex = getattr(libc, "renamex_np", None)
        if renamex is None:
            return False
        renamex.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex.restype = ctypes.c_int
        if renamex(os.fsencode(left), os.fsencode(right), 0x00000002) == 0:
            return True
        code = ctypes.get_errno()
        if code in {errno.ENOTSUP, errno.EINVAL, errno.ENOSYS}:
            return False
        raise OSError(code, os.strerror(code))
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            return False
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        if renameat2(-100, os.fsencode(left), -100, os.fsencode(right), 2) == 0:
            return True
        code = ctypes.get_errno()
        if code in {errno.ENOTSUP, errno.EINVAL, errno.ENOSYS}:
            return False
        raise OSError(code, os.strerror(code))
    return False


def _remove_tree(path: Path) -> None:
    """Remove one exact tree without ever traversing a link/reparse target."""
    if path.parent == path or not path.name:
        raise OSError("refusing to remove an unbounded path")
    if os.name != "nt":
        with _BoundDirectory.open(path.parent) as parent:
            info = parent.lstat(path.name)
            if stat.S_ISLNK(info.st_mode):
                parent.unlink(path.name)
                return
            if not stat.S_ISDIR(info.st_mode):
                raise OSError("tree target is not a directory")
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(path.name, flags, dir_fd=parent.descriptor)
            try:
                _remove_fd_contents(descriptor)
            finally:
                os.close(descriptor)
            os.rmdir(path.name, dir_fd=parent.descriptor)
            parent.fsync()
        return
    _remove_tree_windows(path)


def _remove_fd_contents(descriptor: int) -> None:
    with os.scandir(descriptor) as entries:
        names = [entry.name for entry in entries]
    if len(names) > MAX_FRAME_COUNT + 16:
        raise OSError("tree entry count exceeds cleanup bound")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    for name in names:
        _validate_component(name)
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            child = os.open(name, flags, dir_fd=descriptor)
            try:
                _remove_fd_contents(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)


def _remove_tree_windows(path: Path) -> None:
    info = path.lstat()
    if path.is_symlink() or path.is_junction() or stat.S_ISLNK(info.st_mode):
        path.unlink()
        return
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("tree target is not a directory")
    with os.scandir(path) as entries:
        children = list(entries)
    if len(children) > MAX_FRAME_COUNT + 16:
        raise OSError("tree entry count exceeds cleanup bound")
    for entry in children:
        child = path / entry.name
        child_info = child.lstat()
        if (
            stat.S_ISDIR(child_info.st_mode)
            and not child.is_symlink()
            and not child.is_junction()
        ):
            _remove_tree_windows(child)
        else:
            child.unlink()
    path.rmdir()


def _bounded_tree_size(path: Path) -> int:
    total = 0
    entries = 0
    stack = [path]
    while stack:
        current = stack.pop()
        with os.scandir(current) as children:
            for child in children:
                entries += 1
                if entries > MAX_FRAME_COUNT + 16:
                    raise _unsafe_error("scratch tree exceeds the size-scan bound")
                info = child.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise _unsafe_error("scratch tree contains a symbolic link")
                if stat.S_ISDIR(info.st_mode):
                    if (Path(current) / child.name).is_junction():
                        raise _unsafe_error("scratch tree contains a junction")
                    stack.append(Path(current) / child.name)
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                else:
                    raise _unsafe_error("scratch tree contains an unsafe entry")
    return total


def _cleanup_snapshot(path: Path, primary: AppError) -> None:
    if not path.exists():
        return
    try:
        _remove_tree(path)
    except OSError as cleanup_error:
        primary.add_note(f"additional scratch cleanup failure: {cleanup_error}")


def _parse_png_header(header: bytes, filename: str) -> tuple[int, int]:
    if (
        len(header) < 33
        or header[:8] != _PNG_SIGNATURE
        or header[12:16] != b"IHDR"
        or int.from_bytes(header[8:12], "big") != 13
    ):
        raise _set_error(f"frame {filename} is not a canonical PNG")
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    if header[24] != 8 or header[25] != 6:
        raise _set_error(f"frame {filename} must be 8-bit RGBA PNG")
    if header[26] != 0 or header[27] != 0 or header[28] not in {0, 1}:
        raise _set_error(f"frame {filename} has unsupported PNG encoding")
    return width, height


def _frame_from_payload(value: object) -> CutFrame:
    payload = _object(value, "frame")
    _exact_keys(
        payload,
        {"filename", "height", "index", "mtime_ns", "sha256", "size_bytes", "width"},
        "frame",
    )
    return CutFrame(
        index=_int(payload["index"], "frame index"),
        filename=_string(payload["filename"], "frame filename"),
        width=_int(payload["width"], "frame width"),
        height=_int(payload["height"], "frame height"),
        size_bytes=_int(payload["size_bytes"], "frame size"),
        mtime_ns=_int(payload["mtime_ns"], "frame mtime"),
        sha256=_string(payload["sha256"], "frame sha256"),
    )


def _validate_cache_inputs(inputs: FrozenJsonMap) -> None:
    _exact_keys(
        inputs,
        {
            "crop",
            "edge_settings",
            "model",
            "orientation_color_version",
            "pipeline_schema_version",
            "rembg_version",
            "sampling",
            "source_sha256",
        },
        "cache_key_inputs",
    )
    _validate_sha256(_string(inputs["source_sha256"], "source sha256"), "source sha256")
    sampling = _frozen_object(inputs["sampling"], "sampling")
    _exact_keys(sampling, {"end", "fps", "start"}, "sampling")
    start = _fraction_payload(sampling["start"], "sampling start")
    end = _fraction_payload(sampling["end"], "sampling end")
    if start < 0 or end <= start:
        raise _manifest_error("sampling interval must be positive and half-open")
    _bounded_int(_int(sampling["fps"], "fps"), "fps", minimum=1, maximum=240)
    crop = _frozen_object(inputs["crop"], "crop")
    _exact_keys(crop, {"height", "width", "x", "y"}, "crop")
    _bounded_int(
        _int(crop["x"], "crop x"), "crop x", minimum=0, maximum=MAX_CUT_DIMENSION
    )
    _bounded_int(
        _int(crop["y"], "crop y"), "crop y", minimum=0, maximum=MAX_CUT_DIMENSION
    )
    _validate_dimensions(
        _int(crop["width"], "crop width"), _int(crop["height"], "crop height")
    )
    model = _frozen_object(inputs["model"], "model")
    _exact_keys(model, {"id", "weight_sha256"}, "model")
    _bounded_text(_string(model["id"], "model id"), "model id")
    _validate_sha256(
        _string(model["weight_sha256"], "model weight sha256"),
        "model weight sha256",
    )
    for field in (
        "rembg_version",
        "pipeline_schema_version",
        "orientation_color_version",
    ):
        _bounded_text(_string(inputs[field], field), field)
    edge = _frozen_object(inputs["edge_settings"], "edge_settings")
    _exact_keys(edge, {"mode"}, "edge_settings")
    _bounded_text(_string(edge["mode"], "edge mode"), "edge mode")


def _fraction_payload(value: FrozenJsonValue, field: str) -> float:
    payload = _frozen_object(value, field)
    _exact_keys(payload, {"denominator", "numerator"}, field)
    numerator = _int(payload["numerator"], f"{field} numerator")
    denominator = _int(payload["denominator"], f"{field} denominator")
    _bounded_int(numerator, field, minimum=0, maximum=_MAX_INT64)
    _bounded_int(denominator, field, minimum=1, maximum=_MAX_INT64)
    return numerator / denominator


def _freeze_json(value: object, *, field: str, depth: int = 0) -> FrozenJsonValue:
    if depth > 16:
        raise _manifest_error(f"{field} nesting exceeds the bound")
    if value is None or type(value) in {str, int, bool}:
        if isinstance(value, str) and (
            len(value) > MAX_SOURCE_PATH_CHARS or "\x00" in value
        ):
            raise _manifest_error(f"{field} contains an invalid string")
        if type(value) is int and not -_MAX_INT64 <= value <= _MAX_INT64:
            raise _manifest_error(f"{field} integer exceeds the bound")
        return cast(JsonScalar, value)
    if isinstance(value, Mapping):
        if len(value) > 64:
            raise _manifest_error(f"{field} object has too many keys")
        items: list[tuple[str, FrozenJsonValue]] = []
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise _manifest_error(f"{field} contains an invalid key")
            items.append((key, _freeze_json(item, field=field, depth=depth + 1)))
        if len({key for key, _item in items}) != len(items):
            raise _manifest_error(f"{field} contains duplicate keys")
        return FrozenJsonMap(items)
    if isinstance(value, (list, tuple)):
        if len(value) > 1024:
            raise _manifest_error(f"{field} array exceeds the bound")
        return tuple(_freeze_json(item, field=field, depth=depth + 1) for item in value)
    raise _manifest_error(f"{field} contains a non-JSON value")


def _thaw_json(value: FrozenJsonValue) -> JsonValue:
    if isinstance(value, FrozenJsonMap):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json(payload: object) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise _manifest_error("manifest data is not canonical JSON") from error


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _exact_keys(value: Mapping[str, object], expected: set[str], field: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        detail = f"{field} keys do not match the strict schema"
        if missing:
            detail += f"; missing {missing[0]!r}"
        if unknown:
            detail += f"; unknown {unknown[0]!r}"
        raise _manifest_error(detail)


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _manifest_error(f"{field} must be an object")
    return value


def _frozen_object(value: FrozenJsonValue, field: str) -> FrozenJsonMap:
    if not isinstance(value, FrozenJsonMap):
        raise _manifest_error(f"{field} must be an object")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise _manifest_error(f"{field} must be a string")
    return value


def _int(value: object, field: str) -> int:
    if type(value) is not int:
        raise _manifest_error(f"{field} must be an integer")
    return value


def _bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise _manifest_error(f"{field} must be a boolean")
    return value


def _bounded_int(value: int, field: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _manifest_error(f"{field} must be between {minimum} and {maximum}")
    return value


def _bounded_text(value: str, field: str) -> str:
    if not value or len(value) > MAX_TEXT_CHARS or "\x00" in value:
        raise _manifest_error(f"{field} must be a bounded non-empty string")
    return value


def _validate_dimensions(width: int, height: int) -> None:
    if (
        type(width) is not int
        or type(height) is not int
        or not 1 <= width <= MAX_CUT_DIMENSION
        or not 1 <= height <= MAX_CUT_DIMENSION
        or width * height > MAX_CUT_PIXELS
    ):
        raise _set_error("cut dimensions exceed the supported allocation bound")


def _validate_frame_index(index: int) -> None:
    if type(index) is not int or not 0 <= index < MAX_FRAME_COUNT:
        raise _set_error(f"frame index must be between 0 and {MAX_FRAME_COUNT - 1}")


def _frame_filename(index: int) -> str:
    return f"frame-{index:06d}.png"


def _validate_cache_key(cache_key: str) -> None:
    if not isinstance(cache_key, str) or _CACHE_KEY_RE.fullmatch(cache_key) is None:
        raise _unsafe_error("cache key must be canonical lowercase SHA-256")


def _validate_job_id(job_id: str) -> None:
    if (
        not isinstance(job_id, str)
        or len(job_id) > MAX_JOB_ID_CHARS
        or _SAFE_JOB_RE.fullmatch(job_id) is None
    ):
        raise _unsafe_error("job ID is not a safe workspace component")


def _validate_component(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 255
        or name in {".", ".."}
        or "\x00" in name
        or "/" in name
        or "\\" in name
        or PurePath(name).name != name
        or PureWindowsPath(name).name != name
    ):
        raise _unsafe_error("workspace entry name is unsafe")


def _validate_path_value(path: Path) -> None:
    if not isinstance(path, Path):
        raise _unsafe_error("workspace paths must be Path values")
    text = str(path)
    if not text or len(text) > MAX_PATH_CHARS or "\x00" in text:
        raise _unsafe_error("workspace path exceeds the safety bound")


def _validate_sha256(value: str, field: str) -> str:
    if not isinstance(value, str) or _CACHE_KEY_RE.fullmatch(value) is None:
        raise _manifest_error(f"{field} must be lowercase hexadecimal SHA-256")
    return value


def _hash_file(source: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := source.read(COPY_CHUNK_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


def _sha256_fd(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, COPY_CHUNK_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError("short write while copying cut frame")
        offset += written


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _directory_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _entry_exists_no_follow(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or path.is_symlink() or path.is_junction():
        raise _unsafe_error(f"workspace entry {path.name!r} is redirected")
    if not stat.S_ISDIR(info.st_mode):
        raise _unsafe_error(f"workspace entry {path.name!r} is not a directory")
    return True


def _unlink_regular(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OSError("refusing to unlink a non-regular workspace entry")
    path.unlink()


def _fsync_fd(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EBADF}:
            raise


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    with _BoundDirectory.open(path) as bound:
        bound.fsync()


def _same_lexical_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


_promotion_locks_guard = Lock()
_promotion_locks: dict[str, Lock] = {}


def _promotion_lock(key: str) -> Lock:
    with _promotion_locks_guard:
        return _promotion_locks.setdefault(key, Lock())


def _raise_if_cancelled(cancelled: CancellationCheck) -> None:
    if cancelled():
        raise AppError(
            ErrorCode.JOB_CANCELLED,
            "cut-snapshot",
            "error.job.cancelled",
            "rebuild snapshot was cancelled",
            "retry-job",
        )


def _not_cancelled() -> bool:
    return False


def _require_workspace(workspace: CutWorkspace) -> CutWorkspace:
    if type(workspace) is not CutWorkspace:
        raise TypeError("workspace must be an exact CutWorkspace")
    _workspace_layout(workspace.output_directory, create=False)
    if workspace.path.exists():
        _assert_safe_directory(workspace.path)
    return workspace


def _manifest_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.CUT_MANIFEST_INVALID,
        "cut-manifest",
        "error.cuts.manifest-invalid",
        detail,
        "regenerate-or-repair-cuts",
    )


def _set_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.CUT_SET_INVALID,
        "cut-validation",
        "error.cuts.invalid",
        detail,
        "repair-or-regenerate-cuts",
    )


def _stage_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.CUT_STAGE_FAILED,
        "cut-stage",
        "error.cuts.stage-failed",
        detail,
        "retry-render",
    )


def _promotion_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.CUT_PROMOTION_FAILED,
        "cut-promotion",
        "error.cuts.promotion-failed",
        detail,
        "retry-render",
    )


def _snapshot_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.CUT_SNAPSHOT_FAILED,
        "cut-snapshot",
        "error.cuts.snapshot-failed",
        detail,
        "retry-rebuild",
    )


def _cuts_changed(detail: str) -> AppError:
    return AppError(
        ErrorCode.CUTS_CHANGED_DURING_SNAPSHOT,
        "cut-snapshot",
        "error.cuts.changed-during-snapshot",
        detail,
        "retry-rebuild",
    )


def _unsafe_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.CUT_WORKSPACE_UNSAFE,
        "cut-workspace",
        "error.cuts.workspace-unsafe",
        detail,
        "choose-local-output-directory",
    )


def _delete_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.CUT_WORKSPACE_DELETE_FAILED,
        "cut-workspace-delete",
        "error.cuts.delete-failed",
        detail,
        "retry-workspace-delete",
    )
