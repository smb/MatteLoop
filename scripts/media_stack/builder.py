"""Build, verify, cache, and archive MatteLoop's native media stack."""

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact_set import (
    artifact_set_path,
    create_artifact_set,
    validate_artifact_set,
)
from .compliance import create_compliance_archive
from .manifest import (
    MediaStackManifest,
    SourceSpec,
    load_manifest,
    media_stack_identity,
)
from .platforms import (
    BuildTarget,
    detect_target,
    ffmpeg_commands,
    libwebp_commands,
    pyav_build_command,
    repair_wheel_command,
)
from .sources import ensure_source, extract_source
from .verifier import provenance_path

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class MediaStackArtifacts:
    wheel: Path
    provenance: Path
    compliance_archive: Path
    report: Path
    artifact_set: Path
    identity: str


class MediaStackBuildError(RuntimeError):
    """A named media-stack command stage failed without promoting its outputs."""

    def __init__(
        self,
        stage: str,
        command: Sequence[str],
        returncode: int,
        staging_dir: Path,
        *,
        staging_retained: bool | None = None,
    ) -> None:
        self.stage = stage
        self.command = tuple(command)
        self.returncode = returncode
        self.staging_dir = staging_dir
        self.staging_retained = (
            staging_dir.is_dir() if staging_retained is None else staging_retained
        )
        rendered = " ".join(self.command)
        staging_status = (
            f"staging retained at {staging_dir}"
            if self.staging_retained
            else f"staging was not created at {staging_dir}"
        )
        super().__init__(
            f"stage {stage!r} failed with exit code {returncode}: {rendered}; "
            f"{staging_status}"
        )


@dataclass(slots=True)
class _BuildContext:
    root: Path
    manifest_path: Path
    manifest: MediaStackManifest
    target: BuildTarget
    identity: str
    identity_dir: Path
    staging: Path
    runner: CommandRunner
    tool_python: Path
    build_commands: list[tuple[str, ...]]


def ensure_media_stack(
    root: Path,
    cache_dir: Path,
    *,
    force: bool = False,
    runner: CommandRunner = subprocess.run,
) -> MediaStackArtifacts:
    """Return a freshly verified cached wheel and matching compliance material."""
    root = root.resolve()
    cache_dir = cache_dir.resolve()
    staging = _create_staging(cache_dir)
    manifest_path = root / "packaging" / "media-stack" / "manifest.toml"
    manifest = _load_build_manifest(manifest_path, staging)
    target = _detect_build_target(manifest, staging)
    identity = media_stack_identity(
        manifest_path,
        os_name=target.os_name,
        machine=target.machine,
        python_tag=target.python_tag,
        deployment_target=target.deployment_target,
    )
    identity_dir = cache_dir / identity
    context = _context(
        root, manifest_path, manifest, target, identity, identity_dir, staging, runner
    )
    _ensure_tool_environment(context)
    cached = _cached_artifacts(context)
    if cached is not None and not force:
        _reverify_cached(context, cached)
        shutil.rmtree(staging)
        return cached
    artifacts = _build_media_stack(context)
    shutil.rmtree(staging)
    return artifacts


def _create_staging(cache_dir: Path) -> Path:
    staging = cache_dir / f".staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir(parents=True)
    except OSError as error:
        raise MediaStackBuildError(
            "staging",
            ("mkdir", str(staging)),
            error.errno or 1,
            staging,
        ) from error
    return staging


def _context(
    root: Path,
    manifest_path: Path,
    manifest: MediaStackManifest,
    target: BuildTarget,
    identity: str,
    identity_dir: Path,
    staging: Path,
    runner: CommandRunner,
) -> _BuildContext:
    tool_environment = identity_dir / "tool-venv"
    tool_python = (
        tool_environment / "Scripts" / "python.exe"
        if target.target_id == "windows-x64"
        else tool_environment / "bin" / "python"
    )
    return _BuildContext(
        root,
        manifest_path,
        manifest,
        target,
        identity,
        identity_dir,
        staging,
        runner,
        tool_python,
        [],
    )


def _load_build_manifest(path: Path, failure_dir: Path) -> MediaStackManifest:
    try:
        return load_manifest(path)
    except (OSError, ValueError) as error:
        raise MediaStackBuildError(
            "manifest", ("load", str(path)), 1, failure_dir
        ) from error


