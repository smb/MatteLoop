#!/usr/bin/env python3
"""Build the unsigned native bundle for the current supported platform."""

from __future__ import annotations

import argparse
import configparser
import contextlib
import hashlib
import importlib.metadata
import os
import platform as platform_module
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from zipfile import BadZipFile, ZipFile, ZipInfo

if __package__:
    from scripts.media_stack.artifact_set import (
        artifact_set_path,
        validate_artifact_set,
    )
    from scripts.media_stack.builder import MediaStackArtifacts, ensure_media_stack
    from scripts.media_stack.manifest import VerificationContract, load_manifest
    from scripts.media_stack.platforms import BuildTarget, detect_target
    from scripts.media_stack.verifier import (
        VerificationReport,
        forbidden_bundle_entries,
        provenance_path,
        verify_media_wheel,
    )
else:
    from media_stack.artifact_set import artifact_set_path, validate_artifact_set
    from media_stack.builder import MediaStackArtifacts, ensure_media_stack
    from media_stack.manifest import VerificationContract, load_manifest
    from media_stack.platforms import BuildTarget, detect_target
    from media_stack.verifier import (
        VerificationReport,
        forbidden_bundle_entries,
        provenance_path,
        verify_media_wheel,
    )

ROOT = Path(__file__).resolve().parent.parent
DIST_PATH = ROOT / "dist"

_PINNED_DISTRIBUTIONS = {
    "PySide6": "6.10.x",
    "Nuitka": "2.8.10",
    "onnxruntime": "1.29.0",
}

_PLATFORM_ICONS = {
    "darwin": "assets/branding/matteloop/derived/matteloop.icns",
    "win32": "assets/branding/matteloop/derived/matteloop.ico",
}
_CORRECTED_BRANDING_ASSETS = (
    "assets/branding/matteloop/matteloop-app-icon-1024-alpha-green.png",
    "assets/branding/matteloop/matteloop-ui-mark-1024-alpha-green.png",
)
_MEDIA_MANIFEST = Path("packaging/media-stack/manifest.toml")
_MEDIA_CACHE = Path(".matteloop-build-cache/media-stack")

MediaStackEnsurer = Callable[..., MediaStackArtifacts]
MediaWheelVerifier = Callable[..., VerificationReport]


@dataclass(frozen=True, slots=True)
class PreparedMediaStack:
    av_directory: Path
    compliance_archive: Path
    target: BuildTarget
    contract: VerificationContract
    identity: str


@dataclass(frozen=True, slots=True)
class PreparedComplianceEvidence:
    archive_temporary: Path
    checksum_temporary: Path
    archive: Path
    checksum: Path


def deploy_executable(
    *, python_executable: Path = Path(sys.executable), os_name: str = sys.platform
) -> Path:
    """Return the pyside6-deploy executable belonging to one Python install."""
    filename = "pyside6-deploy.exe" if os_name == "win32" else "pyside6-deploy"
    return python_executable.with_name(filename)


def prerequisite_errors(
    *,
    os_name: str = sys.platform,
    machine: str = platform_module.machine(),
    python_version: tuple[int, int] = sys.version_info[:2],
    deploy_path: Path | None = None,
    installed_versions: Mapping[str, str | None] | None = None,
) -> tuple[str, ...]:
    """Return readable errors for platform, interpreter, and toolchain checks."""
    errors: list[str] = []
    if os_name not in {"darwin", "win32"}:
        errors.append(
            "Linux and other platforms are deferred; native packaging supports "
            "macOS arm64 and Windows x64 only."
        )
    elif os_name == "darwin" and machine.lower() not in {"arm64", "aarch64"}:
        errors.append(
            f"macOS packaging requires arm64, but this machine reports {machine!r}."
        )
    elif os_name == "win32" and machine.lower() not in {"amd64", "x86_64"}:
        errors.append(
            f"Windows packaging requires x64, but this machine reports {machine!r}."
        )

    if python_version != (3, 13):
        errors.append(
            "CPython 3.13 is required; "
            f"this interpreter is {python_version[0]}.{python_version[1]}."
        )

    checked_deploy_path = deploy_path or deploy_executable()
    if not checked_deploy_path.is_file():
        errors.append(
            f"Missing build prerequisite: {checked_deploy_path.name}. "
            "Run `uv sync --all-groups` from the project root."
        )

    versions = installed_versions or _installed_versions()
    for distribution, expected in _PINNED_DISTRIBUTIONS.items():
        actual = versions.get(distribution)
        if actual is None:
            errors.append(
                f"Missing build prerequisite: {distribution}. "
                "Run `uv sync --all-groups` from the project root."
            )
        elif not _version_matches(distribution, actual):
            errors.append(
                f"{distribution} {actual} is installed, but the build requires "
                f"{distribution} {expected}. Run `uv sync --all-groups`."
            )
    return tuple(errors)


