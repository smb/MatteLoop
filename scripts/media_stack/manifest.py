"""Typed loading and identity calculation for the media-stack manifest."""

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_SUPPORTED_TARGETS = ("macos-arm64", "windows-x64")
_SOURCE_NAMES = frozenset(("ffmpeg", "libwebp", "pyav"))
_TOOL_SOURCE_NAMES = frozenset(("cython",))
_TOOL_NAMES = frozenset(
    ("build", "setuptools", "cython", "wheel", "delocate", "delvewheel")
)
_SOURCE_FIELDS = frozenset(("name", "version", "url", "sha256", "archive_root"))
_VERIFICATION_FIELDS = frozenset(
    (
        "required_codecs",
        "required_formats",
        "forbidden_tokens",
        "forbidden_library_fragments",
    )
)
_FLOATING_TOKEN = re.compile(
    r"(?:^|[-_.])(?:current|head|latest|main|master|nightly|snapshot|stable|trunk)(?:$|[-_.])",
    re.IGNORECASE,
)
_EXACT_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)+")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class SourceSpec:
    name: str
    version: str
    url: str
    sha256: str
    archive_root: str


@dataclass(frozen=True, slots=True)
class ToolVersions:
    build: str
    setuptools: str
    cython: str
    wheel: str
    delocate: str
    delvewheel: str


@dataclass(frozen=True, slots=True)
class VerificationContract:
    required_codecs: tuple[str, ...]
    required_formats: tuple[str, ...]
    forbidden_tokens: tuple[str, ...]
    forbidden_library_fragments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MediaStackManifest:
    schema_version: int
    targets: tuple[str, ...]
    python_abi: str
    macos_deployment_target: str
    sources: tuple[SourceSpec, ...]
    tool_sources: tuple[SourceSpec, ...]
    tools: ToolVersions
    verification: VerificationContract


def load_manifest(path: Path) -> MediaStackManifest:
    """Load and validate a checked-in media-stack TOML manifest."""
    with path.open("rb") as manifest_file:
        raw = tomllib.load(manifest_file)
    _require_keys(
        raw,
        {
            "schema_version",
            "targets",
            "python_abi",
            "macos_deployment_target",
            "sources",
            "tool_sources",
            "tools",
            "verification",
        },
        "manifest",
    )
    schema_version = _require_int(raw["schema_version"], "schema_version")
    if schema_version != 1:
        raise ValueError("unsupported manifest schema_version")
    targets = _require_strings(raw["targets"], "targets")
    if targets != _SUPPORTED_TARGETS:
        raise ValueError(f"unsupported target set: {targets!r}")
    python_abi = _require_string(raw["python_abi"], "python_abi")
    if python_abi != "cp313":
        raise ValueError(f"unsupported python_abi: {python_abi!r}")
    deployment_target = _require_pinned_version(
        raw["macos_deployment_target"], "macos_deployment_target"
    )
    sources = _load_sources(raw["sources"], _SOURCE_NAMES, "sources")
    tool_sources = _load_sources(
        raw["tool_sources"], _TOOL_SOURCE_NAMES, "tool_sources"
    )
    tools = _load_tools(raw["tools"])
    if tool_sources[0].version != tools.cython:
        raise ValueError("tool_sources.cython version must match tools.cython")
    verification = _load_verification(raw["verification"])
    return MediaStackManifest(
        schema_version=schema_version,
        targets=targets,
        python_abi=python_abi,
        macos_deployment_target=deployment_target,
        sources=sources,
        tool_sources=tool_sources,
        tools=tools,
        verification=verification,
    )


def media_stack_identity(
    manifest_path: Path,
    *,
    os_name: str,
    machine: str,
    python_tag: str,
    deployment_target: str,
    builder_revision: int = 1,
) -> str:
    """Return the short cache identity for one manifest and build contract."""
    payload = (
        manifest_path.read_bytes()
        + b"\0"
        + json.dumps(
            {
                "builder_revision": builder_revision,
                "deployment_target": deployment_target,
                "machine": machine.lower(),
                "os_name": os_name,
                "python_tag": python_tag,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return hashlib.sha256(payload).hexdigest()[:24]


def _load_sources(
    value: Any, expected_names: frozenset[str], context: str
) -> tuple[SourceSpec, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be an array of tables")
    sources: list[SourceSpec] = []
    for index, raw_source in enumerate(value):
        item = f"{context}[{index}]"
        source = _require_mapping(raw_source, item)
        _require_keys(source, _SOURCE_FIELDS, item)
        name = _require_string(source["name"], f"{item}.name")
        version = _require_pinned_version(source["version"], f"{item}.version")
        url = _require_url(source["url"], version, f"{item}.url")
        sha256 = _require_string(source["sha256"], f"{item}.sha256")
        if not _SHA256.fullmatch(sha256):
            raise ValueError(
                f"{name} sha256 must be 64 lowercase hexadecimal characters"
            )
        archive_root = _require_string(source["archive_root"], f"{item}.archive_root")
        if _FLOATING_TOKEN.search(archive_root):
            raise ValueError(f"{name} archive_root must be pinned")
        sources.append(SourceSpec(name, version, url, sha256, archive_root))
    names = tuple(source.name for source in sources)
    if len(names) != len(set(names)) or set(names) != expected_names:
        source_kind = "source" if context == "sources" else "tool source"
        raise ValueError(
            f"{source_kind} names must be exactly one each of {expected_names!r}"
        )
    return tuple(sources)


def _load_tools(value: Any) -> ToolVersions:
    tools = _require_mapping(value, "tools")
    _require_keys(tools, _TOOL_NAMES, "tools")
    versions = {
        name: _require_pinned_version(tools[name], f"tools.{name}")
        for name in _TOOL_NAMES
    }
    return ToolVersions(**versions)


def _load_verification(value: Any) -> VerificationContract:
    verification = _require_mapping(value, "verification")
    _require_keys(verification, _VERIFICATION_FIELDS, "verification")
    return VerificationContract(
        required_codecs=_require_strings(
            verification["required_codecs"], "verification.required_codecs"
        ),
        required_formats=_require_strings(
            verification["required_formats"], "verification.required_formats"
        ),
        forbidden_tokens=_require_strings(
            verification["forbidden_tokens"], "verification.forbidden_tokens"
        ),
        forbidden_library_fragments=_require_strings(
            verification["forbidden_library_fragments"],
            "verification.forbidden_library_fragments",
        ),
    )


def _require_keys(
    value: dict[str, Any], expected: set[str] | frozenset[str], context: str
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing={missing!r}")
        if unknown:
            details.append(f"unknown={unknown!r}")
        detail = ", ".join(details)
        raise ValueError(f"{context} has invalid keys ({detail})")


def _require_mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{context} must be a table")
    return value


def _require_int(value: Any, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{context} must be an integer")
    return value


def _require_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} must be a non-empty string")
    return value


def _require_strings(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be an array of strings")
    return tuple(
        _require_string(item, f"{context}[{index}]") for index, item in enumerate(value)
    )


def _require_pinned_version(value: Any, context: str) -> str:
    version = _require_string(value, context)
    if not _EXACT_VERSION.fullmatch(version):
        raise ValueError(f"{context} must be an exact pinned version")
    return version


def _require_url(value: Any, version: str, context: str) -> str:
    url = _require_string(value, context)
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc or not parts.path:
        raise ValueError(f"{context} must be an HTTPS URL")
    if parts.query or parts.fragment or _FLOATING_TOKEN.search(url):
        raise ValueError(f"{context} must be pinned")
    if version not in parts.path:
        raise ValueError(f"{context} must include the pinned version")
    return url
