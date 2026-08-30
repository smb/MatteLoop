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
import hashlib
import importlib
import json
import os
import re
import stat
import sys
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from functools import wraps
from pathlib import Path, PurePath, PureWindowsPath
from threading import Lock, RLock
from types import TracebackType
from typing import (
    Any,
    BinaryIO,
    Final,
    Literal,
    ParamSpec,
    Self,
    TypeVar,
    cast,
    overload,
)

from PIL import Image, UnidentifiedImageError

from rembggui.core.errors import AppError, ErrorCode
from rembggui.core.fingerprints import cut_cache_key_from_inputs
from rembggui.core.rgba import RgbaOwnershipTracker
from rembggui.jobs.models.cache_fs import BoundDirectoryCloseError, UnsafeCacheError

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
MAX_MOUNTINFO_BYTES: Final = 4 * 1024 * 1024
MAX_DEFERRED_BOUND_DIRECTORY_CLOSES = 128

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
type _BoundaryKind = Literal[
    "unsafe", "set", "stage", "promotion", "snapshot", "delete"
]
type _FilesystemFailure = OSError | UnsafeCacheError | BoundDirectoryCloseError

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _filesystem_boundary(
    kind: _BoundaryKind, operation: str
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Keep native/cache filesystem exceptions behind the public AppError API."""

    def decorate(function: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(function)
        def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            try:
                return function(*args, **kwargs)
            except AppError:
                raise
            except (UnsafeCacheError, BoundDirectoryCloseError) as error:
                raise _structured_filesystem_failure(kind, operation, error) from error
            except OSError as error:
                raise _structured_filesystem_failure(kind, operation, error) from error

        return wrapped

    return decorate


def _structured_filesystem_failure(
    kind: _BoundaryKind, operation: str, error: _FilesystemFailure
) -> AppError:
    if isinstance(error, (UnsafeCacheError, BoundDirectoryCloseError)):
        return _unsafe_error(f"{operation}: {error}")
    return _boundary_error(kind, f"{operation}: {error}")


@dataclass(frozen=True, slots=True, init=False, eq=False)
class FrozenJsonMap(Mapping[str, FrozenJsonValue]):
    """Small recursively immutable mapping used by frozen manifests."""

    _items: tuple[tuple[str, FrozenJsonValue], ...]

    def __init__(self, items: Sequence[tuple[str, FrozenJsonValue]]) -> None:
        if len(items) > 64:
            raise _manifest_error("frozen JSON object has too many keys")
        canonical: list[tuple[str, FrozenJsonValue]] = []
        for pair in items:
            if type(pair) is not tuple or len(pair) != 2:
                raise _manifest_error("frozen JSON object entries must be key pairs")
            key, value = pair
            if type(key) is not str or not key or len(key) > 64:
                raise _manifest_error("frozen JSON object contains an invalid key")
            canonical.append((key, _freeze_json(value, field="frozen JSON", depth=1)))
        if len({key for key, _value in canonical}) != len(canonical):
            raise _manifest_error("frozen JSON object contains duplicate keys")
        ordered = tuple(sorted(canonical, key=lambda item: item[0]))
        object.__setattr__(self, "_items", ordered)

    def __getitem__(self, key: str) -> FrozenJsonValue:
        for candidate, value in self._items:
            if candidate == key:
                return value
        raise KeyError(key)

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
        if type(self.filename) is not str:
            raise _manifest_error("frame filename must be an exact string")
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
        if type(self.alpha_threshold) is not str or not self.alpha_threshold:
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
            type(self.schema) is not str
            or type(self.schema_version) is not int
            or self.schema != MANIFEST_SCHEMA
            or self.schema_version != MANIFEST_SCHEMA_VERSION
        ):
            raise _manifest_error("unsupported cut manifest schema")
        if type(self.cache_key_inputs) is not FrozenJsonMap:
            raise _manifest_error("cache_key_inputs must be an exact frozen object")
        _validate_cache_inputs(self.cache_key_inputs)
        expected_key = self.cache_key_for(self.cache_key_inputs)
        _validate_cache_key(self.cache_key)
        if self.cache_key != expected_key:
            raise _manifest_error("cache key does not match its authoritative inputs")
        if (
            type(self.source_path) is not str
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
        if type(frozen) is not FrozenJsonMap:
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
        if type(frozen) is not FrozenJsonMap:
            raise _manifest_error("cache_key_inputs must be an object")
        _validate_cache_inputs(frozen)
        thawed = cast(dict[str, object], _thaw_json(frozen))
        return cut_cache_key_from_inputs(thawed)

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
        assert type(frozen_inputs) is FrozenJsonMap
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
    @_filesystem_boundary("stage", "cannot create staged cut workspace")
    def create_staging(
        cls, output_directory: Path, cache_key: str, job_id: str
    ) -> Self:
        _validate_cache_key(cache_key)
        _validate_job_id(job_id)
        output, root, cuts, scratch = _workspace_layout(output_directory, create=True)
        with _promotion_lock(str(cuts / cache_key)):
            _recover_promotion(cuts, cache_key)
        stage = cuts / f".stage-{cache_key}-{job_id}"
        try:
            with _BoundDirectory.open(cuts) as bound:
                bound.mkdir(stage.name, exist_ok=False)
                with bound.open_child(stage.name):
                    pass
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
    @_filesystem_boundary("unsafe", "cannot open promoted cut workspace")
    def open(cls, output_directory: Path, cache_key: str) -> Self:
        _validate_cache_key(cache_key)
        output, root, cuts, scratch = _workspace_layout(output_directory, create=False)
        with _promotion_lock(str(cuts / cache_key)):
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

    @_filesystem_boundary("set", "cannot read promoted cut")
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
                with _fdopen_owned(descriptor, "rb") as source:
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

    @_filesystem_boundary("set", "cannot update cut-workspace pin")
    def set_pinned(self, pinned: bool, *, now_ns: int | None = None) -> CutManifest:
        if type(pinned) is not bool:
            raise TypeError("pinned must be a bool")
        if self.lifecycle is not WorkspaceLifecycle.PROMOTED:
            raise _set_error("pin updates require a promoted cut workspace")
        with _promotion_lock(str(self.cuts_root / self.cache_key)):
            detect_external_edits(self, now_ns=now_ns)
            manifest, identity = _read_manifest(self.path)
            updated = replace(manifest, pinned=pinned)
            _write_manifest_atomic(self.path, updated, expected_identity=identity)
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


@_filesystem_boundary("stage", "cannot stage cut frame")
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


@_filesystem_boundary("stage", "cannot discard staged cut workspace")
def discard_staged_set(workspace: CutWorkspace) -> bool:
    """Explicitly remove one unpublished staged set and nothing durable."""
    _require_workspace(workspace)
    if workspace.lifecycle is not WorkspaceLifecycle.STAGING:
        raise _stage_error("discard requires a staged cut workspace")
    try:
        with _BoundDirectory.open(workspace.cuts_root) as parent:
            _remove_bound_tree(parent, workspace.path.name)
            parent.fsync()
    except FileNotFoundError:
        return False
    return True


@_filesystem_boundary("promotion", "cannot prepare cuts for render")
def promote_for_render(
    workspace: CutWorkspace,
    manifest: CutManifest,
    scratch_directory: Path,
    *,
    cancelled: CancellationCheck | None = None,
    prefer_reflink: bool = True,
) -> tuple[CutWorkspace, CutWorkspace, CutManifest]:
    """Snapshot a validated stage, then durably publish it for later jobs."""
    _require_workspace(workspace)
    if workspace.lifecycle is not WorkspaceLifecycle.STAGING:
        raise _promotion_error("render promotion requires a staged workspace")
    if type(manifest) is not CutManifest or manifest.cache_key != workspace.cache_key:
        raise _manifest_error("render manifest does not match its staged workspace")
    check_cancelled = cancelled if cancelled is not None else _not_cancelled
    if not callable(check_cancelled):
        raise TypeError("cancelled must be callable")
    private: CutWorkspace | None = None
    try:
        _raise_if_cancelled(check_cancelled)
        _write_manifest_atomic(workspace.path, manifest)
        candidate = validate_cut_set(workspace)
        private = _snapshot_validated_workspace(
            workspace,
            candidate,
            scratch_directory,
            cancelled=check_cancelled,
            prefer_reflink=prefer_reflink,
        )
        _raise_if_cancelled(check_cancelled)
        durable = promote_cut_set(workspace)
        promoted_manifest = validate_cut_set(durable)
        return durable, private, promoted_manifest
    except AppError as error:
        if private is not None:
            _cleanup_snapshot(scratch_directory, error)
        if workspace.path.exists():
            _cleanup_staged_cut(workspace.path, error)
        raise


@_filesystem_boundary("promotion", "cannot promote cut workspace")
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
        _cleanup_staged_cut(workspace.path, error)
        raise
    except (OSError, UnsafeCacheError, BoundDirectoryCloseError) as error:
        failure = _structured_filesystem_failure(
            "promotion", "cannot validate staged cut workspace", error
        )
        _cleanup_staged_cut(workspace.path, failure)
        raise failure from error
    target = workspace.cuts_root / workspace.cache_key
    marker = workspace.cuts_root / f".replace-{workspace.cache_key}.json"
    token = uuid.uuid4().hex
    backup = workspace.cuts_root / f".backup-{workspace.cache_key}-{token}"
    lock = _promotion_lock(str(target))
    with lock:
        _recover_promotion(workspace.cuts_root, workspace.cache_key)
        cuts_bound: _BoundDirectory | None = None
        try:
            with _BoundDirectory.open(workspace.cuts_root) as opened_cuts_bound:
                cuts_bound = opened_cuts_bound
                previous_hash: str | None = None
                try:
                    target_info = cuts_bound.lstat(target.name)
                except FileNotFoundError:
                    target_exists = False
                else:
                    if stat.S_ISLNK(target_info.st_mode) or not stat.S_ISDIR(
                        target_info.st_mode
                    ):
                        raise _unsafe_error(
                            f"workspace entry {target.name!r} is redirected"
                        )
                    target_exists = True
                if target_exists:
                    with cuts_bound.open_child(target.name) as previous_bound:
                        previous, _identity = _read_bound_manifest(previous_bound)
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
                try:
                    _write_journal(marker, journal, bound=cuts_bound)
                except AppError as error:
                    _cleanup_staged_cut(workspace.path, error, parent=cuts_bound)
                    raise
                except (
                    OSError,
                    UnsafeCacheError,
                    BoundDirectoryCloseError,
                ) as error:
                    failure = _structured_filesystem_failure(
                        "promotion", "cannot create cut promotion journal", error
                    )
                    _cleanup_staged_cut(
                        workspace.path,
                        failure,
                        parent=cuts_bound,
                    )
                    raise failure from error
                old_location: Path | None = None
                try:
                    if target_exists:
                        exchanged = (
                            False
                            if cuts_bound.descriptor is None
                            else _atomic_directory_exchange(workspace.path, target)
                        )
                        if exchanged:
                            old_location = workspace.path
                            journal["phase"] = "new-active"
                            journal["used_exchange"] = True
                            _write_journal(marker, journal, bound=cuts_bound)
                        else:
                            cuts_bound.replace_directory(target.name, backup.name)
                            old_location = backup
                            journal["phase"] = "old-moved"
                            _write_journal(marker, journal, bound=cuts_bound)
                            cuts_bound.replace_directory(
                                workspace.path.name, target.name
                            )
                            journal["phase"] = "new-active"
                            _write_journal(marker, journal, bound=cuts_bound)
                    else:
                        cuts_bound.replace_directory(workspace.path.name, target.name)
                        journal["phase"] = "new-active"
                        _write_journal(marker, journal, bound=cuts_bound)
                    promoted = CutWorkspace(
                        workspace.output_directory,
                        workspace.workspace_root,
                        workspace.cuts_root,
                        workspace.scratch_root,
                        workspace.cache_key,
                        target,
                        WorkspaceLifecycle.PROMOTED,
                    )
                    with cuts_bound.open_child(target.name) as promoted_bound:
                        validated = _validate_bound_cut_set(
                            promoted_bound, workspace.cache_key
                        )
                    if validated.to_json_bytes() != candidate.to_json_bytes():
                        raise _promotion_error(
                            "promoted manifest changed during replacement"
                        )
                    cuts_bound.fsync()
                    if old_location is not None:
                        try:
                            _remove_bound_tree(cuts_bound, old_location.name)
                        except FileNotFoundError:
                            pass
                    _unlink_bound_regular(cuts_bound, marker.name)
                    cuts_bound.fsync()
                    return promoted
                except AppError:
                    raise
                except OSError as error:
                    raise _promotion_error(
                        f"atomic cut promotion failed: {error}"
                    ) from error
        finally:
            if cuts_bound is None or not cuts_bound.owns_resources():
                try:
                    _recover_promotion(workspace.cuts_root, workspace.cache_key)
                except AppError:
                    pass
                except (OSError, UnsafeCacheError, BoundDirectoryCloseError):
                    pass


@_filesystem_boundary("set", "cannot validate cut workspace")
def validate_cut_set(workspace: CutWorkspace) -> CutManifest:
    """Validate manifest, namespace, frame bytes, metadata, and exact hashes."""
    _require_workspace(workspace)
    with _promotion_lock(str(workspace.cuts_root / workspace.cache_key)):
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


def _validate_bound_cut_set(bound: _BoundDirectory, cache_key: str) -> CutManifest:
    manifest, manifest_identity = _read_bound_manifest(bound)
    if manifest.cache_key != cache_key:
        raise _set_error("manifest cache key does not match workspace")
    _scan_bound_cut_set(
        bound,
        manifest,
        manifest_identity,
        compare_recorded=True,
    )
    return manifest


@_filesystem_boundary("set", "cannot detect external cut edits")
def detect_external_edits(
    workspace: CutWorkspace, *, now_ns: int | None = None
) -> CutManifest:
    """Rescan valid cuts, persist current hashes, and invalidate derived union data."""
    _require_workspace(workspace)
    if workspace.lifecycle is not WorkspaceLifecycle.PROMOTED:
        raise _set_error("external edit detection requires promoted durable cuts")
    with _promotion_lock(str(workspace.cuts_root / workspace.cache_key)):
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
            last_used_at_ns=max(
                timestamp, manifest.created_at_ns, manifest.last_used_at_ns
            ),
        )
        if updated != manifest:
            _write_manifest_atomic(
                workspace.path, updated, expected_identity=manifest_identity
            )
        return validate_cut_set(workspace)


@_filesystem_boundary("set", "cannot compare-and-set cut union metadata")
def compare_and_set_union_metadata(
    workspace: CutWorkspace,
    expected_frame_hashes: Sequence[str],
    union_metadata: CutUnionMetadata,
    *,
    now_ns: int | None = None,
) -> bool:
    """Publish derived union data only while the expected cut bytes still win."""
    _require_workspace(workspace)
    if workspace.lifecycle is not WorkspaceLifecycle.PROMOTED:
        raise _set_error("union metadata updates require durable promoted cuts")
    if isinstance(expected_frame_hashes, (str, bytes)):
        raise TypeError("expected_frame_hashes must be a sequence")
    expected = tuple(expected_frame_hashes)
    if any(type(value) is not str for value in expected):
        raise TypeError("expected frame hashes must be strings")
    if type(union_metadata) is not CutUnionMetadata:
        raise TypeError("union_metadata must be a CutUnionMetadata")
    timestamp = time.time_ns() if now_ns is None else now_ns
    _bounded_int(timestamp, "last-use timestamp", minimum=0, maximum=_MAX_INT64)
    with _promotion_lock(str(workspace.cuts_root / workspace.cache_key)):
        manifest, manifest_identity = _read_manifest(workspace.path)
        frames, _identities = _scan_cut_set(
            workspace.path,
            manifest,
            manifest_identity,
            compare_recorded=False,
        )
        if tuple(frame.sha256 for frame in frames) != expected:
            return False
        updated = replace(
            manifest,
            frames=frames,
            edited=manifest.edited or frames != manifest.frames,
            union_metadata=union_metadata,
            last_used_at_ns=max(
                timestamp, manifest.created_at_ns, manifest.last_used_at_ns
            ),
        )
        try:
            _write_manifest_atomic(
                workspace.path, updated, expected_identity=manifest_identity
            )
        except AppError as error:
            if (
                error.code is ErrorCode.CUT_MANIFEST_INVALID
                and "changed before the atomic update" in error.technical_detail
            ):
                return False
            raise
        return True


def _snapshot_validated_workspace(
    workspace: CutWorkspace,
    baseline: CutManifest,
    scratch_directory: Path,
    *,
    cancelled: CancellationCheck,
    prefer_reflink: bool,
) -> CutWorkspace:
    if not isinstance(scratch_directory, Path):
        raise _unsafe_error("scratch directory must be a Path")
    _validate_path_value(scratch_directory)
    if scratch_directory.parent != workspace.scratch_root:
        raise _unsafe_error("snapshot must use scratch/<job-id> under its workspace")
    _validate_job_id(scratch_directory.name)
    snapshot_path = scratch_directory / "cuts-snapshot"
    started = False
    try:
        _raise_if_cancelled(cancelled)
        source_manifest, manifest_identity = _read_manifest(workspace.path)
        if source_manifest != baseline:
            raise _cuts_changed("staged manifest changed before private snapshot")
        _frames, baseline_identities = _scan_cut_set(
            workspace.path,
            baseline,
            manifest_identity,
            compare_recorded=True,
        )
        with _BoundDirectory.open(workspace.scratch_root) as scratch_bound:
            scratch_bound.mkdir(scratch_directory.name, exist_ok=False)
            started = True
            with scratch_bound.open_child(scratch_directory.name) as job_bound:
                job_bound.mkdir(snapshot_path.name, exist_ok=False)
                with job_bound.open_child(snapshot_path.name):
                    pass
        for frame in baseline.frames:
            _raise_if_cancelled(cancelled)
            _copy_frame_descriptor_bound(
                workspace.path,
                snapshot_path,
                frame,
                prefer_reflink=prefer_reflink,
            )
        _write_manifest_atomic(snapshot_path, baseline)
        _raise_if_cancelled(cancelled)
        after, after_identity = _read_manifest(workspace.path)
        after_frames, after_identities = _scan_cut_set(
            workspace.path,
            after,
            after_identity,
            compare_recorded=True,
        )
        if (
            after != baseline
            or after_frames != baseline.frames
            or after_identities != baseline_identities
        ):
            raise _cuts_changed("cuts changed during private render snapshot")
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
        raise
    except OSError as error:
        failure = _snapshot_error(f"cannot create private render snapshot: {error}")
        if started:
            _cleanup_snapshot(scratch_directory, failure)
        raise failure from error


@_filesystem_boundary("snapshot", "cannot snapshot cut workspace")
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
        with _BoundDirectory.open(workspace.scratch_root) as scratch_bound:
            scratch_bound.mkdir(scratch_directory.name, exist_ok=False)
            started = True
            with scratch_bound.open_child(scratch_directory.name) as job_bound:
                job_bound.mkdir(snapshot_path.name, exist_ok=False)
                with job_bound.open_child(snapshot_path.name):
                    pass
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


@_filesystem_boundary("unsafe", "cannot list cut workspaces")
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
        for scanned, (name, info) in enumerate(bound.iter_entries(), start=1):
            if scanned > MAX_WORKSPACE_ENTRIES * 3:
                raise _unsafe_error("workspace namespace exceeds the listing bound")
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
            size_bytes += len(manifest.to_json_bytes())
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


@_filesystem_boundary("delete", "cannot delete cut workspace")
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
        try:
            manifest, _identity = _read_manifest(workspace.path)
        except AppError as error:
            if not allow_pinned:
                raise AppError(
                    ErrorCode.CUT_WORKSPACE_PINNED,
                    "cut-workspace-delete",
                    "error.cuts.pin-unknown",
                    "corrupt manifest prevents a reliable pinned-state check",
                    "confirm-delete-pinned-workspace",
                ) from error
            manifest = None
        if manifest is not None and manifest.pinned and not allow_pinned:
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


@_filesystem_boundary("delete", "cannot clean scratch workspace")
def cleanup_scratch(output_directory: Path, job_id: str) -> bool:
    """Immediately remove one exact scratch job after success or cancellation."""
    _validate_job_id(job_id)
    _output, _root, _cuts, scratch = _workspace_layout(output_directory, create=False)
    target = scratch / job_id
    try:
        with _BoundDirectory.open(scratch) as bound:
            try:
                info = bound.lstat(job_id)
            except FileNotFoundError:
                return False
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _unsafe_error(f"scratch entry {job_id!r} is redirected")
        _remove_tree(target)
        _fsync_directory(scratch)
        return True
    except FileNotFoundError:
        return False
    except AppError:
        raise
    except OSError as error:
        raise _delete_error(f"cannot clean scratch job {job_id!r}: {error}") from error


@_filesystem_boundary("snapshot", "cannot clean abandoned scratch workspaces")
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
    try:
        with _BoundDirectory.open(path) as bound:
            return _scan_bound_cut_set(
                bound,
                manifest,
                manifest_identity,
                compare_recorded=compare_recorded,
            )
    except AppError:
        raise
    except OSError as error:
        raise _set_error(f"cannot inspect cut directory: {error}") from error


def _scan_bound_cut_set(
    bound: _BoundDirectory,
    manifest: CutManifest,
    manifest_identity: tuple[int, int, int, int, int],
    *,
    compare_recorded: bool,
) -> tuple[tuple[CutFrame, ...], tuple[tuple[int, int, int, int, int], ...]]:
    expected_names = {MANIFEST_FILENAME, *(frame.filename for frame in manifest.frames)}
    try:
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
        frame, identity = _inspect_bound_frame(
            bound,
            expected,
            compare_recorded=compare_recorded,
            load_pixels=True,
        )
        if (frame.width, frame.height) != (manifest.width, manifest.height):
            raise _set_error(f"frame {frame.filename} dimensions do not match manifest")
        frames.append(frame)
        identities.append(identity)
    try:
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
            return _inspect_bound_frame(
                bound,
                expected,
                compare_recorded=compare_recorded,
                load_pixels=load_pixels,
            )
    except AppError:
        raise
    except (OSError, UnidentifiedImageError, SyntaxError, ValueError) as error:
        raise _set_error(
            f"frame {expected.filename} is not a readable PNG: {error}"
        ) from error


def _inspect_bound_frame(
    bound: _BoundDirectory,
    expected: CutFrame,
    *,
    compare_recorded: bool,
    load_pixels: bool,
) -> tuple[CutFrame, tuple[int, int, int, int, int]]:
    try:
        descriptor = bound.open_read(expected.filename)
        with _fdopen_owned(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise _unsafe_error(f"frame {expected.filename} is not regular")
            if not 1 <= before.st_size <= MAX_FRAME_FILE_BYTES:
                raise _set_error(f"frame {expected.filename} has an invalid byte size")
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
        fcntl_module = importlib.import_module("fcntl")
        destination_fd = destination.open_new_fd(filename)
        try:
            try:
                fcntl_module.ioctl(destination_fd, 0x40049409, source_fd)
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
    __slots__ = (
        "_windows_api",
        "_windows_cleanup_handles",
        "_windows_handles",
        "descriptor",
        "path",
    )

    def __init__(
        self,
        path: Path,
        descriptor: int | None,
        *,
        windows_handles: tuple[int, ...] = (),
        windows_api: Any = None,
    ) -> None:
        self.path = path
        self.descriptor = descriptor
        self._windows_handles = windows_handles
        self._windows_cleanup_handles: tuple[int, ...] = ()
        self._windows_api = windows_api

    @classmethod
    def open(cls, path: Path) -> Self:
        _validate_path_value(path)
        if ".." in path.parts:
            raise _unsafe_error("workspace directory traversal is not allowed")
        absolute = Path(os.path.abspath(path))
        if not absolute.is_absolute() or not absolute.parts:
            raise _unsafe_error("workspace directory must be absolute")
        if os.name == "nt":
            return cls._open_windows(absolute)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(absolute.anchor, flags)
            for component in absolute.parts[1:]:
                _validate_component(component)
                child = os.open(component, flags, dir_fd=descriptor)
                try:
                    opened = os.fstat(child)
                    if not stat.S_ISDIR(opened.st_mode):
                        raise _unsafe_error(
                            f"workspace component {component!r} is not a directory"
                        )
                except BaseException:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            return cls(absolute, descriptor)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                failure = _unsafe_error("workspace namespace contains redirection")
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                raise failure from error
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise
        except BaseException:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise

    @classmethod
    def _open_windows(cls, path: Path) -> Self:
        from rembggui.jobs.models.cache_fs import (
            _FILE_ATTRIBUTE_DIRECTORY,
            _FILE_ATTRIBUTE_REPARSE_POINT,
            _WINDOWS_DIRECTORY_ACCESS,
            _WINDOWS_DIRECTORY_FLAGS,
            _WINDOWS_DIRECTORY_SHARE,
            _WINDOWS_WRITABLE_DIRECTORY_ACCESS,
            _CtypesWindowsDirectoryApi,
        )

        api = _CtypesWindowsDirectoryApi()
        handles: list[int] = []
        try:
            anchor = Path(path.anchor)
            handle = api.open_anchor(
                anchor,
                desired_access=_WINDOWS_DIRECTORY_ACCESS,
                share_mode=_WINDOWS_DIRECTORY_SHARE,
                flags=_WINDOWS_DIRECTORY_FLAGS,
            )
            handles.append(handle)
            components = path.parts[1:]
            for index, component in enumerate(components):
                _validate_component(component)
                handle = api.open_child_directory(
                    handles[-1],
                    component,
                    create=False,
                    desired_access=(
                        _WINDOWS_WRITABLE_DIRECTORY_ACCESS
                        if index == len(components) - 1
                        else _WINDOWS_DIRECTORY_ACCESS
                    ),
                    share_mode=_WINDOWS_DIRECTORY_SHARE,
                    flags=_WINDOWS_DIRECTORY_FLAGS,
                )
                handles.append(handle)
                attributes = api.file_attributes(handle)
                if not attributes & _FILE_ATTRIBUTE_DIRECTORY or attributes & (
                    _FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise _unsafe_error(
                        f"workspace component {component!r} is redirected"
                    )
            return cls(
                path,
                None,
                windows_handles=tuple(handles),
                windows_api=api,
            )
        except BaseException:
            for handle in reversed(handles):
                try:
                    api.close_handle(handle)
                except OSError:
                    pass
            raise

    @classmethod
    def _open_windows_publication(cls, path: Path) -> Self:
        """Bind only the final output parent with publication sharing/access."""
        from rembggui.jobs.models.cache_fs import (
            _FILE_ATTRIBUTE_DIRECTORY,
            _FILE_ATTRIBUTE_REPARSE_POINT,
            _WINDOWS_DIRECTORY_ACCESS,
            _WINDOWS_DIRECTORY_FLAGS,
            _WINDOWS_DIRECTORY_SHARE,
            _WINDOWS_PUBLICATION_DIRECTORY_ACCESS,
            _WINDOWS_PUBLICATION_SHARE,
            _CtypesWindowsDirectoryApi,
        )

        api = _CtypesWindowsDirectoryApi()
        handles: list[int] = []
        try:
            anchor = Path(path.anchor)
            components = path.parts[1:]
            if components:
                handle = api.open_anchor(
                    anchor,
                    desired_access=_WINDOWS_DIRECTORY_ACCESS,
                    share_mode=_WINDOWS_DIRECTORY_SHARE,
                    flags=_WINDOWS_DIRECTORY_FLAGS,
                )
            else:
                handle = api.open_publication_anchor(
                    anchor,
                    desired_access=_WINDOWS_PUBLICATION_DIRECTORY_ACCESS,
                    share_mode=_WINDOWS_PUBLICATION_SHARE,
                    flags=_WINDOWS_DIRECTORY_FLAGS,
                )
            handles.append(handle)
            for index, component in enumerate(components):
                _validate_component(component)
                final = index == len(components) - 1
                if final:
                    handle = api.open_publication_child_directory(
                        handles[-1],
                        component,
                        create=False,
                        desired_access=_WINDOWS_PUBLICATION_DIRECTORY_ACCESS,
                        share_mode=_WINDOWS_PUBLICATION_SHARE,
                        flags=_WINDOWS_DIRECTORY_FLAGS,
                    )
                else:
                    handle = api.open_child_directory(
                        handles[-1],
                        component,
                        create=False,
                        desired_access=_WINDOWS_DIRECTORY_ACCESS,
                        share_mode=_WINDOWS_DIRECTORY_SHARE,
                        flags=_WINDOWS_DIRECTORY_FLAGS,
                    )
                handles.append(handle)
                attributes = api.file_attributes(handle)
                if not attributes & _FILE_ATTRIBUTE_DIRECTORY or attributes & (
                    _FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise _unsafe_error(f"output component {component!r} is redirected")
            return cls(path, None, windows_handles=tuple(handles), windows_api=api)
        except BaseException as error:
            failed_handles: list[int] = []
            for handle in reversed(handles):
                try:
                    api.close_handle(handle)
                except OSError as close_error:
                    failed_handles.append(handle)
                    error.add_note(
                        f"additional publication-binding cleanup failure: {close_error}"
                    )
            if failed_handles:
                owner = cls(
                    path,
                    None,
                    windows_handles=tuple(reversed(failed_handles)),
                    windows_api=api,
                )
                _retain_deferred_bound_directory_close(owner, error)
            raise

    def __enter__(self) -> Self:
        return self

    def owns_resources(self) -> bool:
        return bool(
            self.descriptor is not None
            or self._windows_handles
            or self._windows_cleanup_handles
        )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.close()
        except BaseException as error:
            if self._windows_handles or self._windows_cleanup_handles:
                _retain_deferred_bound_directory_close(
                    self, exc_value if exc_value is not None else error
                )
            if exc_value is not None:
                exc_value.add_note(f"additional bound-directory close failure: {error}")
                return
            raise

    def close(self) -> None:
        failures: list[OSError] = []
        descriptor = self.descriptor
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as error:
                failures.append(error)
            else:
                self.descriptor = None
        cleanup_handles = self._windows_cleanup_handles
        failed_cleanup_handles: set[int] = set()
        for handle in reversed(cleanup_handles):
            try:
                self._windows_api.close_handle(handle)
            except OSError as error:
                failures.append(error)
                failed_cleanup_handles.add(handle)
        self._windows_cleanup_handles = tuple(
            handle for handle in cleanup_handles if handle in failed_cleanup_handles
        )
        handles = self._windows_handles
        failed_handles: set[int] = set()
        for handle in reversed(handles):
            try:
                self._windows_api.close_handle(handle)
            except OSError as error:
                failures.append(error)
                failed_handles.add(handle)
        self._windows_handles = tuple(
            handle for handle in handles if handle in failed_handles
        )
        if failures:
            detail = "; ".join(str(error) for error in failures)
            raise _unsafe_error(f"cannot close bound workspace resources: {detail}")
        _forget_deferred_bound_directory_close(self)

    def assert_still_named(self) -> None:
        self.assert_handle_safe()
        if self._windows_handles:
            return
        if self.descriptor is not None:
            opened = os.fstat(self.descriptor)
            try:
                named = self.path.lstat()
            except OSError as error:
                raise _unsafe_error("bound workspace directory was renamed") from error
            if _directory_identity(opened) != _directory_identity(named):
                raise _unsafe_error("bound workspace directory was redirected")

    def assert_handle_safe(self) -> None:
        """Validate held handles without relying on their lexical namespace."""
        if self._windows_handles:
            for handle in self._windows_handles:
                self._windows_api.assert_directory_handle(handle)
            return
        if self.descriptor is not None and not stat.S_ISDIR(
            os.fstat(self.descriptor).st_mode
        ):
            raise _unsafe_error("bound workspace handle is not a directory")

    def lstat(self, name: str) -> os.stat_result:
        _validate_component(name)
        if self.descriptor is not None:
            return os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        if self._windows_handles:
            return cast(
                os.stat_result,
                self._windows_api.lstat_at(self._windows_handles[-1], name),
            )
        return (self.path / name).lstat()

    def publication_lstat(self, name: str) -> os.stat_result:
        _validate_component(name)
        if self.descriptor is not None:
            return os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        if self._windows_handles:
            return cast(
                os.stat_result,
                self._windows_api.publication_lstat_at(self._windows_handles[-1], name),
            )
        return (self.path / name).lstat()

    def mkdir(self, name: str, *, exist_ok: bool) -> None:
        _validate_component(name)
        try:
            if self.descriptor is not None:
                os.mkdir(name, mode=0o700, dir_fd=self.descriptor)
            elif self._windows_handles:
                self._windows_api.mkdir_at(
                    self._windows_handles[-1], name, exist_ok=exist_ok
                )
            else:
                os.mkdir(self.path / name, mode=0o700)
        except FileExistsError:
            if not exist_ok:
                raise
            info = self.lstat(name)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _unsafe_error(f"workspace entry {name!r} is redirected")

    def open_child(self, name: str) -> _BoundDirectory:
        _validate_component(name)
        if self.descriptor is not None:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(name, flags, dir_fd=self.descriptor)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISDIR(info.st_mode):
                    raise _unsafe_error(f"workspace entry {name!r} is not a directory")
                return _BoundDirectory(self.path / name, descriptor)
            except BaseException:
                os.close(descriptor)
                raise
        if self._windows_handles:
            from rembggui.jobs.models.cache_fs import (
                _FILE_ATTRIBUTE_DIRECTORY,
                _FILE_ATTRIBUTE_REPARSE_POINT,
                _WINDOWS_DIRECTORY_FLAGS,
                _WINDOWS_DIRECTORY_SHARE,
                _WINDOWS_WRITABLE_DIRECTORY_ACCESS,
            )

            handle = self._windows_api.open_child_directory(
                self._windows_handles[-1],
                name,
                create=False,
                desired_access=_WINDOWS_WRITABLE_DIRECTORY_ACCESS,
                share_mode=_WINDOWS_DIRECTORY_SHARE,
                flags=_WINDOWS_DIRECTORY_FLAGS,
            )
            try:
                attributes = self._windows_api.file_attributes(handle)
                if not attributes & _FILE_ATTRIBUTE_DIRECTORY or attributes & (
                    _FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise _unsafe_error(f"workspace entry {name!r} is redirected")
                return _BoundDirectory(
                    self.path / name,
                    None,
                    windows_handles=(handle,),
                    windows_api=self._windows_api,
                )
            except BaseException as error:
                try:
                    self._windows_api.close_handle(handle)
                except BaseException as cleanup_error:
                    self._windows_cleanup_handles += (handle,)
                    error.add_note(
                        f"additional child-handle cleanup failure: {cleanup_error}"
                    )
                raise
        return _BoundDirectory.open(self.path / name)

    def open_publication_child(self, name: str) -> _BoundDirectory:
        _validate_component(name)
        if self.descriptor is not None:
            return self.open_child(name)
        if self._windows_handles:
            from rembggui.jobs.models.cache_fs import (
                _FILE_ATTRIBUTE_DIRECTORY,
                _FILE_ATTRIBUTE_REPARSE_POINT,
                _WINDOWS_DIRECTORY_FLAGS,
                _WINDOWS_PUBLICATION_DIRECTORY_ACCESS,
                _WINDOWS_PUBLICATION_SHARE,
            )

            handle = self._windows_api.open_publication_child_directory(
                self._windows_handles[-1],
                name,
                create=False,
                desired_access=_WINDOWS_PUBLICATION_DIRECTORY_ACCESS,
                share_mode=_WINDOWS_PUBLICATION_SHARE,
                flags=_WINDOWS_DIRECTORY_FLAGS,
            )
            try:
                attributes = self._windows_api.file_attributes(handle)
                if not attributes & _FILE_ATTRIBUTE_DIRECTORY or attributes & (
                    _FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise _unsafe_error(f"output entry {name!r} is redirected")
                return _BoundDirectory(
                    self.path / name,
                    None,
                    windows_handles=(handle,),
                    windows_api=self._windows_api,
                )
            except BaseException as error:
                try:
                    self._windows_api.close_handle(handle)
                except BaseException as cleanup_error:
                    self._windows_cleanup_handles += (handle,)
                    error.add_note(
                        f"additional child-handle cleanup failure: {cleanup_error}"
                    )
                raise
        return _BoundDirectory.open(self.path / name)

    def open_read(self, name: str) -> int:
        _validate_component(name)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            if self.descriptor is not None:
                return os.open(name, flags, dir_fd=self.descriptor)
            if self._windows_handles:
                return cast(
                    int,
                    self._windows_api.open_read_at(self._windows_handles[-1], name),
                )
            return os.open(self.path / name, flags)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise _unsafe_error(
                    f"workspace entry {name!r} is redirected"
                ) from error
            raise

    def open_publication_read(self, name: str) -> int:
        _validate_component(name)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            if self.descriptor is not None:
                return os.open(name, flags, dir_fd=self.descriptor)
            if self._windows_handles:
                return cast(
                    int,
                    self._windows_api.open_publication_read_at(
                        self._windows_handles[-1], name
                    ),
                )
            return os.open(self.path / name, flags)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise _unsafe_error(
                    f"workspace entry {name!r} is redirected"
                ) from error
            raise

    def open_publication_read_write(self, name: str) -> int:
        _validate_component(name)
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            if self.descriptor is not None:
                return os.open(name, flags, dir_fd=self.descriptor)
            if self._windows_handles:
                return cast(
                    int,
                    self._windows_api.open_publication_read_write_at(
                        self._windows_handles[-1], name
                    ),
                )
            return os.open(self.path / name, flags)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise _unsafe_error(f"output entry {name!r} is redirected") from error
            raise

    def open_read_write(self, name: str) -> int:
        _validate_component(name)
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if self.descriptor is not None:
            return os.open(name, flags, dir_fd=self.descriptor)
        if self._windows_handles:
            return cast(
                int,
                self._windows_api.open_read_at(self._windows_handles[-1], name),
            )
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
        if self._windows_handles:
            return cast(
                int,
                self._windows_api.open_new_read_write_at(
                    self._windows_handles[-1], name
                ),
            )
        return os.open(self.path / name, flags, 0o600)

    def open_new_publication_fd(self, name: str) -> int:
        _validate_component(name)
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if self.descriptor is not None:
            return os.open(name, flags, 0o600, dir_fd=self.descriptor)
        if self._windows_handles:
            return cast(
                int,
                self._windows_api.open_new_publication_read_write_at(
                    self._windows_handles[-1], name
                ),
            )
        return os.open(self.path / name, flags, 0o600)

    def open_new(self, name: str) -> BinaryIO:
        descriptor = self.open_new_fd(name)
        return _fdopen_owned(descriptor, "wb")

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
        elif self._windows_handles:
            self._windows_api.replace_at(self._windows_handles[-1], source, destination)
        else:
            os.replace(self.path / source, self.path / destination)

    def replace_publication(self, source: str, destination: str) -> None:
        _validate_component(source)
        _validate_component(destination)
        if self.descriptor is not None:
            os.replace(
                source,
                destination,
                src_dir_fd=self.descriptor,
                dst_dir_fd=self.descriptor,
            )
        elif self._windows_handles:
            self._windows_api.replace_publication_at(
                self._windows_handles[-1], source, destination
            )
        else:
            os.replace(self.path / source, self.path / destination)

    def replace_directory(self, source: str, destination: str) -> None:
        _validate_component(source)
        _validate_component(destination)
        if self.descriptor is not None:
            os.replace(
                source,
                destination,
                src_dir_fd=self.descriptor,
                dst_dir_fd=self.descriptor,
            )
        elif self._windows_handles:
            self._windows_api.replace_directory_at(
                self._windows_handles[-1], source, destination
            )
        else:
            os.replace(self.path / source, self.path / destination)

    def unlink(self, name: str) -> None:
        _validate_component(name)
        if self.descriptor is not None:
            os.unlink(name, dir_fd=self.descriptor)
        elif self._windows_handles:
            self._windows_api.unlink_at(
                self._windows_handles[-1], name, require_regular=False
            )
        else:
            (self.path / name).unlink()

    def rmdir(self, name: str) -> None:
        _validate_component(name)
        if self.descriptor is not None:
            os.rmdir(name, dir_fd=self.descriptor)
        elif self._windows_handles:
            self._windows_api.rmdir_at(self._windows_handles[-1], name)
        else:
            os.rmdir(self.path / name)

    def iter_entries(
        self, *, max_entries: int | None = None
    ) -> Iterator[tuple[str, os.stat_result]]:
        if max_entries is None:
            max_entries = MAX_FRAME_COUNT + 16
        _bounded_int(
            max_entries,
            "directory enumeration count",
            minimum=1,
            maximum=MAX_FRAME_COUNT + 16,
        )
        if self._windows_handles:
            yield from self._windows_api.iter_entries_at(
                self._windows_handles[-1], max_entries=max_entries
            )
            return
        target: int | Path = (
            self.descriptor if self.descriptor is not None else self.path
        )
        with os.scandir(target) as entries:
            for count, entry in enumerate(entries, start=1):
                if count > max_entries:
                    raise _unsafe_error("directory entry count exceeds its bound")
                yield entry.name, entry.stat(follow_symlinks=False)

    def fsync(self) -> None:
        if self.descriptor is not None:
            _fsync_fd(self.descriptor)
        elif self._windows_handles:
            self._windows_api.flush_directory_strict(self._windows_handles[-1])


class PublicationDirectory:
    """One handle-bound output parent for an entire publication transaction."""

    __slots__ = ("_directory", "_stack", "path")

    def __init__(self, directory: _BoundDirectory, stack: ExitStack) -> None:
        self._directory = directory
        self._stack = stack
        self.path = directory.path

    @classmethod
    def open(cls, path: Path) -> PublicationDirectory:
        stack = ExitStack()
        try:
            absolute = Path(os.path.abspath(path))
            directory = stack.enter_context(
                _BoundDirectory._open_windows_publication(absolute)
                if os.name == "nt"
                else _BoundDirectory.open(absolute)
            )
            return cls(directory, stack)
        except BaseException as error:
            try:
                stack.__exit__(type(error), error, error.__traceback__)
            except BaseException as cleanup_error:
                error.add_note(
                    f"additional publication-parent cleanup failure: {cleanup_error}"
                )
            raise

    def close(self, primary: BaseException | None = None) -> None:
        if primary is None:
            self._stack.close()
            return
        self._stack.__exit__(type(primary), primary, primary.__traceback__)

    def assert_still_bound(self) -> None:
        self._directory.assert_still_named()

    def assert_handle_bound(self) -> None:
        self._directory.assert_handle_safe()

    def name_for(self, path: Path) -> str:
        _validate_path_value(path)
        parent = Path(os.path.abspath(path.parent))
        if parent != self.path:
            raise _unsafe_error("output entry is outside the bound publication parent")
        _validate_component(path.name)
        return path.name

    def target_key(self, path: Path) -> str:
        name = self.name_for(path)
        platform = (
            "windows"
            if os.name == "nt"
            else "darwin"
            if sys.platform == "darwin"
            else "posix"
        )
        return _output_target_component_key(name, platform=platform)

    def path_for(self, name: str) -> Path:
        _validate_component(name)
        return self.path / name

    def lstat(self, name: str) -> os.stat_result:
        return self._directory.publication_lstat(name)

    def open_read(self, name: str) -> int:
        return self._directory.open_publication_read(name)

    def open_read_write(self, name: str) -> int:
        return self._directory.open_publication_read_write(name)

    def open_new(self, name: str) -> int:
        return self._directory.open_new_publication_fd(name)

    def replace(self, source: str, destination: str) -> None:
        self._directory.replace_publication(source, destination)

    def replace_from(
        self,
        source_directory: RecoveryDirectory,
        source: str,
        destination: str,
    ) -> None:
        _rename_bound_publication(
            source_directory._directory,
            source,
            self._directory,
            destination,
            replace=True,
        )

    def rename_no_replace_from(
        self,
        source_directory: RecoveryDirectory,
        source: str,
        destination: str,
    ) -> None:
        _rename_bound_publication(
            source_directory._directory,
            source,
            self._directory,
            destination,
            replace=False,
        )

    def fsync(self) -> None:
        self._directory.fsync()

    def open_private_directory(self, name: str, purpose: str) -> RecoveryDirectory:
        return RecoveryDirectory.open_from(self, name, purpose)

    def acquire_output_lock(
        self,
        directory: RecoveryDirectory,
        target_key: str,
    ) -> AdvisoryFileLock:
        """Bind a target lock and its private directory to this parent handle."""
        if not isinstance(directory, RecoveryDirectory):
            raise TypeError("output lock requires a private publication directory")
        if (
            not isinstance(target_key, str)
            or len(target_key) != 64
            or any(character not in "0123456789abcdef" for character in target_key)
        ):
            raise ValueError("output target key must be lowercase SHA-256")
        return directory._acquire_advisory_lock(
            f"{target_key}.transaction.lock",
            publication=self,
            anchor_name=f".{target_key}.transaction-anchor",
        )


class _SystemAdvisoryFileLock:
    """Non-blocking process lock adapter over one held regular-file fd."""

    __slots__ = ("_platform", "_posix", "_windows")

    def __init__(
        self,
        *,
        platform: str | None = None,
        posix_module: Any | None = None,
        windows_module: Any | None = None,
    ) -> None:
        selected = (
            ("windows" if os.name == "nt" else "posix")
            if platform is None
            else platform
        )
        if selected not in {"posix", "windows"}:
            raise ValueError("advisory-lock platform must be posix or windows")
        if selected == "posix" and windows_module is not None:
            raise ValueError("POSIX advisory locks do not accept a Windows module")
        if selected == "windows" and posix_module is not None:
            raise ValueError("Windows advisory locks do not accept a POSIX module")
        self._platform = selected
        self._posix = (
            importlib.import_module("fcntl")
            if selected == "posix" and posix_module is None
            else posix_module
        )
        self._windows = (
            importlib.import_module("msvcrt")
            if selected == "windows" and windows_module is None
            else windows_module
        )

    def acquire_nonblocking(self, descriptor: int) -> bool:
        if type(descriptor) is not int or descriptor < 0:
            raise ValueError("advisory-lock descriptor must be a non-negative int")
        if self._platform == "posix":
            posix = self._posix
            if posix is None:
                raise RuntimeError("POSIX advisory-lock adapter is unavailable")
            try:
                posix.flock(descriptor, posix.LOCK_EX | posix.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    return False
                raise
            return True
        windows = self._windows
        if windows is None:
            raise RuntimeError("Windows advisory-lock adapter is unavailable")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            windows.locking(descriptor, windows.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(
                error, "winerror", None
            ) in {33, 36}:
                return False
            raise
        return True


class _OpenedDescriptorOwner:
    """Own an fd immediately and consume its integer on every close attempt."""

    __slots__ = ("_close_guard", "_descriptor")

    def __init__(self, descriptor: int) -> None:
        if type(descriptor) is not int or descriptor < 0:
            raise ValueError("owned descriptor must be a non-negative int")
        self._descriptor: int | None = descriptor
        self._close_guard = Lock()

    @property
    def descriptor(self) -> int:
        descriptor = self._descriptor
        if descriptor is None:
            raise _unsafe_error("owned descriptor was already consumed")
        return descriptor

    def transfer(self) -> int:
        with self._close_guard:
            descriptor = self.descriptor
            self._descriptor = None
            return descriptor

    def close(
        self,
        primary: BaseException | None = None,
        *,
        detail: str,
    ) -> None:
        with self._close_guard:
            descriptor = self._descriptor
            if descriptor is None:
                return
            # A failed close leaves POSIX fd state unspecified.  Consume the
            # integer before calling close so it can never be retried after
            # the kernel may have made that integer reusable.
            self._descriptor = None
            try:
                os.close(descriptor)
            except BaseException as error:
                if primary is not None:
                    primary.add_note(f"additional {detail} cleanup failure: {error}")
                    return
                structured = _unsafe_error(f"cannot close {detail}: {error}")
                raise structured from error


class AdvisoryFileLock:
    """Owned advisory lock, optionally bound to an output-parent anchor."""

    __slots__ = (
        "_adapter",
        "_anchor_descriptor",
        "_anchor_identity",
        "_anchor_name",
        "_close_guard",
        "_descriptor",
        "_directory",
        "_directory_identity",
        "_local_key",
        "_local_lock",
        "_locked",
        "_publication",
        "_lock_identity",
        "name",
    )

    def __init__(
        self,
        name: str,
        descriptor: int,
        adapter: _SystemAdvisoryFileLock,
        local_key: str,
        local_lock: Lock,
        *,
        locked: bool = True,
        directory: RecoveryDirectory | None = None,
        lock_identity: tuple[int, int] | None = None,
        publication: PublicationDirectory | None = None,
        anchor_name: str | None = None,
        anchor_descriptor: int | None = None,
        anchor_identity: tuple[int, int] | None = None,
        directory_identity: tuple[int, int] | None = None,
    ) -> None:
        self.name = name
        self._descriptor: int | None = descriptor
        self._adapter = adapter
        self._close_guard = Lock()
        self._locked = locked
        self._local_key = local_key
        self._local_lock: Lock | None = local_lock
        self._directory = directory
        self._lock_identity = lock_identity
        self._publication = publication
        self._anchor_name = anchor_name
        self._anchor_descriptor = anchor_descriptor
        self._anchor_identity = anchor_identity
        self._directory_identity = directory_identity

    @property
    def anchored(self) -> bool:
        return self._publication is not None

    def assert_owned(self) -> None:
        """Fail closed if the parent, private directory, anchor, or lock changed."""
        descriptor = self._descriptor
        directory = self._directory
        lock_identity = self._lock_identity
        if descriptor is None or directory is None or lock_identity is None:
            raise _unsafe_error("output transaction lock is no longer owned")
        directory.assert_handle_owned()
        current_lock = directory.lstat(self.name)
        if (
            stat.S_ISLNK(current_lock.st_mode)
            or not stat.S_ISREG(current_lock.st_mode)
            or _directory_identity(current_lock) != lock_identity
            or _directory_identity(os.fstat(descriptor)) != lock_identity
        ):
            raise _unsafe_error("output transaction lock ownership changed")

        publication = self._publication
        if publication is None:
            return
        anchor_name = self._anchor_name
        anchor_descriptor = self._anchor_descriptor
        anchor_identity = self._anchor_identity
        directory_identity = self._directory_identity
        if (
            anchor_name is None
            or anchor_descriptor is None
            or anchor_identity is None
            or directory_identity is None
        ):
            raise _unsafe_error("output transaction anchor is incomplete")
        publication.assert_handle_bound()
        if directory.identity != directory_identity:
            raise _unsafe_error("output private directory ownership changed")
        before = publication.lstat(anchor_name)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or _directory_identity(before) != anchor_identity
            or _directory_identity(os.fstat(anchor_descriptor)) != anchor_identity
        ):
            raise _unsafe_error("output transaction anchor ownership changed")
        payload = _read_small_descriptor(anchor_descriptor)
        after = publication.lstat(anchor_name)
        if _directory_identity(after) != anchor_identity:
            raise _unsafe_error("output transaction anchor changed while checked")
        if _parse_output_lock_anchor(payload) != (directory_identity, lock_identity):
            raise _unsafe_error("output transaction anchor content changed")
        after_lock = directory.lstat(self.name)
        if _directory_identity(after_lock) != lock_identity:
            raise _unsafe_error("output transaction lock changed while checked")

    def close(self, primary: BaseException | None = None) -> None:
        with self._close_guard:
            descriptor = self._descriptor
            if descriptor is None and self._anchor_descriptor is None:
                return
            failures: list[BaseException] = []
            if self._locked and descriptor is None:
                failures.append(
                    _unsafe_error("output transaction lost its locked descriptor")
                )
                self._locked = False
            for attribute in ("_descriptor", "_anchor_descriptor"):
                owned_descriptor = getattr(self, attribute)
                if owned_descriptor is None:
                    continue
                # POSIX does not define whether an fd remains open after every
                # close error.  Consume the integer before the attempt so no
                # retry can close an unrelated descriptor that reused it.
                setattr(self, attribute, None)
                try:
                    os.close(owned_descriptor)
                except BaseException as error:
                    failures.append(error)
                finally:
                    if attribute == "_descriptor":
                        self._locked = False
            local_lock = self._local_lock
            if local_lock is not None:
                _release_local_advisory_lock(self._local_key, local_lock)
                self._local_lock = None
            if not failures:
                return
            detail = "; ".join(str(error) for error in failures)
            if primary is not None:
                primary.add_note(
                    f"additional output-transaction lock cleanup failure: {detail}"
                )
                return
            failure = _unsafe_error(f"cannot close output-transaction lock: {detail}")
            raise failure from failures[0]


class LockedSlotFile:
    """One exact fixed-slot inode locked until its owning artifact closes."""

    __slots__ = (
        "_adapter",
        "_close_guard",
        "_descriptor",
        "_directory",
        "_identity",
        "_local_key",
        "_local_lock",
        "_locked",
        "name",
    )

    def __init__(
        self,
        directory: RecoveryDirectory,
        name: str,
        descriptor: int,
        identity: tuple[int, int],
        adapter: _SystemAdvisoryFileLock,
        local_key: str,
        local_lock: Lock,
        *,
        locked: bool = True,
    ) -> None:
        self._directory = directory
        self.name = name
        self._descriptor: int | None = descriptor
        self._identity = identity
        self._adapter = adapter
        self._close_guard = Lock()
        self._local_key = local_key
        self._local_lock: Lock | None = local_lock
        self._locked = locked

    @property
    def descriptor(self) -> int:
        descriptor = self._descriptor
        if descriptor is None:
            raise _unsafe_error("output private slot is closed")
        return descriptor

    @property
    def identity(self) -> tuple[int, int]:
        return self._identity

    def assert_owned(self) -> None:
        if not self._locked or self._local_lock is None:
            raise _unsafe_error("output private slot lock is no longer owned")
        descriptor = self.descriptor
        self._directory.assert_handle_owned()
        opened = os.fstat(descriptor)
        current = self._directory.lstat(self.name)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or _directory_identity(opened) != self._identity
            or _directory_identity(current) != self._identity
        ):
            raise _unsafe_error("output private slot ownership changed")

    def reset_for_write(self, source_descriptor: int) -> None:
        """Truncate only this locked, singly-linked inode, never its source."""
        self.assert_owned()
        descriptor = self.descriptor
        opened = os.fstat(descriptor)
        source = os.fstat(source_descriptor)
        if _directory_identity(source) == self._identity:
            raise _unsafe_error("output private slot aliases its write source")
        if opened.st_nlink != 1:
            raise _unsafe_error("hard-linked output private slot cannot be recycled")
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        self.assert_owned()

    def rename_to(self, name: str) -> None:
        _validate_component(name)
        current = self._directory.lstat(name)
        if _directory_identity(current) != self._identity:
            raise _unsafe_error("renamed output private slot identity changed")
        self.name = name
        self.assert_owned()

    def close(self, primary: BaseException | None = None) -> None:
        with self._close_guard:
            descriptor = self._descriptor
            if descriptor is None:
                return
            self._descriptor = None
            failure: BaseException | None = None
            try:
                os.close(descriptor)
            except BaseException as error:
                failure = error
            finally:
                self._locked = False
            local_lock = self._local_lock
            if local_lock is not None:
                _release_local_advisory_lock(self._local_key, local_lock)
                self._local_lock = None
            if failure is None:
                return
            if primary is not None:
                primary.add_note(f"additional output-slot cleanup failure: {failure}")
                return
            structured = _unsafe_error(f"cannot close output private slot: {failure}")
            raise structured from failure


class RecoveryDirectory:
    """A private child resolved from one already-bound publication parent.

    All names are handle-relative. ``path_for`` is diagnostic-only.
    """

    __slots__ = (
        "_directory",
        "_identity",
        "_owned_parent",
        "_parent",
        "_stack",
        "name",
        "path",
    )

    def __init__(
        self,
        path: Path,
        name: str,
        parent: PublicationDirectory,
        directory: _BoundDirectory,
        identity: tuple[int, int],
        stack: ExitStack,
        owned_parent: PublicationDirectory | None,
    ) -> None:
        self.path = path
        self.name = name
        self._parent = parent
        self._directory = directory
        self._identity = identity
        self._stack = stack
        self._owned_parent = owned_parent

    @classmethod
    def open(cls, parent_path: Path, name: str) -> RecoveryDirectory:
        parent = PublicationDirectory.open(parent_path)
        try:
            result = cls.open_from(parent, name, "recovery")
        except BaseException as error:
            try:
                parent.close(error)
            except BaseException as cleanup_error:
                error.add_note(
                    f"additional recovery-parent cleanup failure: {cleanup_error}"
                )
            raise
        result._owned_parent = parent
        return result

    @classmethod
    def open_from(
        cls,
        parent: PublicationDirectory,
        name: str,
        purpose: str,
    ) -> RecoveryDirectory:
        _validate_component(name)
        if not isinstance(purpose, str) or not purpose:
            raise TypeError("private output-directory purpose is required")
        stack = ExitStack()
        try:
            try:
                info = parent._directory.lstat(name)
            except FileNotFoundError:
                parent._directory.mkdir(name, exist_ok=False)
                parent.fsync()
                info = parent._directory.lstat(name)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _unsafe_error(f"output {purpose} namespace is redirected")
            if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
                raise _unsafe_error(
                    f"output {purpose} namespace must have mode 0700 or stricter"
                )
            directory = stack.enter_context(
                parent._directory.open_publication_child(name)
            )
            result = cls(
                parent.path / name,
                name,
                parent,
                directory,
                _directory_identity(parent.lstat(name)),
                stack,
                None,
            )
            result.assert_still_bound()
            return result
        except BaseException as error:
            try:
                stack.__exit__(type(error), error, error.__traceback__)
            except BaseException as cleanup_error:
                error.add_note(
                    f"additional private-directory cleanup failure: {cleanup_error}"
                )
            raise

    def close(self, primary: BaseException | None = None) -> None:
        close_primary = primary
        try:
            if close_primary is None:
                self._stack.close()
            else:
                self._stack.__exit__(
                    type(close_primary),
                    close_primary,
                    close_primary.__traceback__,
                )
        except BaseException as error:
            close_primary = error
        owned_parent = self._owned_parent
        self._owned_parent = None
        if owned_parent is not None:
            try:
                owned_parent.close(close_primary)
            except BaseException as error:
                if close_primary is not None:
                    close_primary.add_note(
                        f"additional recovery-parent cleanup failure: {error}"
                    )
                else:
                    close_primary = error
        if primary is None and close_primary is not None:
            raise close_primary

    def assert_still_bound(self) -> None:
        self._parent.assert_still_bound()
        self._directory.assert_still_named()
        current = self._parent.lstat(self.name)
        if (
            not stat.S_ISDIR(current.st_mode)
            or _directory_identity(current) != self._identity
        ):
            raise _unsafe_error("output private directory ownership changed")

    def assert_handle_owned(self) -> None:
        self._parent.assert_handle_bound()
        self._directory.assert_handle_safe()
        current = self._parent.lstat(self.name)
        if (
            not stat.S_ISDIR(current.st_mode)
            or _directory_identity(current) != self._identity
        ):
            raise _unsafe_error("output private directory ownership changed")

    @property
    def identity(self) -> tuple[int, int]:
        return self._identity

    def path_for(self, name: str) -> Path:
        _validate_component(name)
        return self.path / name

    def lstat(self, name: str) -> os.stat_result:
        return self._directory.publication_lstat(name)

    def open_read(self, name: str) -> int:
        return self._directory.open_publication_read(name)

    def open_read_write(self, name: str) -> int:
        return self._directory.open_publication_read_write(name)

    def open_locked_slot(
        self,
        name: str,
        owner: AdvisoryFileLock,
        *,
        create_if_missing: bool = True,
    ) -> LockedSlotFile:
        """Open and lock one exact fixed-slot inode before any mutation."""
        _validate_component(name)
        if not isinstance(owner, AdvisoryFileLock) or not owner.anchored:
            raise _unsafe_error("output private slot requires an anchored owner")
        owner.assert_owned()
        descriptor_owner: _OpenedDescriptorOwner | None = None
        held: LockedSlotFile | None = None
        local_lock: Lock | None = None
        local_key = ""
        try:
            if create_if_missing:
                try:
                    descriptor_owner = _OpenedDescriptorOwner(
                        self._directory.open_new_publication_fd(name)
                    )
                except FileExistsError:
                    descriptor_owner = None
            if descriptor_owner is None:
                before = self._directory.publication_lstat(name)
                if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                    raise _unsafe_error("output private slot is redirected")
                descriptor_owner = _OpenedDescriptorOwner(
                    self._directory.open_publication_read_write(name)
                )
                opened = os.fstat(descriptor_owner.descriptor)
                after = self._directory.publication_lstat(name)
                if not (
                    _directory_identity(before)
                    == _directory_identity(opened)
                    == _directory_identity(after)
                ):
                    raise _unsafe_error("output private slot changed while opened")
            identity = _directory_identity(os.fstat(descriptor_owner.descriptor))
            local_key = f"output-slot:{identity[0]}:{identity[1]}"
            local_lock = _acquire_local_advisory_lock(local_key)
            if local_lock is None:
                raise BlockingIOError(
                    errno.EWOULDBLOCK,
                    "output private slot is already active",
                )
            adapter = _SystemAdvisoryFileLock()
            held = LockedSlotFile(
                self,
                name,
                descriptor_owner.descriptor,
                identity,
                adapter,
                local_key,
                local_lock,
                locked=False,
            )
            descriptor_owner.transfer()
            descriptor_owner = None
            local_lock = None
            if not adapter.acquire_nonblocking(held.descriptor):
                raise BlockingIOError(
                    errno.EWOULDBLOCK,
                    "output private slot is already active",
                )
            held._locked = True
            held.assert_owned()
            owner.assert_owned()
            return held
        except BaseException as error:
            if held is not None:
                held.close(error)
            elif descriptor_owner is not None:
                descriptor_owner.close(error, detail="output-slot handle")
            if local_lock is not None:
                _release_local_advisory_lock(local_key, local_lock)
            raise

    def open_fixed_pending(
        self,
        name: str,
        owner: AdvisoryFileLock | None = None,
    ) -> int:
        """Create a new bounded pending inode; existing slots require a lock."""
        if owner is not None:
            owner.assert_owned()
        try:
            descriptor = self._directory.open_new_publication_fd(name)
        except FileExistsError:
            raise _unsafe_error(
                "existing output private pending entry has no transaction owner "
                "or inode lock"
            )
        if owner is not None:
            try:
                owner.assert_owned()
            except BaseException:
                os.close(descriptor)
                raise
        return descriptor

    def acquire_advisory_lock(self, name: str) -> AdvisoryFileLock:
        """Acquire a fixed, never-unlinked process lock in this bound directory."""
        return self._acquire_advisory_lock(name)

    def _acquire_advisory_lock(
        self,
        name: str,
        *,
        publication: PublicationDirectory | None = None,
        anchor_name: str | None = None,
    ) -> AdvisoryFileLock:
        _validate_component(name)
        if (publication is None) != (anchor_name is None):
            raise ValueError("publication and output-lock anchor must be paired")
        if publication is not None:
            if publication is not self._parent:
                raise _unsafe_error("output lock parent does not own private directory")
            if anchor_name is None:
                raise AssertionError("validated output-lock anchor is missing")
            _validate_component(anchor_name)
        descriptor_owner: _OpenedDescriptorOwner | None = None
        anchor_owner: _OpenedDescriptorOwner | None = None
        held: AdvisoryFileLock | None = None
        local_path = (
            publication.path_for(anchor_name)
            if publication is not None and anchor_name is not None
            else self.path_for(name)
        )
        local_key = os.path.normcase(os.path.abspath(local_path))
        if os.name == "nt" or sys.platform == "darwin":
            local_key = unicodedata.normalize("NFC", local_key).casefold()
        local_lock = _acquire_local_advisory_lock(local_key)
        if local_lock is None:
            raise BlockingIOError(
                errno.EWOULDBLOCK,
                "output transaction is already active",
            )
        created = False
        try:
            expected_directory_identity: tuple[int, int] | None = None
            expected_lock_identity: tuple[int, int] | None = None
            anchor_identity: tuple[int, int] | None = None
            if publication is not None and anchor_name is not None:
                try:
                    before_anchor = publication.lstat(anchor_name)
                    if stat.S_ISLNK(before_anchor.st_mode) or not stat.S_ISREG(
                        before_anchor.st_mode
                    ):
                        raise _unsafe_error("output transaction anchor is redirected")
                    anchor_owner = _OpenedDescriptorOwner(
                        publication.open_read(anchor_name)
                    )
                    opened_anchor = os.fstat(anchor_owner.descriptor)
                    payload = _read_small_descriptor(anchor_owner.descriptor)
                    after_anchor = publication.lstat(anchor_name)
                    if not (
                        _directory_identity(before_anchor)
                        == _directory_identity(opened_anchor)
                        == _directory_identity(after_anchor)
                    ):
                        raise _unsafe_error(
                            "output transaction anchor changed while opened"
                        )
                    anchor_identity = _directory_identity(opened_anchor)
                    (
                        expected_directory_identity,
                        expected_lock_identity,
                    ) = _parse_output_lock_anchor(payload)
                    if expected_directory_identity != self.identity:
                        raise _unsafe_error(
                            "output transaction private directory does not match anchor"
                        )
                except FileNotFoundError:
                    if anchor_owner is not None:
                        raced_anchor_owner = anchor_owner
                        anchor_owner = None
                        raced_anchor_owner.close(
                            detail="raced output-transaction anchor handle"
                        )

            try:
                if expected_lock_identity is not None:
                    before = self._directory.publication_lstat(name)
                    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                        raise _unsafe_error("output transaction lock is redirected")
                    descriptor_owner = _OpenedDescriptorOwner(
                        self._directory.open_publication_read_write(name)
                    )
                    opened = os.fstat(descriptor_owner.descriptor)
                    after_open = self._directory.publication_lstat(name)
                    if not (
                        _directory_identity(before)
                        == _directory_identity(opened)
                        == _directory_identity(after_open)
                        == expected_lock_identity
                    ):
                        raise _unsafe_error(
                            "output transaction lock does not match anchor"
                        )
                else:
                    descriptor_owner = _OpenedDescriptorOwner(
                        self._directory.open_new_publication_fd(name)
                    )
                    created = True
            except FileExistsError:
                before = self._directory.publication_lstat(name)
                if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                    raise _unsafe_error("output transaction lock is redirected")
                descriptor_owner = _OpenedDescriptorOwner(
                    self._directory.open_publication_read_write(name)
                )
                opened = os.fstat(descriptor_owner.descriptor)
                after_open = self._directory.publication_lstat(name)
                if not (
                    _directory_identity(before)
                    == _directory_identity(opened)
                    == _directory_identity(after_open)
                ):
                    raise _unsafe_error("output transaction lock changed while opened")

            adapter = _SystemAdvisoryFileLock()
            if descriptor_owner is None:
                raise RuntimeError("output transaction descriptor was not opened")
            lock_identity = _directory_identity(os.fstat(descriptor_owner.descriptor))
            held = AdvisoryFileLock(
                name,
                descriptor_owner.descriptor,
                adapter,
                local_key,
                local_lock,
                locked=False,
                directory=self,
                lock_identity=lock_identity,
                publication=publication,
                anchor_name=anchor_name,
                anchor_descriptor=(
                    anchor_owner.descriptor if anchor_owner is not None else None
                ),
                anchor_identity=anchor_identity,
                directory_identity=self.identity if publication is not None else None,
            )
            descriptor_owner.transfer()
            if anchor_owner is not None:
                anchor_owner.transfer()
            descriptor_owner = None
            anchor_owner = None
            locked_descriptor = held._descriptor
            if locked_descriptor is None:
                raise RuntimeError(
                    "acquired output-transaction lock lost its descriptor"
                )
            if not adapter.acquire_nonblocking(locked_descriptor):
                raise BlockingIOError(
                    errno.EWOULDBLOCK,
                    "output transaction is already active",
                )
            held._locked = True
            locked_info = os.fstat(locked_descriptor)
            if locked_info.st_size == 0:
                os.ftruncate(locked_descriptor, 1)
                os.fsync(locked_descriptor)
            current = self._directory.publication_lstat(name)
            if (
                stat.S_ISLNK(current.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or _directory_identity(current)
                != _directory_identity(os.fstat(locked_descriptor))
            ):
                raise _unsafe_error("output transaction lock changed while acquired")
            if created:
                self._directory.fsync()
            if publication is not None and anchor_name is not None:
                if held._anchor_descriptor is None:
                    payload = _output_lock_anchor_payload(
                        self.identity,
                        _directory_identity(os.fstat(locked_descriptor)),
                    )
                    new_anchor_owner = _OpenedDescriptorOwner(
                        publication.open_new(anchor_name)
                    )
                    try:
                        _write_all(new_anchor_owner.descriptor, payload)
                        os.fsync(new_anchor_owner.descriptor)
                        new_anchor_info = os.fstat(new_anchor_owner.descriptor)
                        current_anchor = publication.lstat(anchor_name)
                        if not stat.S_ISREG(
                            current_anchor.st_mode
                        ) or _directory_identity(current_anchor) != _directory_identity(
                            new_anchor_info
                        ):
                            raise _unsafe_error(
                                "output transaction anchor changed while created"
                            )
                        publication.fsync()
                    except BaseException as error:
                        new_anchor_owner.close(
                            error,
                            detail="output-transaction anchor handle",
                        )
                        raise
                    held._anchor_descriptor = new_anchor_owner.transfer()
                    held._anchor_identity = _directory_identity(new_anchor_info)
                held.assert_owned()
            else:
                self.assert_still_bound()
            return held
        except BaseException as error:
            if held is not None:
                held.close(error)
            elif descriptor_owner is not None:
                descriptor_owner.close(
                    error,
                    detail="output-transaction lock handle",
                )
            if anchor_owner is not None:
                anchor_owner.close(
                    error,
                    detail="output-transaction anchor handle",
                )
            if held is None:
                _release_local_advisory_lock(local_key, local_lock)
            raise

    def link_parent_file(
        self,
        source: str,
        destination: str,
        owner: AdvisoryFileLock | None = None,
    ) -> bool:
        _validate_component(source)
        _validate_component(destination)
        parent_descriptor = self._parent._directory.descriptor
        if parent_descriptor is None or self._directory.descriptor is None:
            return False
        if owner is not None:
            owner.assert_owned()
        os.link(
            source,
            destination,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=self._directory.descriptor,
            follow_symlinks=False,
        )
        if owner is not None:
            owner.assert_owned()
        return True

    def link_file(
        self,
        source: str,
        destination: str,
        owner: AdvisoryFileLock | None = None,
    ) -> bool:
        _validate_component(source)
        _validate_component(destination)
        if self._directory.descriptor is None:
            return False
        if owner is not None:
            owner.assert_owned()
        os.link(
            source,
            destination,
            src_dir_fd=self._directory.descriptor,
            dst_dir_fd=self._directory.descriptor,
            follow_symlinks=False,
        )
        if owner is not None:
            owner.assert_owned()
        return True

    def replace(self, source: str, destination: str) -> None:
        self._directory.replace_publication(source, destination)

    def replace_owned(
        self,
        source: str,
        destination: str,
        source_slot: LockedSlotFile,
        owner: AdvisoryFileLock,
        *,
        source_alias: bool = False,
    ) -> None:
        """Rename a locked source only after locking an existing destination."""
        _validate_component(source)
        _validate_component(destination)
        if source_slot._directory is not self or (
            not source_alias and source_slot.name != source
        ):
            raise _unsafe_error("output private source slot does not match rename")
        owner.assert_owned()
        self.assert_handle_owned()
        source_slot.assert_owned()
        source_info = self.lstat(source)
        if _directory_identity(source_info) != source_slot.identity:
            raise _unsafe_error("output private source alias is not locked")
        destination_slot: LockedSlotFile | None = None
        primary: BaseException | None = None
        try:
            try:
                destination_info = self.lstat(destination)
            except FileNotFoundError:
                destination_info = None
            if (
                destination_info is not None
                and _directory_identity(destination_info) == source_slot.identity
            ):
                current_source = self.lstat(source)
                if _directory_identity(current_source) != source_slot.identity:
                    raise _unsafe_error("locked output private alias changed")
                # Both fixed names already identify the held inode.  There is
                # no portable unlink-if-identity operation: a pathname check
                # followed by unlink could delete a replacement entry.  Keep
                # the bounded alias and its inode lock instead of mutating it.
                source_slot.assert_owned()
                owner.assert_owned()
                return
            try:
                destination_slot = self.open_locked_slot(
                    destination,
                    owner,
                    create_if_missing=False,
                )
            except FileNotFoundError:
                destination_slot = None
            source_slot.assert_owned()
            owner.assert_owned()
            if destination_slot is None:
                _rename_bound_publication(
                    self._directory,
                    source,
                    self._directory,
                    destination,
                    replace=False,
                )
            else:
                destination_slot.assert_owned()
                self._directory.replace_publication(source, destination)
            if source_alias:
                source_slot.assert_owned()
                installed = self.lstat(destination)
                if _directory_identity(installed) != source_slot.identity:
                    raise _unsafe_error("locked output private alias was not installed")
            else:
                source_slot.rename_to(destination)
            owner.assert_owned()
        except BaseException as error:
            primary = error
            raise
        finally:
            if destination_slot is not None:
                destination_slot.close(primary)

    def fsync(self) -> None:
        self._directory.fsync()


def _rename_bound_publication(
    source_directory: _BoundDirectory,
    source: str,
    destination_directory: _BoundDirectory,
    destination: str,
    *,
    replace: bool,
) -> None:
    _validate_component(source)
    _validate_component(destination)
    if (
        source_directory.descriptor is not None
        and destination_directory.descriptor is not None
    ):
        if replace:
            os.replace(
                source,
                destination,
                src_dir_fd=source_directory.descriptor,
                dst_dir_fd=destination_directory.descriptor,
            )
            return
        _rename_no_replace_bound(
            source_directory.descriptor,
            source,
            destination_directory.descriptor,
            destination,
        )
        return
    if source_directory._windows_handles and destination_directory._windows_handles:
        if source_directory._windows_api is not destination_directory._windows_api:
            raise _unsafe_error("publication directories have different handle owners")
        source_directory._windows_api.rename_publication_at(
            source_directory._windows_handles[-1],
            source,
            destination_directory._windows_handles[-1],
            destination,
            replace=replace,
        )
        return
    raise _unsafe_error("handle-relative output rename is unavailable")


def _rename_no_replace_bound(
    source_descriptor: int,
    source: str,
    destination_descriptor: int,
    destination: str,
) -> None:
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        renamex = getattr(libc, "renameatx_np", None)
        if renamex is None:
            raise OSError(errno.ENOTSUP, "renameatx_np is unavailable")
        renamex.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renamex.restype = ctypes.c_int
        result = renamex(
            source_descriptor,
            os.fsencode(source),
            destination_descriptor,
            os.fsencode(destination),
            0x00000004,
        )
    elif sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOTSUP, "renameat2 is unavailable")
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            source_descriptor,
            os.fsencode(source),
            destination_descriptor,
            os.fsencode(destination),
            0x00000001,
        )
    else:
        raise OSError(
            errno.ENOTSUP,
            "atomic handle-relative no-replace rename is unsupported",
        )
    if result == 0:
        return
    code = ctypes.get_errno()
    if code == errno.EEXIST:
        raise FileExistsError(code, os.strerror(code), destination)
    raise OSError(code, os.strerror(code), destination)


def _workspace_layout(
    output_directory: Path, *, create: bool
) -> tuple[Path, Path, Path, Path]:
    output = _canonical_output_directory(output_directory)
    root = output / ".rembggui-work"
    cuts = root / "cuts"
    scratch = root / "scratch"
    if create:
        try:
            with _BoundDirectory.open(output) as output_bound:
                output_bound.mkdir(root.name, exist_ok=True)
                with output_bound.open_child(root.name) as root_bound:
                    root_bound.mkdir(cuts.name, exist_ok=True)
                    root_bound.mkdir(scratch.name, exist_ok=True)
                    with root_bound.open_child(cuts.name):
                        pass
                    with root_bound.open_child(scratch.name):
                        pass
        except AppError:
            raise
        except OSError as error:
            raise _unsafe_error(
                f"cannot create workspace directory: {error}"
            ) from error
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
    absolute = Path(os.path.abspath(path))
    try:
        with _BoundDirectory.open(absolute):
            pass
    except OSError as error:
        raise _unsafe_error("output directory must exist locally") from error
    _assert_local_filesystem(absolute)
    return absolute


def _assert_safe_directory(path: Path) -> None:
    try:
        with _BoundDirectory.open(path):
            pass
    except AppError:
        raise
    except OSError as error:
        raise _unsafe_error(f"workspace directory is unavailable: {error}") from error


def _assert_local_filesystem(
    path: Path,
    *,
    probe: Callable[[_BoundDirectory], bool] | None = None,
) -> None:
    try:
        with _BoundDirectory.open(path) as bound:
            checker = _default_local_filesystem_probe if probe is None else probe
            if not checker(bound):
                raise _unsafe_error(
                    "network workspaces are outside the local-filesystem contract"
                )
    except AppError:
        raise
    except OSError as error:
        raise _unsafe_error(
            f"cannot prove the workspace filesystem is local: {error}"
        ) from error


def _default_local_filesystem_probe(bound: _BoundDirectory) -> bool:
    if os.name == "nt":
        text = str(bound.path)
        if text.startswith(("\\\\", "//")):
            return False
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        return _windows_drive_type_is_local(
            int(kernel32.GetDriveTypeW(str(Path(bound.path.anchor))))
        )
    descriptor = bound.descriptor
    if descriptor is None:
        return False
    flags = os.fstatvfs(descriptor).f_flag
    local_flag = getattr(os, "ST_LOCAL", None)
    if local_flag is not None:
        return bool(flags & local_flag)
    if sys.platform == "darwin":
        return _darwin_descriptor_is_local(descriptor)
    if sys.platform.startswith("linux"):
        info = os.fstat(descriptor)
        return _linux_mount_is_local(info.st_dev)
    raise OSError(errno.ENOTSUP, "host has no local-filesystem identity probe")


def _darwin_descriptor_is_local(descriptor: int) -> bool:
    class DarwinStatfs(ctypes.Structure):
        _fields_ = (
            ("f_bsize", ctypes.c_uint32),
            ("f_iosize", ctypes.c_int32),
            ("f_blocks", ctypes.c_uint64),
            ("f_bfree", ctypes.c_uint64),
            ("f_bavail", ctypes.c_uint64),
            ("f_files", ctypes.c_uint64),
            ("f_ffree", ctypes.c_uint64),
            ("f_fsid", ctypes.c_int32 * 2),
            ("f_owner", ctypes.c_uint32),
            ("f_type", ctypes.c_uint32),
            ("f_flags", ctypes.c_uint32),
            ("f_fssubtype", ctypes.c_uint32),
            ("f_fstypename", ctypes.c_char * 16),
            ("f_mntonname", ctypes.c_char * 1024),
            ("f_mntfromname", ctypes.c_char * 1024),
            ("f_reserved", ctypes.c_uint32 * 8),
        )

    filesystem = DarwinStatfs()
    libc = ctypes.CDLL(None, use_errno=True)
    fstatfs = libc.fstatfs
    fstatfs.argtypes = [ctypes.c_int, ctypes.POINTER(DarwinStatfs)]
    fstatfs.restype = ctypes.c_int
    if fstatfs(descriptor, ctypes.byref(filesystem)) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return bool(filesystem.f_flags & 0x00001000)


def _windows_drive_type_is_local(drive_type: int) -> bool:
    # DRIVE_REMOVABLE, FIXED, CDROM, and RAMDISK are local. UNKNOWN,
    # NO_ROOT_DIR, and REMOTE cannot satisfy the durable-workspace contract.
    return drive_type in {2, 3, 5, 6}


def _linux_mount_is_local(device: int) -> bool:
    major_minor = f"{os.major(device)}:{os.minor(device)}"
    with open("/proc/self/mountinfo", "rb") as source:
        encoded = source.read(MAX_MOUNTINFO_BYTES + 1)
    return _linux_mountinfo_is_local(encoded, major_minor)


def _linux_mountinfo_is_local(encoded: bytes, major_minor: str) -> bool:
    local_types = {
        "aufs",
        "btrfs",
        "erofs",
        "exfat",
        "ext2",
        "ext3",
        "ext4",
        "f2fs",
        "hfsplus",
        "iso9660",
        "jfs",
        "nilfs2",
        "ntfs",
        "ntfs3",
        "overlay",
        "ramfs",
        "reiserfs",
        "squashfs",
        "tmpfs",
        "udf",
        "ufs",
        "vfat",
        "xfs",
        "zfs",
    }
    if len(encoded) > MAX_MOUNTINFO_BYTES:
        raise OSError(errno.EOVERFLOW, "mount table exceeds its parsing bound")
    matches: list[str] = []
    for raw_line in encoded.splitlines():
        if len(raw_line) > MAX_PATH_CHARS * 4:
            raise OSError(errno.EOVERFLOW, "mount table line exceeds its bound")
        fields = raw_line.split(b" ")
        if len(fields) < 10 or fields[2].decode("ascii", "strict") != major_minor:
            continue
        try:
            separator = fields.index(b"-")
            filesystem = fields[separator + 1].decode("ascii", "strict").lower()
        except (ValueError, IndexError, UnicodeDecodeError) as error:
            raise OSError(errno.EINVAL, "mount table entry is malformed") from error
        matches.append(filesystem)
        if len(matches) > 64:
            raise OSError(errno.EOVERFLOW, "device has too many mount bindings")
    if not matches:
        raise OSError(errno.ENODEV, "workspace mount identity is unavailable")
    return all(filesystem in local_types for filesystem in matches)


def _read_manifest(path: Path) -> tuple[CutManifest, tuple[int, int, int, int, int]]:
    try:
        with _BoundDirectory.open(path) as bound:
            return _read_bound_manifest(bound)
    except AppError:
        raise
    except OSError as error:
        raise _manifest_error(f"cannot read manifest: {error}") from error


def _read_bound_manifest(
    bound: _BoundDirectory,
) -> tuple[CutManifest, tuple[int, int, int, int, int]]:
    try:
        descriptor = bound.open_read(MANIFEST_FILENAME)
        with _fdopen_owned(descriptor, "rb") as source:
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


def _write_manifest_atomic(
    path: Path,
    manifest: CutManifest,
    *,
    expected_identity: tuple[int, int, int, int, int] | None = None,
) -> None:
    if type(manifest) is not CutManifest:
        raise _manifest_error("manifest must be an exact CutManifest")
    encoded = manifest.to_json_bytes()
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise _manifest_error("manifest exceeds the bounded byte limit")
    temporary = f".manifest-{uuid.uuid4().hex}.tmp"
    try:
        with _BoundDirectory.open(path) as bound:
            try:
                output = bound.open_new(temporary)
                try:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
                finally:
                    output.close()
                if expected_identity is not None:
                    current = bound.lstat(MANIFEST_FILENAME)
                    if _stat_identity(current) != expected_identity:
                        raise _manifest_error(
                            "manifest changed before the atomic update committed"
                        )
                bound.replace(temporary, MANIFEST_FILENAME)
                bound.fsync()
                bound.assert_still_named()
            finally:
                try:
                    bound.unlink(temporary)
                except FileNotFoundError:
                    pass
    except AppError:
        raise
    except OSError as error:
        raise _manifest_error(f"cannot atomically write manifest: {error}") from error


def _write_journal(
    path: Path,
    payload: Mapping[str, object],
    *,
    bound: _BoundDirectory | None = None,
) -> None:
    encoded = _canonical_json(dict(payload)) + b"\n"
    if len(encoded) > 16 * 1024:
        raise _promotion_error("promotion journal exceeds its byte bound")
    if bound is not None:
        if not _same_lexical_path(bound.path, path.parent):
            raise _unsafe_error("promotion journal bound to the wrong directory")
        _write_bound_journal(bound, path.name, encoded)
        return
    with _BoundDirectory.open(path.parent) as opened_bound:
        _write_bound_journal(opened_bound, path.name, encoded)


def _write_bound_journal(bound: _BoundDirectory, name: str, encoded: bytes) -> None:
    _validate_component(name)
    temporary = f".{name}-{uuid.uuid4().hex}.tmp"
    try:
        output = bound.open_new(temporary)
        try:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        finally:
            output.close()
        bound.replace(temporary, name)
        bound.fsync()
    finally:
        try:
            bound.unlink(temporary)
        except FileNotFoundError:
            pass


def _recover_all_promotions(cuts_root: Path) -> None:
    with _BoundDirectory.open(cuts_root) as bound:
        keys: list[str] = []
        for name, info in bound.iter_entries():
            match = _MARKER_RE.fullmatch(name)
            if match is None:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise _unsafe_error("promotion recovery marker is redirected")
            if len(keys) >= MAX_WORKSPACE_ENTRIES:
                raise _unsafe_error("promotion recovery marker count is unbounded")
            keys.append(match.group(1))
    for key in keys:
        with _promotion_lock(str(cuts_root / key)):
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
                with _fdopen_owned(descriptor, "rb") as source:
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
            with _BoundDirectory.open(cuts_root) as bound:
                bound.replace_directory(old_location.name, target.name)
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
    with _BoundDirectory.open(left.parent) as parent:
        descriptor = parent.descriptor
        if descriptor is None:
            return False
        if sys.platform == "darwin":
            libc = ctypes.CDLL(None, use_errno=True)
            renameatx = getattr(libc, "renameatx_np", None)
            if renameatx is None:
                return False
            renameatx.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameatx.restype = ctypes.c_int
            if (
                renameatx(
                    descriptor,
                    os.fsencode(left.name),
                    descriptor,
                    os.fsencode(right.name),
                    0x00000002,
                )
                == 0
            ):
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
            if (
                renameat2(
                    descriptor,
                    os.fsencode(left.name),
                    descriptor,
                    os.fsencode(right.name),
                    2,
                )
                == 0
            ):
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
    with _BoundDirectory.open(path.parent) as parent:
        _remove_bound_tree(parent, path.name)


def _remove_bound_tree(parent: _BoundDirectory, name: str) -> None:
    _validate_component(name)
    info = parent.lstat(name)
    if stat.S_ISLNK(info.st_mode):
        parent.unlink(name)
        return
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("tree target is not a directory")
    with parent.open_child(name) as child:
        _remove_bound_contents(child, [0])
    parent.rmdir(name)
    parent.fsync()


def _remove_bound_contents(bound: _BoundDirectory, removed: list[int]) -> None:
    while True:
        selected: tuple[str, os.stat_result] | None = None
        for name, info in bound.iter_entries():
            removed[0] += 1
            if removed[0] > MAX_FRAME_COUNT + 16:
                raise OSError("tree entry count exceeds cleanup bound")
            selected = name, info
            break
        if selected is None:
            return
        name, info = selected
        _validate_component(name)
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            with bound.open_child(name) as child:
                _remove_bound_contents(child, removed)
            bound.rmdir(name)
        else:
            bound.unlink(name)


def _bounded_tree_size(path: Path) -> int:
    with _BoundDirectory.open(path) as bound:
        counter = [0]
        return _bounded_bound_tree_size(bound, counter)


def _bounded_bound_tree_size(bound: _BoundDirectory, counter: list[int]) -> int:
    total = 0
    for name, info in bound.iter_entries():
        counter[0] += 1
        if counter[0] > MAX_FRAME_COUNT + 16:
            raise _unsafe_error("scratch tree exceeds the size-scan bound")
        if stat.S_ISLNK(info.st_mode):
            raise _unsafe_error("scratch tree contains a symbolic link")
        if stat.S_ISDIR(info.st_mode):
            with bound.open_child(name) as child:
                total += _bounded_bound_tree_size(child, counter)
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
    except (
        AppError,
        OSError,
        UnsafeCacheError,
        BoundDirectoryCloseError,
    ) as cleanup_error:
        primary.add_note(f"additional scratch cleanup failure: {cleanup_error}")


def _cleanup_staged_cut(
    path: Path,
    primary: AppError,
    *,
    parent: _BoundDirectory | None = None,
) -> None:
    try:
        if parent is not None:
            if not _same_lexical_path(parent.path, path.parent):
                raise _unsafe_error("staged-cut cleanup bound to the wrong directory")
            try:
                _remove_bound_tree(parent, path.name)
            except FileNotFoundError:
                pass
        elif path.exists():
            _remove_tree(path)
    except (
        AppError,
        OSError,
        UnsafeCacheError,
        BoundDirectoryCloseError,
    ) as cleanup_error:
        primary.add_note(f"additional staged-cut cleanup failure: {cleanup_error}")


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
    _exact_keys(edge, {"alpha_matting", "mode"}, "edge_settings")
    edge_mode = _bounded_text(_string(edge["mode"], "edge mode"), "edge mode")
    if edge_mode not in {"standard", "decontaminate", "alpha_matting"}:
        raise _manifest_error("edge mode is not supported by the pinned catalog")
    matting = _frozen_object(edge["alpha_matting"], "alpha matting")
    _exact_keys(
        matting,
        {"background_threshold", "erode_size", "foreground_threshold"},
        "alpha matting",
    )
    foreground = _bounded_int(
        _int(matting["foreground_threshold"], "matting foreground threshold"),
        "matting foreground threshold",
        minimum=1,
        maximum=255,
    )
    background = _bounded_int(
        _int(matting["background_threshold"], "matting background threshold"),
        "matting background threshold",
        minimum=0,
        maximum=255,
    )
    if background >= foreground:
        raise _manifest_error("matting background must be below foreground")
    _bounded_int(
        _int(matting["erode_size"], "matting erosion size"),
        "matting erosion size",
        minimum=0,
        maximum=_MAX_INT64,
    )


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
            if type(key) is not str or not key or len(key) > 64:
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
    if type(value) is FrozenJsonMap:
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return cast(JsonScalar, value)


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
    if type(value) is not FrozenJsonMap:
        raise _manifest_error(f"{field} must be an object")
    return value


def _string(value: object, field: str) -> str:
    if type(value) is not str:
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
    if type(cache_key) is not str or _CACHE_KEY_RE.fullmatch(cache_key) is None:
        raise _unsafe_error("cache key must be canonical lowercase SHA-256")


def _validate_job_id(job_id: str) -> None:
    if (
        type(job_id) is not str
        or len(job_id) > MAX_JOB_ID_CHARS
        or _SAFE_JOB_RE.fullmatch(job_id) is None
    ):
        raise _unsafe_error("job ID is not a safe workspace component")


def _validate_component(name: str) -> None:
    if (
        type(name) is not str
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
    if type(value) is not str or _CACHE_KEY_RE.fullmatch(value) is None:
        raise _manifest_error(f"{field} must be lowercase hexadecimal SHA-256")
    return value


def _hash_file(source: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := source.read(COPY_CHUNK_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


def _fdopen_owned(descriptor: int, mode: str) -> BinaryIO:
    """Transfer one descriptor to a file object or close it exactly once."""
    try:
        return cast(BinaryIO, os.fdopen(descriptor, mode))
    except BaseException as primary_error:
        try:
            os.close(descriptor)
        except BaseException as close_error:
            primary_error.add_note(
                f"additional descriptor cleanup failure: {close_error}"
            )
        raise


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


_OUTPUT_LOCK_ANCHOR_SCHEMA = "rembggui-output-lock-anchor-v1"


def _output_lock_anchor_payload(
    directory_identity: tuple[int, int],
    lock_identity: tuple[int, int],
) -> bytes:
    values = (*directory_identity, *lock_identity)
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("output lock anchor identities must be non-negative integers")
    return (
        f"{_OUTPUT_LOCK_ANCHOR_SCHEMA}\n"
        f"{directory_identity[0]}:{directory_identity[1]}\n"
        f"{lock_identity[0]}:{lock_identity[1]}\n"
    ).encode("ascii")


def _parse_output_lock_anchor(
    payload: bytes,
) -> tuple[tuple[int, int], tuple[int, int]]:
    if not isinstance(payload, bytes) or len(payload) > 256:
        raise _unsafe_error("output transaction anchor has invalid size")
    try:
        schema, directory, lock, terminator = payload.decode("ascii").split("\n")
        directory_parts = directory.split(":")
        lock_parts = lock.split(":")
        if (
            schema != _OUTPUT_LOCK_ANCHOR_SCHEMA
            or terminator
            or len(directory_parts) != 2
            or len(lock_parts) != 2
            or any(
                not part or not part.isdecimal()
                for part in (*directory_parts, *lock_parts)
            )
        ):
            raise ValueError
        values = tuple(int(part) for part in (*directory_parts, *lock_parts))
    except (UnicodeDecodeError, ValueError) as error:
        raise _unsafe_error("output transaction anchor is malformed") from error
    return (values[0], values[1]), (values[2], values[3])


def _read_small_descriptor(descriptor: int) -> bytes:
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        payload = os.read(descriptor, 257)
        if len(payload) > 256:
            raise _unsafe_error("output transaction anchor exceeds its size bound")
        return payload
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)


def _output_target_component_key(name: str, *, platform: str) -> str:
    """Hash only a validated entry component in its filesystem name domain."""
    _validate_component(name)
    if platform not in {"windows", "darwin", "posix"}:
        raise ValueError("output target platform is invalid")
    canonical = unicodedata.normalize("NFC", name)
    if platform in {"windows", "darwin"}:
        canonical = canonical.casefold()
    encoded = f"rembggui-output-target-v1\0{platform}\0{canonical}".encode()
    return hashlib.sha256(encoded).hexdigest()


def _entry_exists_no_follow(path: Path) -> bool:
    try:
        with _BoundDirectory.open(path.parent) as bound:
            info = bound.lstat(path.name)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        raise _unsafe_error(f"workspace entry {path.name!r} is redirected")
    if not stat.S_ISDIR(info.st_mode):
        raise _unsafe_error(f"workspace entry {path.name!r} is not a directory")
    return True


def _unlink_regular(path: Path) -> None:
    with _BoundDirectory.open(path.parent) as bound:
        _unlink_bound_regular(bound, path.name)


def _unlink_bound_regular(bound: _BoundDirectory, name: str) -> None:
    _validate_component(name)
    info = bound.lstat(name)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OSError("refusing to unlink a non-regular workspace entry")
    bound.unlink(name)


def _fsync_fd(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EBADF}:
            raise


def _fsync_directory(path: Path) -> None:
    with _BoundDirectory.open(path) as bound:
        bound.fsync()


def _same_lexical_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


_promotion_locks_guard = Lock()
_promotion_locks: dict[str, RLock] = {}
_advisory_locks_guard = Lock()
_advisory_locks: dict[str, Lock] = {}
_deferred_bound_directory_closes_guard = RLock()
_deferred_bound_directory_closes: list[_BoundDirectory] = []
_ATTACHED_BOUND_DIRECTORY_CLOSES = "_rembggui_bound_directory_close_owners"


def _acquire_local_advisory_lock(key: str) -> Lock | None:
    with _advisory_locks_guard:
        lock = _advisory_locks.setdefault(key, Lock())
        if not lock.acquire(blocking=False):
            return None
        return lock


def _release_local_advisory_lock(key: str, lock: Lock) -> None:
    with _advisory_locks_guard:
        lock.release()
        if _advisory_locks.get(key) is lock:
            del _advisory_locks[key]


def _attach_close_owner(primary: BaseException, owner: Any) -> None:
    attached = list(getattr(primary, _ATTACHED_BOUND_DIRECTORY_CLOSES, ()))
    if owner not in attached:
        attached.append(owner)
        setattr(primary, _ATTACHED_BOUND_DIRECTORY_CLOSES, tuple(attached))


def _promotion_lock(key: str) -> RLock:
    with _promotion_locks_guard:
        return _promotion_locks.setdefault(key, RLock())


def _retain_deferred_bound_directory_close(
    owner: _BoundDirectory, primary: BaseException
) -> None:
    with _deferred_bound_directory_closes_guard:
        if owner in _deferred_bound_directory_closes:
            return
        if len(_deferred_bound_directory_closes) < max(
            0, MAX_DEFERRED_BOUND_DIRECTORY_CLOSES
        ):
            _deferred_bound_directory_closes.append(owner)
            return
        attached = list(getattr(primary, _ATTACHED_BOUND_DIRECTORY_CLOSES, ()))
        if owner not in attached:
            attached.append(owner)
            setattr(primary, _ATTACHED_BOUND_DIRECTORY_CLOSES, tuple(attached))
        primary.add_note(
            "deferred-close registry capacity was exhausted; "
            "the retry owner remains attached to this primary error"
        )


def _forget_deferred_bound_directory_close(owner: _BoundDirectory) -> None:
    with _deferred_bound_directory_closes_guard:
        try:
            _deferred_bound_directory_closes.remove(owner)
        except ValueError:
            pass


def _pending_deferred_bound_directory_closes() -> int:
    with _deferred_bound_directory_closes_guard:
        return len(_deferred_bound_directory_closes)


def _drain_deferred_bound_directory_closes() -> int:
    closed = 0
    failures: list[AppError] = []
    with _deferred_bound_directory_closes_guard:
        for owner in tuple(_deferred_bound_directory_closes):
            try:
                owner.close()
            except AppError as error:
                failures.append(error)
            else:
                closed += 1
    if failures:
        failure = _unsafe_error(
            f"{len(failures)} deferred bound-directory close owner(s) remain"
        )
        for close_failure in failures:
            failure.add_note(f"additional deferred close failure: {close_failure}")
        raise failure
    return closed


def _drain_attached_bound_directory_closes(primary: BaseException) -> int:
    closed = 0
    failures: list[tuple[_BoundDirectory, AppError]] = []
    with _deferred_bound_directory_closes_guard:
        owners = tuple(getattr(primary, _ATTACHED_BOUND_DIRECTORY_CLOSES, ()))
        for owner in owners:
            try:
                owner.close()
            except AppError as error:
                failures.append((owner, error))
            else:
                closed += 1
        setattr(
            primary,
            _ATTACHED_BOUND_DIRECTORY_CLOSES,
            tuple(owner for owner, _error in failures),
        )
    if failures:
        failure = _unsafe_error(
            f"{len(failures)} primary-attached close owner(s) remain"
        )
        for _owner, close_failure in failures:
            failure.add_note(f"additional attached close failure: {close_failure}")
        raise failure
    return closed


def transfer_deferred_bound_directory_closes(
    source: BaseException, target: BaseException
) -> None:
    """Transfer retry owners when an internal boundary error is translated."""
    with _deferred_bound_directory_closes_guard:
        owners = tuple(getattr(source, _ATTACHED_BOUND_DIRECTORY_CLOSES, ()))
        if not owners:
            return
        existing = list(getattr(target, _ATTACHED_BOUND_DIRECTORY_CLOSES, ()))
        for owner in owners:
            if owner not in existing:
                existing.append(owner)
        setattr(target, _ATTACHED_BOUND_DIRECTORY_CLOSES, tuple(existing))
        setattr(source, _ATTACHED_BOUND_DIRECTORY_CLOSES, ())


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


def _boundary_error(kind: _BoundaryKind, detail: str) -> AppError:
    if kind == "unsafe":
        return _unsafe_error(detail)
    if kind == "set":
        return _set_error(detail)
    if kind == "stage":
        return _stage_error(detail)
    if kind == "promotion":
        return _promotion_error(detail)
    if kind == "snapshot":
        return _snapshot_error(detail)
    return _delete_error(detail)


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