def packaging_input_errors(root: Path = ROOT) -> tuple[str, ...]:
    """Return errors for missing files named by the native packaging spec."""
    errors = list(branding_input_errors(root))
    entrypoint_path = root / "packaging" / "entrypoint.py"
    spec_path = root / "packaging" / "pysidedeploy.spec"
    for path in (entrypoint_path, spec_path):
        if not path.is_file():
            errors.append(f"Missing packaging input: {path.relative_to(root)}.")

    if not spec_path.is_file():
        return tuple(errors)

    try:
        sources = _spec_data_sources(spec_path)
    except (configparser.Error, KeyError, ValueError) as error:
        errors.append(f"Could not read {spec_path.relative_to(root)}: {error}.")
        return tuple(errors)

    for source in sources:
        path = root / source
        if not path.is_file():
            errors.append(f"Spec data file is missing: {source}.")
    return tuple(errors)


def branding_input_errors(root: Path = ROOT) -> tuple[str, ...]:
    """Return errors for the corrected masters and committed native icons."""
    errors: list[str] = []
    assets = (*_CORRECTED_BRANDING_ASSETS, *_PLATFORM_ICONS.values())
    for relative in assets:
        path = root / relative
        if not path.is_file():
            errors.append(f"Missing branding asset: {relative}.")
            continue
        if path.suffix == ".png" and path.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            errors.append(f"Branding asset is not a PNG: {relative}.")
        elif path.suffix == ".ico" and path.read_bytes()[:4] != b"\x00\x00\x01\x00":
            errors.append(f"Branding asset is not an ICO: {relative}.")
        elif path.suffix == ".icns" and path.read_bytes()[:4] != b"icns":
            errors.append(f"Branding asset is not an ICNS: {relative}.")
    return tuple(errors)


def build_command(
    deploy_path: Path, spec_path: Path = Path("packaging/pysidedeploy.spec")
) -> list[str]:
    """Return the reproducible native build command."""
    return [
        str(deploy_path),
        "-c",
        str(spec_path),
        "--force",
    ]


def prepare_temporary_spec(
    source_spec: Path,
    destination_spec: Path,
    av_directory: Path,
    *,
    os_name: str = sys.platform,
) -> None:
    """Select the platform icon and add raw PyAV files to a temporary spec."""
    try:
        icon = _PLATFORM_ICONS[os_name]
    except KeyError as error:
        raise ValueError(f"unsupported native packaging platform: {os_name}") from error
    content = source_spec.read_text(encoding="utf-8")
    mac_icon = _PLATFORM_ICONS["darwin"]
    content = content.replace(f"icon = {mac_icon}", f"icon = {icon}", 1)
    marker = "\t--nofollow-import-to=av\n"
    if marker not in content:
        raise ValueError("packaging spec must contain the PyAV nofollow marker")
    wheel_file_args = "\n".join(
        f"\t--include-data-files={native_file}=av/"
        f"{native_file.relative_to(av_directory).as_posix()}"
        for native_file in sorted(
            path
            for path in av_directory.rglob("*")
            if path.is_file()
            and path.suffix in {".dll", ".dylib", ".pyd", ".py", ".so"}
        )
    )
    if not wheel_file_args:
        raise ValueError(f"no PyAV wheel files found in {av_directory}")
    content = content.replace(
        marker,
        f"{marker}\t--include-data-dir={av_directory}=av\n"
        f"{wheel_file_args}\n",
        1,
    )
    destination_spec.write_text(content, encoding="utf-8")