def _detect_build_target(
    manifest: MediaStackManifest, failure_dir: Path
) -> BuildTarget:
    try:
        return detect_target(
            python_tag=manifest.python_abi,
            deployment_target=manifest.macos_deployment_target,
        )
    except ValueError as error:
        raise MediaStackBuildError(
            "target", ("detect", "host"), 1, failure_dir
        ) from error


def _ensure_tool_environment(context: _BuildContext) -> None:
    environment = context.tool_python.parents[1]
    marker = environment / ".matteloop-media-tools.json"
    requirements = _tool_requirements(context)
    expected = _canonical_json({"requirements": requirements})
    if context.tool_python.is_file() and marker.is_file():
        if marker.read_text(encoding="utf-8") == expected:
            return
    if environment.exists():
        shutil.rmtree(environment)
    _run_command(
        context,
        "tools",
        ("uv", "venv", "--python", sys.executable, str(environment)),
    )
    _run_command(
        context,
        "tools",
        ("uv", "pip", "install", "--python", str(context.tool_python), *requirements),
    )
    if not context.tool_python.is_file():
        raise MediaStackBuildError("tools", ("uv", "venv"), 1, context.staging)
    _atomic_write(marker, expected.encode())


def _tool_requirements(context: _BuildContext) -> tuple[str, ...]:
    tools = context.manifest.tools
    requirements = [
        f"build=={tools.build}",
        f"setuptools=={tools.setuptools}",
        f"Cython=={tools.cython}",
        f"wheel=={tools.wheel}",
    ]
    if context.target.target_id == "macos-arm64":
        requirements.append(f"delocate=={tools.delocate}")
    else:
        requirements.append(f"delvewheel=={tools.delvewheel}")
    return tuple(requirements)


def _cached_artifacts(context: _BuildContext) -> MediaStackArtifacts | None:
    finished = context.identity_dir / "finished"
    wheels = tuple(finished.glob("av-*.whl")) if finished.is_dir() else ()
    if len(wheels) != 1:
        return None
    wheel = wheels[0].resolve()
    artifacts = MediaStackArtifacts(
        wheel=wheel,
        provenance=provenance_path(wheel),
        compliance_archive=(
            finished
            / (
                f"MatteLoop-media-sources-{context.target.target_id}-"
                f"{context.identity}.tar.gz"
            )
        ).resolve(),
        report=(finished / "verification-report.json").resolve(),
        artifact_set=artifact_set_path(wheel),
        identity=context.identity,
    )
    paths = (
        artifacts.wheel,
        artifacts.provenance,
        artifacts.compliance_archive,
        artifacts.report,
        artifacts.artifact_set,
    )
    return artifacts if all(path.is_file() for path in paths) else None


