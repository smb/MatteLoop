"""Deterministic LGPL source-compliance archive creation."""

import gzip
import hashlib
import json
import os
import shlex
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from io import BytesIO
from pathlib import Path
from typing import Any

from .platforms import BuildTarget

_SOURCE_NAMES = frozenset(("ffmpeg", "libwebp", "pyav"))
_TOOL_SOURCE_NAMES = frozenset(("cython",))
_COMMON_LICENCES = frozenset(
    ("ffmpeg", "libwebp", "pyav", "build", "setuptools", "cython", "wheel")
)


def create_compliance_archive(
    output_dir: Path,
    *,
    target: BuildTarget,
    identity: str,
    source_archives: Mapping[str, Path],
    tool_source_archives: Mapping[str, Path],
    manifest_path: Path,
    provenance_path: Path,
    report_path: Path,
    commands: Sequence[Sequence[str]],
    tool_versions: Mapping[str, str],
    compiler_evidence: str,
    licence_files: Mapping[str, Sequence[Path]],
) -> Path:
    """Create and atomically publish one target-specific compliance archive."""
    _validate_inputs(target, source_archives, tool_source_archives, licence_files)
    members = _archive_members(
        target=target,
        identity=identity,
        source_archives=source_archives,
        tool_source_archives=tool_source_archives,
        manifest_path=manifest_path,
        provenance_path=provenance_path,
        report_path=report_path,
        commands=commands,
        tool_versions=tool_versions,
        compiler_evidence=compiler_evidence,
        licence_files=licence_files,
    )
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / (
        f"MatteLoop-media-sources-{target.target_id}-{identity}.tar.gz"
    )
    _publish_archive(destination, members)
    return destination


def _validate_inputs(
    target: BuildTarget,
    sources: Mapping[str, Path],
    tool_sources: Mapping[str, Path],
    licences: Mapping[str, Sequence[Path]],
) -> None:
    if set(sources) != _SOURCE_NAMES:
        raise ValueError("compliance sources must be exactly FFmpeg, libwebp, and PyAV")
    if set(tool_sources) != _TOOL_SOURCE_NAMES:
        raise ValueError("compliance tool sources must contain exactly Cython")
    repair_tool = "delocate" if target.target_id == "macos-arm64" else "delvewheel"
    missing = sorted((_COMMON_LICENCES | {repair_tool}) - set(licences))
    if missing:
        raise ValueError(f"missing compliance licence files: {', '.join(missing)}")
    if any(not files for files in licences.values()):
        raise ValueError("each compliance component must provide a licence file")


def _archive_members(**values: Any) -> dict[str, bytes]:
    sources: Mapping[str, Path] = values["source_archives"]
    tool_sources: Mapping[str, Path] = values["tool_source_archives"]
    licences: Mapping[str, Sequence[Path]] = values["licence_files"]
    report_path: Path = values["report_path"]
    members = {f"sources/{path.name}": path.read_bytes() for path in sources.values()}
    members.update(
        {
            f"tool-sources/{path.name}": path.read_bytes()
            for path in tool_sources.values()
        }
    )
    members.update(
        {
            "manifest.toml": values["manifest_path"].read_bytes(),
            "changes.diff": b"",
            values["provenance_path"].name: values["provenance_path"].read_bytes(),
            "verification-report.json": report_path.read_bytes(),
            "dependency-inventory.txt": _dependency_inventory(report_path),
            "build/commands.txt": _command_evidence(values["commands"]),
            "build/tool-versions.json": _json_bytes(values["tool_versions"]),
            "build/target.json": _target_bytes(values["target"]),
            "build/compiler-versions.txt": values["compiler_evidence"].encode(),
            "source-checksums.json": _source_checksums({**sources, **tool_sources}),
            "REBUILD.md": _rebuild_instructions(values["target"], values["identity"]),
        }
    )
    for component, files in licences.items():
        for path in files:
            members[f"licences/{component}/{path.name}"] = path.read_bytes()
    return members


def _dependency_inventory(report_path: Path) -> bytes:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    try:
        dependencies = report["evidence"]["dependencies"]
    except (KeyError, TypeError) as error:
        raise ValueError("verifier report has no dependency inventory") from error
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) for item in dependencies
    ):
        raise ValueError("verifier dependency inventory must be a string array")
    return ("".join(f"{item}\n" for item in dependencies)).encode()


def _command_evidence(commands: Sequence[Sequence[str]]) -> bytes:
    return (
        "".join(f"$ {shlex.join(tuple(command))}\n" for command in commands)
    ).encode()


def _target_bytes(target: BuildTarget) -> bytes:
    return _json_bytes(
        {
            "deployment_target": target.deployment_target,
            "machine": target.machine,
            "os_name": target.os_name,
            "python_tag": target.python_tag,
            "target_id": target.target_id,
        }
    )


def _source_checksums(sources: Mapping[str, Path]) -> bytes:
    return _json_bytes(
        {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in sources.items()
        }
    )


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _rebuild_instructions(target: BuildTarget, identity: str) -> bytes:
    return f"""# Rebuild MatteLoop media stack

This archive records target `{target.target_id}` and identity `{identity}`.
Install Xcode command-line tools on macOS arm64, or Visual Studio 2022 and
MSYS2 on Windows x64. From the matching MatteLoop checkout, place these exact
source archives in the media-stack source cache and run:

    python scripts/build_media_stack.py --force

The checked-in manifest verifies every source digest and pins all Python build
and wheel-repair tools. The verifier must pass before any wheel is published.
""".encode()


def _publish_archive(destination: Path, members: Mapping[str, bytes]) -> None:
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            with gzip.GzipFile(
                fileobj=output, mode="wb", filename="", mtime=0
            ) as zipped:
                with tarfile.open(fileobj=zipped, mode="w") as archive:
                    for name in sorted(members):
                        _add_member(archive, name, members[name])
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _add_member(archive: tarfile.TarFile, name: str, contents: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(contents)
    member.mtime = 0
    member.mode = 0o644
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    archive.addfile(member, BytesIO(contents))