def extract_wheel_package(wheel: Path, destination: Path) -> Path:
    """Extract one native top-level PyAV package from a verified wheel."""
    try:
        with ZipFile(wheel) as archive:
            members = _preflight_wheel_members(archive.infolist())
            _extract_wheel_members(archive, members, destination)
    except (BadZipFile, OSError) as error:
        raise ValueError(f"could not extract media wheel {wheel}: {error}") from error
    expected = destination / "av"
    if not (expected / "__init__.py").is_file():
        raise ValueError("media wheel must contain exactly one top-level av package")
    if not any(
        path.is_file() and path.suffix.casefold() in {".so", ".pyd"}
        for path in expected.rglob("*")
    ):
        raise ValueError("media wheel av package must contain a native .so or .pyd")
    return expected


def _preflight_wheel_members(
    entries: list[ZipInfo],
) -> tuple[tuple[ZipInfo, tuple[str, ...]], ...]:
    members: list[tuple[ZipInfo, tuple[str, ...]]] = []
    seen: set[str] = set()
    packages: set[tuple[str, ...]] = set()
    for entry in entries:
        normalized = entry.filename.replace("\\", "/")
        if entry.is_dir():
            normalized = normalized[:-1]
        parts = tuple(normalized.split("/"))
        if _unsafe_wheel_path(normalized, parts):
            raise ValueError(f"unsafe wheel member path: {entry.filename}")
        kind = stat.S_IFMT(entry.external_attr >> 16)
        if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise ValueError(f"unsafe wheel member type: {entry.filename}")
        key = "/".join(parts).casefold()
        if key in seen:
            raise ValueError(f"duplicate wheel member path: {entry.filename}")
        seen.add(key)
        members.append((entry, parts))
        if len(parts) >= 2 and parts[-2:] == ("av", "__init__.py"):
            packages.add(parts[:-1])
    if packages != {("av",)}:
        raise ValueError("media wheel must contain exactly one top-level av package")
    return tuple(members)


def _unsafe_wheel_path(name: str, parts: tuple[str, ...]) -> bool:
    return (
        not name
        or name.startswith("/")
        or bool(PureWindowsPath(name).drive)
        or any(part in {"", ".", ".."} for part in parts)
    )


def _extract_wheel_members(
    archive: ZipFile,
    members: tuple[tuple[ZipInfo, tuple[str, ...]], ...],
    destination: Path,
) -> None:
    destination.mkdir(parents=True)
    for entry, parts in members:
        target = destination.joinpath(*parts)
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(entry) as source, target.open("xb") as output:
            shutil.copyfileobj(source, output)


def prepare_media_stack(
    destination: Path,
    *,
    root: Path = ROOT,
    media_wheel: Path | None = None,
    rebuild: bool = False,
    target: BuildTarget | None = None,
    ensure: MediaStackEnsurer = ensure_media_stack,
    verify: MediaWheelVerifier = verify_media_wheel,
) -> PreparedMediaStack:
    """Select only verified media artifacts and extract their PyAV package."""
    manifest_path = root / _MEDIA_MANIFEST
    manifest = load_manifest(manifest_path)
    resolved_target = target or detect_target(
        python_tag=manifest.python_abi,
        deployment_target=manifest.macos_deployment_target,
    )
    if media_wheel is None:
        artifacts = ensure(root, root / _MEDIA_CACHE, force=rebuild)
    else:
        artifacts = _verified_explicit_artifacts(
            media_wheel, manifest_path, resolved_target, verify
        )
    av_directory = extract_wheel_package(artifacts.wheel, destination)
    return PreparedMediaStack(
        av_directory,
        artifacts.compliance_archive,
        resolved_target,
        manifest.verification,
        artifacts.identity,
    )