def _reverify_cached(context: _BuildContext, artifacts: MediaStackArtifacts) -> None:
    try:
        validate_artifact_set(
            artifacts.artifact_set,
            artifacts.wheel,
            artifacts.provenance,
            artifacts.report,
            artifacts.compliance_archive,
            verified_report=_load_json_object(artifacts.report),
            target=context.target,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise MediaStackBuildError(
            "artifact-set",
            ("validate", str(artifacts.artifact_set)),
            1,
            context.staging,
        ) from error
    staged_report = context.staging / "verification-report.json"
    _verify(context, artifacts.wheel, staged_report)
    staged_binding = create_artifact_set(
        artifacts.wheel,
        artifacts.provenance,
        staged_report,
        artifacts.compliance_archive,
        identity=context.identity,
        manifest_sha256=_sha256(context.manifest_path),
        target=context.target,
        destination=context.staging / artifacts.artifact_set.name,
    )
    _promote_file(staged_report, artifacts.report)
    _promote_file(staged_binding, artifacts.artifact_set)


def _build_media_stack(context: _BuildContext) -> MediaStackArtifacts:
    try:
        archives, sources = _stage_sources(context, context.manifest.sources)
        tool_archives, tool_sources = _stage_sources(
            context, context.manifest.tool_sources
        )
        prefix = context.staging / "prefix"
        prefix.mkdir()
        _build_libraries(context, sources, prefix)
        wheel = _build_and_repair_wheel(context, sources["pyav"], prefix)
        sidecar = _write_provenance(context, wheel)
        report = context.staging / "verification-report.json"
        _verify(context, wheel, report)
        archive = _create_archive(
            context,
            archives,
            sources,
            tool_archives,
            tool_sources,
            sidecar,
            report,
        )
        binding = create_artifact_set(
            wheel,
            sidecar,
            report,
            archive,
            identity=context.identity,
            manifest_sha256=_sha256(context.manifest_path),
            target=context.target,
        )
        return _promote_build(context, wheel, sidecar, report, archive, binding)
    except MediaStackBuildError:
        raise
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise MediaStackBuildError(
            "compliance", ("create", "compliance-archive"), 1, context.staging
        ) from error


def _stage_sources(
    context: _BuildContext,
    specifications: Sequence[SourceSpec],
) -> tuple[dict[str, Path], dict[str, Path]]:
    archives: dict[str, Path] = {}
    extracted: dict[str, Path] = {}
    try:
        for source in specifications:
            archive = ensure_source(source, context.identity_dir / "sources")
            destination = context.staging / "sources" / source.name
            root = extract_source(archive, destination, source.archive_root)
            archives[source.name] = archive
            extracted[source.name] = root
    except (OSError, ValueError) as error:
        raise MediaStackBuildError(
            "sources", ("stage", "manifest-sources"), 1, context.staging
        ) from error
    return archives, extracted


def _build_libraries(
    context: _BuildContext, sources: Mapping[str, Path], prefix: Path
) -> None:
    webp_build = context.staging / "build" / "libwebp"
    ffmpeg_build = context.staging / "build" / "ffmpeg"
    ffmpeg_build.mkdir(parents=True)
    _run_commands(
        context,
        "libwebp",
        libwebp_commands(context.target, sources["libwebp"], webp_build, prefix),
    )
    _run_commands(
        context,
        "ffmpeg",
        ffmpeg_commands(context.target, sources["ffmpeg"], ffmpeg_build, prefix),
    )


def _build_and_repair_wheel(
    context: _BuildContext, pyav_source: Path, prefix: Path
) -> Path:
    raw_output = context.staging / "wheels"
    repaired_output = context.staging / "repaired"
    raw_output.mkdir()
    repaired_output.mkdir()
    command = pyav_build_command(
        context.target, pyav_source, prefix, raw_output, context.tool_python
    )
    _run_command(context, "pyav", command, record=True)
    raw_wheel = _one_wheel(raw_output, "pyav", context)
    repair = repair_wheel_command(
        context.target, raw_wheel, prefix, repaired_output, context.tool_python
    )
    repair_environment = None
    if context.target.target_id == "macos-arm64":
        repair = (str(context.tool_python), *repair)
        repair_environment = os.environ.copy()
        staged_libraries = str(prefix / "lib")
        existing_libraries = repair_environment.get("DYLD_LIBRARY_PATH")
        if existing_libraries:
            staged_libraries += os.pathsep + existing_libraries
        repair_environment["DYLD_LIBRARY_PATH"] = staged_libraries
        repair_environment["MACOSX_DEPLOYMENT_TARGET"] = (
            context.target.deployment_target
        )
    _run_command(
        context,
        "repair",
        repair,
        environment=repair_environment,
        record=True,
    )
    return _one_wheel(repaired_output, "repair", context)


def _one_wheel(directory: Path, stage: str, context: _BuildContext) -> Path:
    wheels = tuple(directory.glob("*.whl"))
    if len(wheels) != 1:
        raise MediaStackBuildError(
            stage, ("locate", str(directory)), 1, context.staging
        )
    return wheels[0]


def _write_provenance(context: _BuildContext, wheel: Path) -> Path:
    path = provenance_path(wheel)
    payload = {
        "identity": context.identity,
        "manifest_sha256": _sha256(context.manifest_path),
        "target_id": context.target.target_id,
        "python_tag": context.target.python_tag,
        "wheel_filename": wheel.name,
        "wheel_sha256": _sha256(wheel),
    }
    _atomic_write(path, _canonical_json(payload).encode())
    return path


def _verify(context: _BuildContext, wheel: Path, report: Path) -> None:
    command = (
        str(context.tool_python),
        str(context.root / "scripts" / "verify_media_stack.py"),
        str(wheel),
        "--manifest",
        str(context.manifest_path),
        "--report",
        str(report),
    )
    environment = os.environ.copy()
    tool_bin = str(context.tool_python.parent)
    environment["PATH"] = os.pathsep.join((tool_bin, environment.get("PATH", "")))
    _run_command(context, "verify", command, environment=environment)


def _create_archive(
    context: _BuildContext,
    archives: Mapping[str, Path],
    sources: Mapping[str, Path],
    tool_archives: Mapping[str, Path],
    tool_sources: Mapping[str, Path],
    sidecar: Path,
    report: Path,
) -> Path:
    return create_compliance_archive(
        context.staging / "compliance",
        target=context.target,
        identity=context.identity,
        source_archives=archives,
        tool_source_archives=tool_archives,
        manifest_path=context.manifest_path,
        provenance_path=sidecar,
        report_path=report,
        commands=_archive_commands(context),
        tool_versions=_effective_tool_versions(context),
        compiler_evidence=_compiler_evidence(context),
        licence_files=_licence_files(context, sources, tool_sources),
    )


def _archive_commands(context: _BuildContext) -> tuple[tuple[str, ...], ...]:
    replacements = (
        (str(context.staging), "${STAGING}"),
        (str(context.identity_dir), "${CACHE_IDENTITY}"),
        (str(context.root), "${REPOSITORY}"),
    )
    normalized: list[tuple[str, ...]] = []
    for command in context.build_commands:
        arguments = []
        for argument in command:
            for original, placeholder in replacements:
                argument = argument.replace(original, placeholder)
            arguments.append(argument)
        normalized.append(tuple(arguments))
    return tuple(normalized)


def _effective_tool_versions(context: _BuildContext) -> dict[str, str]:
    tools = context.manifest.tools
    versions = {
        "build": tools.build,
        "setuptools": tools.setuptools,
        "cython": tools.cython,
        "wheel": tools.wheel,
    }
    repair = "delocate" if context.target.target_id == "macos-arm64" else "delvewheel"
    versions[repair] = getattr(tools, repair)
    return versions


def _compiler_evidence(context: _BuildContext) -> str:
    compiler = _compiler_command(context.target)
    compiler_result = _run_command(context, "compiler", compiler)
    if not (compiler_result.stdout.strip() or compiler_result.stderr.strip()):
        raise MediaStackBuildError("compiler-evidence", compiler, 1, context.staging)
    cmake = ("cmake", "--version")
    cmake_result = _run_command(context, "cmake", cmake)
    return "".join(
        (
            f"Python: {platform.python_version()}\n",
            _render_probe_evidence(compiler, compiler_result),
            _render_probe_evidence(cmake, cmake_result),
        )
    )


def _render_probe_evidence(
    command: Sequence[str], completed: subprocess.CompletedProcess[str]
) -> str:
    evidence = [f"$ {shlex.join(command)}\n"]
    for stream, output in (("stdout", completed.stdout), ("stderr", completed.stderr)):
        if normalized := output.strip():
            evidence.append(f"{stream}:\n{normalized}\n")
    return "".join(evidence)


def _compiler_command(target: BuildTarget) -> tuple[str, ...]:
    if target.target_id == "windows-x64":
        return ("cl",)
    configured = shlex.split(os.environ.get("CC", "cc"))
    if not configured:
        configured = ["cc"]
    return (*configured, "--version")


def _licence_files(
    context: _BuildContext,
    sources: Mapping[str, Path],
    tool_sources: Mapping[str, Path],
) -> dict[str, tuple[Path, ...]]:
    licences = {
        "ffmpeg": _required_files(
            sources["ffmpeg"], ("COPYING.LGPLv2.1", "COPYING.LGPLv3")
        ),
        "libwebp": _required_files(sources["libwebp"], ("COPYING",)),
        "pyav": _first_required_files(sources["pyav"], ("LICENSE.txt", "LICENSE")),
        "cython": _required_files(
            tool_sources["cython"], ("COPYING.txt", "LICENSE.txt")
        ),
    }
    for package in _effective_tool_versions(context):
        if package == "cython":
            continue
        licences[package] = _tool_licences(context.tool_python, package)
    return licences


def _required_files(root: Path, names: Sequence[str]) -> tuple[Path, ...]:
    paths = tuple(root / name for name in names)
    if not all(path.is_file() for path in paths):
        raise ValueError(f"required licence file is missing below {root}")
    return paths


def _first_required_files(root: Path, names: Sequence[str]) -> tuple[Path, ...]:
    for name in names:
        path = root / name
        if path.is_file():
            return (path,)
    raise ValueError(f"required licence file is missing below {root}")


def _tool_licences(tool_python: Path, package: str) -> tuple[Path, ...]:
    environment = tool_python.parents[1]
    normalized = re.sub(r"[-_.]+", "-", package).casefold()
    for metadata in sorted(environment.rglob("*.dist-info")):
        distribution = metadata.name.removesuffix(".dist-info").rsplit("-", 1)[0]
        if re.sub(r"[-_.]+", "-", distribution).casefold() != normalized:
            continue
        candidates = tuple(
            sorted(
                path
                for path in metadata.rglob("*")
                if path.is_file()
                and (
                    "licenses" in path.parts
                    or path.name.casefold().startswith(("license", "copying", "notice"))
                )
            )
        )
        if candidates:
            return candidates
    raise ValueError(f"installed tool {package} has no licence file")


def _promote_build(
    context: _BuildContext,
    wheel: Path,
    sidecar: Path,
    report: Path,
    archive: Path,
    binding: Path,
) -> MediaStackArtifacts:
    finished = (context.identity_dir / "finished").resolve()
    candidate = context.identity_dir / f".finished-candidate-{uuid.uuid4().hex}"
    backup = context.identity_dir / f".finished-backup-{uuid.uuid4().hex}"
    sources = (wheel, sidecar, report, archive, binding)
    destinations = [finished / path.name for path in sources]
    try:
        candidate.mkdir()
        for source in sources:
            os.replace(source, candidate / source.name)
        _switch_finished_directory(context, candidate, finished, backup)
    except MediaStackBuildError:
        raise
    except OSError as error:
        raise MediaStackBuildError(
            "promote",
            ("replace", str(candidate), str(finished)),
            error.errno or 1,
            context.staging,
        ) from error
    return MediaStackArtifacts(
        wheel=destinations[0],
        provenance=destinations[1],
        report=destinations[2],
        compliance_archive=destinations[3],
        artifact_set=destinations[4],
        identity=context.identity,
    )


def _switch_finished_directory(
    context: _BuildContext, candidate: Path, finished: Path, backup: Path
) -> None:
    had_finished = finished.is_dir()
    if had_finished:
        os.replace(finished, backup)
    try:
        os.replace(candidate, finished)
    except OSError as error:
        if had_finished:
            os.replace(backup, finished)
        failed_candidate = context.staging / "failed-finished-candidate"
        try:
            os.replace(candidate, failed_candidate)
        except OSError:
            pass
        raise MediaStackBuildError(
            "promote",
            ("replace", str(candidate), str(finished)),
            error.errno or 1,
            context.staging,
        ) from error
    if had_finished:
        shutil.rmtree(backup)


def _promote_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)


