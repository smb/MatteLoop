from __future__ import annotations

from typing import TYPE_CHECKING

# ruff: noqa: F403,F405
from ._common import *  # noqa: F403,F401

if TYPE_CHECKING:
    from ._errors import _manifest_error
    from ._manifest_validation import (
        _bool,
        _bounded_int,
        _canonical_json,
        _exact_keys,
        _frame_filename,
        _frame_from_payload,
        _freeze_json,
        _int,
        _object,
        _reject_json_constant,
        _strict_object,
        _string,
        _thaw_json,
        _validate_cache_inputs,
        _validate_cache_key,
        _validate_dimensions,
        _validate_frame_index,
        _validate_sha256,
    )

__all__ = (
    "CutFrame",
    "CutManifest",
    "CutUnionMetadata",
    "FrozenJsonMap",
)


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