def bundle_media_errors(
    artifact: Path, target: BuildTarget, contract: VerificationContract
) -> tuple[str, ...]:
    """Return every forbidden media-library finding in a native bundle."""
    return tuple(
        f"{target.target_id} bundle contains forbidden media entry: "
        f"{entry.relative_to(artifact)}"
        for entry in forbidden_bundle_entries(
            artifact, contract.forbidden_library_fragments
        )
    )


def prepare_compliance_evidence(
    archive: Path, destination: Path, target: BuildTarget, identity: str
) -> PreparedComplianceEvidence:
    """Prepare an unpublished compliance pair beside the native artifact."""
    expected_name = f"MatteLoop-media-sources-{target.target_id}-{identity}.tar.gz"
    if archive.name != expected_name:
        raise ValueError(
            "media compliance archive must be named "
            f"{expected_name}, got {archive.name}"
        )
    destination.mkdir(parents=True, exist_ok=True)
    published = destination / expected_name
    checksum = published.with_name(f"{published.name}.sha256")
    token = uuid.uuid4().hex
    archive_temporary = destination / f".{published.name}.{token}.tmp"
    checksum_temporary = destination / f".{checksum.name}.{token}.tmp"
    try:
        _copy_fsynced(archive, archive_temporary)
        _write_fsynced(
            checksum_temporary,
            f"{_sha256(archive_temporary)}  {published.name}\n".encode(),
        )
    except OSError:
        archive_temporary.unlink(missing_ok=True)
        checksum_temporary.unlink(missing_ok=True)
        raise
    return PreparedComplianceEvidence(
        archive_temporary, checksum_temporary, published, checksum
    )


def publish_compliance_evidence(evidence: PreparedComplianceEvidence) -> None:
    """Replace a compliance pair, restoring prior bytes on partial failure."""
    token = uuid.uuid4().hex
    pairs = (
        (evidence.archive_temporary, evidence.archive),
        (evidence.checksum_temporary, evidence.checksum),
    )
    backups = tuple(
        final.with_name(f".{final.name}.{token}.bak") for _temporary, final in pairs
    )
    moved: list[tuple[Path, Path]] = []
    published: list[Path] = []
    try:
        for (_temporary, final), backup in zip(pairs, backups, strict=True):
            if final.exists():
                os.replace(final, backup)
                moved.append((final, backup))
        for temporary, final in pairs:
            os.replace(temporary, final)
            published.append(final)
    except OSError:
        for final in reversed(published):
            final.unlink(missing_ok=True)
        for final, backup in reversed(moved):
            os.replace(backup, final)
        _discard_compliance_evidence(evidence, backups)
        raise
    for backup in backups:
        backup.unlink(missing_ok=True)


def _discard_compliance_evidence(
    evidence: PreparedComplianceEvidence, backups: tuple[Path, ...] = ()
) -> None:
    for path in (
        evidence.archive_temporary,
        evidence.checksum_temporary,
        *backups,
    ):
        path.unlink(missing_ok=True)


def expected_artifact(os_name: str, dist_path: Path = DIST_PATH) -> Path:
    """Return the standalone bundle directory expected from pyside6-deploy."""
    if os_name == "darwin":
        return dist_path / "MatteLoop.app"
    if os_name == "win32":
        return dist_path / "MatteLoop.dist"
    raise ValueError(f"unsupported native packaging platform: {os_name}")


def artifact_size_bytes(path: Path) -> int:
    """Return the byte size of a native bundle directory or file."""
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    raise FileNotFoundError(path)


def remove_previous_artifact(os_name: str, dist_path: Path = DIST_PATH) -> None:
    """Remove only the current platform's generated bundle before rebuilding."""
    artifact = expected_artifact(os_name, dist_path)
    if artifact.is_dir():
        shutil.rmtree(artifact)
    elif artifact.exists():
        artifact.unlink()