def _run_commands(
    context: _BuildContext,
    stage: str,
    commands: Sequence[Sequence[str]],
) -> None:
    for command in commands:
        _run_command(context, stage, command, record=True)


def _run_command(
    context: _BuildContext,
    stage: str,
    command: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    record: bool = False,
) -> subprocess.CompletedProcess[str]:
    normalized = tuple(str(part) for part in command)
    if record:
        context.build_commands.append(normalized)
    kwargs: dict[str, Any] = {"check": False, "capture_output": True, "text": True}
    if environment is not None:
        kwargs["env"] = dict(environment)
    try:
        completed = context.runner(normalized, **kwargs)
    except OSError as error:
        raise MediaStackBuildError(stage, normalized, 127, context.staging) from error
    if completed.returncode != 0:
        raise MediaStackBuildError(
            stage, normalized, completed.returncode, context.staging
        )
    return completed


def _atomic_write(path: Path, contents: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open("xb") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Run the media-stack builder CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    cache = arguments.cache_dir or root / ".matteloop-build-cache" / "media-stack"
    try:
        artifacts = ensure_media_stack(root, cache, force=arguments.force)
    except MediaStackBuildError as error:
        print(f"Media stack build failed: {error}", file=sys.stderr)
        return 1
    payload = _artifact_payload(artifacts)
    if arguments.json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        print(f"Media stack identity: {artifacts.identity}")
        for name in (
            "wheel",
            "provenance",
            "compliance_archive",
            "report",
            "artifact_set",
        ):
            print(f"{name}: {payload[name]}")
    return 0


def _artifact_payload(artifacts: MediaStackArtifacts) -> dict[str, str]:
    return {
        "artifact_set": str(artifacts.artifact_set.resolve()),
        "compliance_archive": str(artifacts.compliance_archive.resolve()),
        "identity": artifacts.identity,
        "provenance": str(artifacts.provenance.resolve()),
        "report": str(artifacts.report.resolve()),
        "wheel": str(artifacts.wheel.resolve()),
    }


def _load_json_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return payload
