"""Strict, immutable model catalog loaded from the pinned release manifest."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import NoReturn, cast
from urllib.parse import unquote, urlsplit

from rembggui.core.errors import AppError, ErrorCode
from rembggui.resources import read_resource_bytes
from rembggui.resources import resource_path as packaged_resource_path

_SCHEMA_VERSION = 1
_PINNED_REMBG_VERSION = "2.0.72"
_DEFAULT_ID = "birefnet-portrait"
_APPROVED_IDS = frozenset(
    {
        "u2net",
        "u2netp",
        "u2net_human_seg",
        "u2net_cloth_seg",
        "silueta",
        "isnet-general-use",
        "isnet-anime",
        "birefnet-general",
        "birefnet-general-lite",
        "birefnet-portrait",
        "birefnet-dis",
        "birefnet-hrsod",
        "birefnet-cod",
        "birefnet-massive",
        "bria-rmbg",
    }
)
_ROOT_FIELDS = {"schema_version", "rembg_version", "default_id", "models"}
_MODEL_FIELDS = {
    "id",
    "display_name",
    "upstream_id",
    "purpose",
    "execution_class",
    "artifact",
    "inference_defaults",
    "required_inputs",
    "edge_modes",
    "supports_render",
    "license_note",
    "privacy_note",
    "warning",
}
_ARTIFACT_FIELDS = {
    "url",
    "runtime_filename",
    "size_bytes",
    "sha256",
    "upstream_checksum",
}
_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_FILENAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\.onnx\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_UPSTREAM_CHECKSUM_PATTERN = re.compile(r"(?:md5:[0-9a-f]{32}|sha256:[0-9a-f]{64})\Z")
_EDGE_MODES = frozenset({"standard", "decontaminate", "alpha_matting"})
_RELEASE_PATH_PREFIX = "/danielgatis/rembg/releases/download/v0.0.0/"


class ExecutionClass(StrEnum):
    LOCAL = "local"


class ClothCategory(StrEnum):
    FULL = "full"


@dataclass(frozen=True, slots=True)
class InferenceDefaults:
    cloth_category: ClothCategory | None = None

    def to_primitives(self) -> dict[str, str]:
        if self.cloth_category is None:
            return {}
        return {"cloth_category": self.cloth_category.value}


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    url: str
    runtime_filename: str
    size_bytes: int
    sha256: str
    upstream_checksum: str


@dataclass(frozen=True, slots=True)
class ModelSpec:
    id: str
    display_name: str
    upstream_id: str
    purpose: str
    execution_class: ExecutionClass
    artifact: ModelArtifact | None
    inference_defaults: InferenceDefaults
    required_inputs: tuple[str, ...]
    edge_modes: tuple[str, ...]
    supports_render: bool
    license_note: str
    privacy_note: str
    warning: str


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    """The only source of model IDs and artifact trust metadata."""

    rembg_version: str
    default_id: str
    ids: tuple[str, ...]
    specs: Mapping[str, ModelSpec]

    def __post_init__(self) -> None:
        if type(self.rembg_version) is not str:
            raise _manifest_error("catalog rembg version must be a string")
        if type(self.default_id) is not str:
            raise _manifest_error("catalog default must be a string")
        if type(self.ids) not in (list, tuple):
            raise _manifest_error("catalog IDs must be an array")
        if not isinstance(self.specs, Mapping):
            raise _manifest_error("catalog specifications must be a mapping")
        ids = tuple(self.ids)
        specs = dict(self.specs)
        if self.rembg_version != _PINNED_REMBG_VERSION:
            raise _manifest_error("catalog rembg version is not the app pin")
        if self.default_id != _DEFAULT_ID:
            raise _manifest_error("catalog default is not the approved default")
        if any(type(model_id) is not str for model_id in ids):
            raise _manifest_error("catalog IDs must be strings")
        if (
            len(ids) != len(_APPROVED_IDS)
            or len(set(ids)) != len(ids)
            or set(ids) != _APPROVED_IDS
            or set(specs) != _APPROVED_IDS
        ):
            raise _manifest_error("catalog IDs are duplicate, missing, or unknown")
        for model_id, spec in specs.items():
            if type(spec) is not ModelSpec or spec.id != model_id:
                raise _manifest_error("catalog model key and specification mismatch")
            _validate_model_spec(spec)
        object.__setattr__(self, "ids", ids)
        object.__setattr__(self, "specs", MappingProxyType(specs))

    @classmethod
    def _build(
        cls, *, rembg_version: str, default_id: str, specs: tuple[ModelSpec, ...]
    ) -> ModelCatalog:
        by_id = {spec.id: spec for spec in specs}
        return cls(
            rembg_version,
            default_id,
            tuple(spec.id for spec in specs),
            MappingProxyType(by_id),
        )

    @staticmethod
    def resource_path(*, runtime_root: Path | None = None) -> Path:
        return packaged_resource_path("model-manifest.json", runtime_root=runtime_root)

    @staticmethod
    def provenance_path(*, runtime_root: Path | None = None) -> Path:
        return packaged_resource_path(
            "model-provenance.json", runtime_root=runtime_root
        )

    @classmethod
    def load_resource(cls, *, runtime_root: Path | None = None) -> ModelCatalog:
        try:
            raw = read_resource_bytes(
                "model-manifest.json",
                runtime_root=runtime_root,
            )
        except (OSError, RuntimeError) as error:
            raise _manifest_error(
                f"pinned model manifest could not be read: {type(error).__name__}"
            ) from error
        return cls.from_bytes(raw)

    @classmethod
    def from_bytes(cls, raw: bytes) -> ModelCatalog:
        if type(raw) is not bytes or not 0 < len(raw) <= 256 * 1024:
            raise _manifest_error("model manifest has an invalid byte length")
        try:
            payload = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=lambda token: _reject_constant(token),
            )
        except AppError:
            raise
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
        ) as error:
            raise _manifest_error("model manifest is not valid strict JSON") from error
        if type(payload) is not dict:
            raise _manifest_error("model manifest root must be an object")
        _exact_keys(payload, _ROOT_FIELDS, "manifest root")
        if _strict_int(payload, "schema_version", minimum=1) != _SCHEMA_VERSION:
            raise _manifest_error("unsupported model manifest schema version")
        if _strict_string(payload, "rembg_version") != _PINNED_REMBG_VERSION:
            raise _manifest_error("model manifest rembg version is not the app pin")
        if _strict_string(payload, "default_id") != _DEFAULT_ID:
            raise _manifest_error("model manifest default is not the approved default")
        model_payloads = payload["models"]
        if type(model_payloads) is not list or len(model_payloads) != len(
            _APPROVED_IDS
        ):
            raise _manifest_error("model manifest must contain exactly 15 entries")
        specs = tuple(_parse_model(item) for item in model_payloads)
        ids = [spec.id for spec in specs]
        if len(set(ids)) != len(ids) or set(ids) != _APPROVED_IDS:
            raise _manifest_error(
                "model manifest IDs are duplicate, missing, or unknown"
            )
        return cls._build(
            rembg_version=_PINNED_REMBG_VERSION,
            default_id=_DEFAULT_ID,
            specs=specs,
        )

    def get(self, model_id: str) -> ModelSpec:
        if type(model_id) is not str:
            raise _model_not_found("model ID must be a string")
        spec = self.specs.get(model_id)
        if spec is None:
            raise _model_not_found(f"model ID {model_id!r} is not approved")
        return spec


def _parse_model(value: object) -> ModelSpec:
    if type(value) is not dict:
        raise _manifest_error("each model entry must be an object")
    _exact_keys(value, _MODEL_FIELDS, "model entry")
    model_id = _validate_model_id(value["id"])
    try:
        execution_class = ExecutionClass(_strict_string(value, "execution_class"))
    except ValueError as error:
        raise _manifest_error("model execution class is invalid") from error
    artifact_value = value["artifact"]
    artifact = (
        None if artifact_value is None else _parse_artifact(artifact_value, model_id)
    )
    inference_defaults = _parse_inference_defaults(
        value["inference_defaults"], model_id
    )
    required_inputs = _string_tuple(value, "required_inputs", allowed=None)
    edge_modes = _string_tuple(value, "edge_modes", allowed=_EDGE_MODES)
    if not edge_modes or len(set(edge_modes)) != len(edge_modes):
        raise _manifest_error("model edge modes must be unique and non-empty")
    spec = ModelSpec(
        id=model_id,
        display_name=cast(str, value["display_name"]),
        upstream_id=cast(str, value["upstream_id"]),
        purpose=cast(str, value["purpose"]),
        execution_class=execution_class,
        artifact=artifact,
        inference_defaults=inference_defaults,
        required_inputs=required_inputs,
        edge_modes=edge_modes,
        supports_render=cast(bool, value["supports_render"]),
        license_note=cast(str, value["license_note"]),
        privacy_note=cast(str, value["privacy_note"]),
        warning=cast(str, value["warning"]),
    )
    _validate_model_spec(spec)
    return spec


def _parse_inference_defaults(value: object, model_id: str) -> InferenceDefaults:
    if type(value) is not dict:
        raise _manifest_error("model inference defaults must be an object")
    expected = {"cloth_category"} if model_id == "u2net_cloth_seg" else set()
    _exact_keys(value, expected, "model inference defaults")
    if model_id != "u2net_cloth_seg":
        return InferenceDefaults()
    try:
        category = ClothCategory(_strict_string(value, "cloth_category"))
    except ValueError as error:
        raise _manifest_error("cloth category default is invalid") from error
    return InferenceDefaults(category)


def _parse_artifact(value: object, model_id: str) -> ModelArtifact:
    if type(value) is not dict:
        raise _manifest_error("model artifact must be an object or null")
    _exact_keys(value, _ARTIFACT_FIELDS, "model artifact")
    artifact = ModelArtifact(
        url=cast(str, value["url"]),
        runtime_filename=cast(str, value["runtime_filename"]),
        size_bytes=cast(int, value["size_bytes"]),
        sha256=cast(str, value["sha256"]),
        upstream_checksum=cast(str, value["upstream_checksum"]),
    )
    _validate_model_artifact(artifact, model_id)
    return artifact


def _validate_model_spec(spec: ModelSpec) -> None:
    if type(spec) is not ModelSpec:
        raise _manifest_error("catalog model specification has an invalid type")
    model_id = _validate_model_id(spec.id)
    _bounded_string(spec.display_name, "display_name")
    upstream_id = _bounded_string(spec.upstream_id, "upstream_id")
    if upstream_id != model_id:
        raise _manifest_error("model upstream ID must match the approved public ID")
    _bounded_string(spec.purpose, "purpose")
    if type(spec.execution_class) is not ExecutionClass:
        raise _manifest_error("model execution class is invalid")
    if spec.artifact is not None:
        _validate_model_artifact(spec.artifact, model_id)
    if type(spec.inference_defaults) is not InferenceDefaults:
        raise _manifest_error("model inference defaults have an invalid type")
    category = spec.inference_defaults.cloth_category
    if category is not None and type(category) is not ClothCategory:
        raise _manifest_error("cloth category default is invalid")
    _validate_string_tuple(spec.required_inputs, "required_inputs", allowed=None)
    _validate_string_tuple(spec.edge_modes, "edge_modes", allowed=_EDGE_MODES)
    if not spec.edge_modes:
        raise _manifest_error("model edge modes must be non-empty")
    if type(spec.supports_render) is not bool:
        raise _manifest_error("model supports_render must be a boolean")
    _bounded_string(spec.license_note, "license_note")
    _bounded_string(spec.privacy_note, "privacy_note")
    _bounded_string(spec.warning, "warning", allow_empty=True)
    _validate_model_invariants(spec)


def _validate_model_id(value: object) -> str:
    model_id = _bounded_string(value, "id")
    if _ID_PATTERN.fullmatch(model_id) is None or model_id not in _APPROVED_IDS:
        raise _manifest_error("model entry has an unknown or malformed ID")
    return model_id


def _validate_model_artifact(artifact: object, model_id: str) -> None:
    if type(artifact) is not ModelArtifact:
        raise _manifest_error("model artifact has an invalid type")
    url = _bounded_string(artifact.url, "url")
    try:
        split = urlsplit(url)
        port = split.port
    except ValueError as error:
        raise _manifest_error("model artifact URL is malformed") from error
    if (
        split.scheme != "https"
        or split.hostname != "github.com"
        or split.username is not None
        or split.password is not None
        or port is not None
        or split.query
        or split.fragment
        or not split.path.startswith(_RELEASE_PATH_PREFIX)
        or unquote(split.path) != split.path
        or "\\" in split.path
        or not split.path.endswith(".onnx")
        or "/" in split.path[len(_RELEASE_PATH_PREFIX) :]
    ):
        raise _manifest_error("model artifact URL is outside the pinned HTTPS release")
    runtime_filename = _bounded_string(artifact.runtime_filename, "runtime_filename")
    if (
        _FILENAME_PATTERN.fullmatch(runtime_filename) is None
        or runtime_filename != f"{model_id}.onnx"
    ):
        raise _manifest_error("model artifact runtime filename is unsafe or mismatched")
    size_bytes = artifact.size_bytes
    if (
        type(size_bytes) is not int
        or size_bytes < 1
        or size_bytes > 2 * 1024 * 1024 * 1024
    ):
        raise _manifest_error("model artifact size is outside the supported bound")
    sha256 = _bounded_string(artifact.sha256, "sha256")
    if _SHA256_PATTERN.fullmatch(sha256) is None:
        raise _manifest_error("model artifact SHA-256 is malformed")
    upstream_checksum = _bounded_string(artifact.upstream_checksum, "upstream_checksum")
    if _UPSTREAM_CHECKSUM_PATTERN.fullmatch(upstream_checksum) is None:
        raise _manifest_error(
            "model artifact upstream checksum provenance is malformed"
        )


def _validate_model_invariants(spec: ModelSpec) -> None:
    if (
        spec.execution_class is not ExecutionClass.LOCAL
        or spec.artifact is None
        or spec.required_inputs
        or not spec.supports_render
    ):
        raise _manifest_error("local model capability invariants are invalid")
    expected_defaults = (
        InferenceDefaults(ClothCategory.FULL)
        if spec.id == "u2net_cloth_seg"
        else InferenceDefaults()
    )
    if spec.inference_defaults != expected_defaults:
        raise _manifest_error("local model inference defaults are invalid")


def _string_tuple(
    payload: dict[str, object], key: str, *, allowed: frozenset[str] | None
) -> tuple[str, ...]:
    value = payload[key]
    if type(value) is not list or len(value) > 16:
        raise _manifest_error(f"{key} must be a bounded array")
    result = tuple(value)
    _validate_string_tuple(result, key, allowed=allowed)
    return cast(tuple[str, ...], result)


def _validate_string_tuple(
    value: object, key: str, *, allowed: frozenset[str] | None
) -> None:
    if type(value) is not tuple or len(value) > 16:
        raise _manifest_error(f"{key} must be a bounded tuple")
    for item in value:
        if type(item) is not str or not item or len(item) > 64:
            raise _manifest_error(f"{key} must contain bounded non-empty strings")
        if allowed is not None and item not in allowed:
            raise _manifest_error(f"{key} contains an unsupported value")
    if len(set(value)) != len(value):
        raise _manifest_error(f"{key} values must be unique")


def _strict_string(
    payload: dict[str, object], key: str, *, allow_empty: bool = False
) -> str:
    return _bounded_string(payload.get(key), key, allow_empty=allow_empty)


def _bounded_string(value: object, key: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or len(value) > 2048 or (not allow_empty and not value):
        raise _manifest_error(f"{key} must be a bounded string")
    return value


def _strict_int(payload: dict[str, object], key: str, *, minimum: int) -> int:
    value = payload.get(key)
    if type(value) is not int or value < minimum:
        raise _manifest_error(f"{key} must be an integer of at least {minimum}")
    return value


def _exact_keys(payload: dict[str, object], expected: set[str], label: str) -> None:
    if set(payload) != expected:
        raise _manifest_error(f"{label} has missing or unknown fields")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _manifest_error("model manifest contains duplicate object keys")
        result[key] = value
    return result


def _reject_constant(token: str) -> NoReturn:
    raise _manifest_error(f"model manifest contains invalid JSON constant {token}")


def _manifest_error(detail: str) -> AppError:
    return AppError(
        ErrorCode.MODEL_MANIFEST_INVALID,
        "model-manifest",
        "error.model.manifest-invalid",
        detail,
        "reinstall-application",
    )


def _model_not_found(detail: str) -> AppError:
    return AppError(
        ErrorCode.MODEL_NOT_FOUND,
        "model-selection",
        "error.model.not-found",
        detail,
        "choose-approved-model",
    )