@contextlib.contextmanager
def temporary_onnxruntime_dylib_alias(
    *, os_name: str = sys.platform, capi_directory: Path | None = None
) -> Iterator[None]:
    """Bridge the missing macOS SONAME alias in the pinned ONNX Runtime wheel."""
    alias: Path | None = None
    if os_name == "darwin":
        directory = capi_directory or _onnxruntime_capi_directory()
        alias = _create_onnxruntime_dylib_alias(directory)
    try:
        yield
    finally:
        if alias is not None:
            alias.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--rebuild-media-stack", action="store_true")
    selection.add_argument("--media-wheel", type=Path)
    arguments = parser.parse_args(argv)

    errors = [*prerequisite_errors(), *packaging_input_errors()]
    if errors:
        print("Native build prerequisites are not satisfied:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    return _run_native_build(arguments.media_wheel, arguments.rebuild_media_stack)


def _run_native_build(media_wheel: Path | None, rebuild_media_stack: bool) -> int:
    deploy_path = deploy_executable()
    artifact = expected_artifact(sys.platform)
    print("Building unsigned native bundle with pyside6-deploy…", flush=True)
    try:
        remove_previous_artifact(sys.platform)
        with temporary_onnxruntime_dylib_alias():
            with tempfile.TemporaryDirectory(
                prefix=".matteloop-build-", dir=ROOT
            ) as raw:
                build_directory = Path(raw)
                prepared = prepare_media_stack(
                    build_directory / "media-wheel",
                    root=ROOT,
                    media_wheel=media_wheel,
                    rebuild=rebuild_media_stack,
                )
                temporary_spec = build_directory / "pysidedeploy.spec"
                prepare_temporary_spec(
                    ROOT / "packaging" / "pysidedeploy.spec",
                    temporary_spec,
                    prepared.av_directory,
                    os_name=sys.platform,
                )
                completed = subprocess.run(
                    build_command(deploy_path, temporary_spec), cwd=ROOT, check=False
                )
    except (OSError, RuntimeError, ValueError) as error:
        print(
            f"Native build preparation or launch failed: {error}",
            file=sys.stderr,
        )
        return 1
    return _finish_native_build(completed, artifact, prepared)


def _finish_native_build(
    completed: subprocess.CompletedProcess[bytes],
    artifact: Path,
    prepared: PreparedMediaStack,
) -> int:
    if completed.returncode != 0:
        print(
            f"Native build failed with exit status {completed.returncode}.",
            file=sys.stderr,
        )
        return completed.returncode or 1

    try:
        size = artifact_size_bytes(artifact)
    except FileNotFoundError:
        print(
            f"Native build reported success but produced no bundle at {artifact}.",
            file=sys.stderr,
        )
        return 1
    if size <= 0:
        print(f"Native build produced an empty bundle at {artifact}.", file=sys.stderr)
        return 1
    media_errors = bundle_media_errors(artifact, prepared.target, prepared.contract)
    if media_errors:
        print("Native bundle media verification failed:", file=sys.stderr)
        for error in media_errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    return _smoke_and_publish_evidence(artifact, prepared, size)


def _smoke_and_publish_evidence(
    artifact: Path, prepared: PreparedMediaStack, size: int
) -> int:
    try:
        evidence = prepare_compliance_evidence(
            prepared.compliance_archive,
            artifact.parent,
            prepared.target,
            prepared.identity,
        )
    except (OSError, ValueError) as error:
        print(f"Could not prepare media compliance evidence: {error}", file=sys.stderr)
        return 1
    smoke = subprocess.run(
        [str(Path(sys.executable)), "packaging/smoke_child.py", "dist"],
        cwd=ROOT,
        check=False,
    )
    if smoke.returncode != 0:
        _discard_compliance_evidence(evidence)
        print(
            "Native build produced a bundle that failed the offline smoke test "
            f"(exit status {smoke.returncode}).",
            file=sys.stderr,
        )
        return smoke.returncode or 1
    try:
        publish_compliance_evidence(evidence)
    except OSError as error:
        print(f"Could not publish media compliance evidence: {error}", file=sys.stderr)
        return 1
    print(f"Built {artifact.relative_to(ROOT)} ({size / 1024**2:.1f} MiB).")
    return 0


def _installed_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for distribution in _PINNED_DISTRIBUTIONS:
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = None
    return versions


def _version_matches(distribution: str, actual: str) -> bool:
    if distribution == "Nuitka":
        return actual == "2.8.10"
    if distribution == "onnxruntime":
        return actual == "1.29.0"
    return actual.startswith("6.10.")


def _onnxruntime_capi_directory() -> Path:
    distribution = importlib.metadata.distribution("onnxruntime")
    return Path(distribution.locate_file("onnxruntime/capi"))


def _create_onnxruntime_dylib_alias(directory: Path) -> Path | None:
    alias = directory / "libonnxruntime.1.dylib"
    if alias.exists() or alias.is_symlink():
        return None
    candidates = tuple(directory.glob("libonnxruntime.*.dylib"))
    if len(candidates) != 1:
        raise RuntimeError(
            "the pinned ONNX Runtime wheel does not expose one versioned "
            f"macOS dylib in {directory}"
        )
    try:
        alias.symlink_to(candidates[0].name)
    except OSError as error:
        raise RuntimeError(
            f"could not create ONNX Runtime dylib alias: {error}"
        ) from error
    return alias


def _spec_data_sources(spec_path: Path) -> tuple[str, ...]:
    parser = configparser.ConfigParser(
        comment_prefixes=("/",), strict=False, allow_no_value=True
    )
    parser.read(spec_path, encoding="utf-8")
    args = shlex.split(parser.get("nuitka", "extra_args"))
    sources: list[str] = []
    for argument in args:
        if not argument.startswith("--include-data-files="):
            continue
        value = argument.partition("=")[2]
        source, separator, _destination = value.partition("=")
        if not separator or not source:
            raise ValueError(f"invalid data-file argument: {argument}")
        sources.append(source)
    return tuple(sources)


def _verified_explicit_artifacts(
    wheel: Path,
    manifest_path: Path,
    target: BuildTarget,
    verify: MediaWheelVerifier,
) -> MediaStackArtifacts:
    sidecar = provenance_path(wheel)
    if not sidecar.is_file():
        raise ValueError(f"provenance sidecar is missing: {sidecar}")
    report = verify(wheel, manifest_path, target)
    report_path = wheel.parent / "verification-report.json"
    compliance = wheel.parent / (
        f"MatteLoop-media-sources-{target.target_id}-{report.identity}.tar.gz"
    )
    if not compliance.is_file():
        raise ValueError(f"media compliance archive is missing: {compliance}")
    binding = artifact_set_path(wheel)
    validate_artifact_set(
        binding,
        wheel,
        sidecar,
        report_path,
        compliance,
        verified_report=_verification_fields(report),
        target=target,
    )
    return MediaStackArtifacts(
        wheel,
        sidecar,
        compliance,
        report_path,
        binding,
        report.identity,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_fsynced(source: Path, destination: Path) -> None:
    with source.open("rb") as input_file, destination.open("xb") as output:
        shutil.copyfileobj(input_file, output)
        output.flush()
        os.fsync(output.fileno())


def _write_fsynced(destination: Path, contents: bytes) -> None:
    with destination.open("xb") as output:
        output.write(contents)
        output.flush()
        os.fsync(output.fileno())


def _verification_fields(report: VerificationReport) -> dict[str, object]:
    return {
        name: getattr(report, name)
        for name in (
            "identity",
            "manifest_sha256",
            "python_tag",
            "target_id",
            "wheel_filename",
            "wheel_sha256",
        )
    }


if __name__ == "__main__":
    raise SystemExit(main())
