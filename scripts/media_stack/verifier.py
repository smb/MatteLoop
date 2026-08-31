"""Verify that a candidate PyAV wheel satisfies MatteLoop's LGPL contract."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, TypeGuard
from zipfile import BadZipFile, ZipFile

from .manifest import (
    MEDIA_STACK_BUILDER_REVISION,
    MediaStackManifest,
    PyAVWheelTags,
    VerificationContract,
    load_manifest,
    media_stack_identity,
)
from .platforms import BuildTarget, detect_target

_PROVENANCE_FIELDS = frozenset(
    (
        "identity",
        "manifest_sha256",
        "target_id",
        "python_tag",
        "wheel_filename",
        "wheel_sha256",
    )
)
_GPL_WITHOUT_L_PREFIX = re.compile(r"(?<!l)gpl")
_GPL_LEGAL_FILENAMES = frozenset(("GPL-3.0.txt", "LGPL-3.0.txt"))
_ARCHIVE_ERRORS = (BadZipFile, OSError, RuntimeError, NotImplementedError)
_ROOT = Path(__file__).resolve().parents[2]
_INSPECTION_SCRIPT = r"""
import json
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

wheel_root = Path(sys.argv[1]).resolve()
try:
    import av
except Exception as error:
    raise RuntimeError(
        "candidate av could not be imported from extracted wheel root"
    ) from error
av_path = Path(av.__file__).resolve()
try:
    av_path.relative_to(wheel_root)
except ValueError as error:
    raise RuntimeError("av.__file__ is not below extracted wheel root") from error

fixture_dir = Path(sys.argv[2]).resolve() if sys.argv[2] else None
if fixture_dir is not None:
    from PIL import Image
    from matteloop.core.webp import encode_lossless_webp, validate_webp
    from matteloop.jobs.source import decode_frame, probe_source

    for request_id, name in enumerate(("h264-sdr.mp4", "h265-sdr.mp4"), 1):
        source = fixture_dir / name
        probe_source(source)
        decoded = decode_frame(source, Fraction(0), request_id)
        decoded.image.close()
    with tempfile.TemporaryDirectory(prefix="matteloop-webp-verification-") as raw:
        scratch = Path(raw)
        frame_paths = (scratch / "frame-0.png", scratch / "frame-1.png")
        for path, color in zip(frame_paths, ((20, 40, 80, 64), (160, 80, 20, 192))):
            with Image.new("RGBA", (128, 128), color) as image:
                image.save(path)
        output = scratch / "verified.webp"
        encode_lossless_webp(frame_paths, (100, 100), output)
        validate_webp(output, expected_frames=2, expected_duration_ms=200)

print(json.dumps({
    "ffmpeg_version": av.ffmpeg_version_info,
    "library_meta": av._core.library_meta,
    "codecs": sorted(av.codecs_available),
    "formats": sorted(av.formats_available),
}, sort_keys=True))
"""


@dataclass(frozen=True, slots=True)
class RuntimeEvidence:
    ffmpeg_version: str
    configurations: tuple[str, ...]
    licenses: tuple[str, ...]
    codecs: tuple[str, ...]
    formats: tuple[str, ...]
    dependencies: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerificationReport:
    identity: str
    manifest_sha256: str
    target_id: str
    python_tag: str
    wheel_filename: str
    wheel_sha256: str
    evidence: RuntimeEvidence


@dataclass(frozen=True, slots=True)
class _InputSnapshots:
    manifest: Path
    wheel: Path
    provenance: Path
    wheel_filename: str


class MediaStackVerificationError(ValueError):
    """One or more reasons a candidate wheel cannot be trusted."""

    def __init__(self, errors: tuple[str, ...]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


class _VerifierArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        _fail((message,))


def configuration_errors(
    evidence: RuntimeEvidence, contract: VerificationContract
) -> tuple[str, ...]:
    """Return all licence, configuration, capability, and dependency errors."""
    errors: list[str] = []
    for configuration in evidence.configurations:
        normalized = configuration.casefold()
        for token in contract.forbidden_tokens:
            if token.casefold() in normalized:
                errors.append(f"forbidden configure token {token!r}: {configuration}")
    errors.extend(_license_errors(evidence.licenses))
    errors.extend(
        _missing_capability_errors(
            evidence.codecs,
            evidence.formats,
            contract.required_codecs,
            contract.required_formats,
        )
    )
    errors.extend(
        _dependency_errors(evidence.dependencies, contract.forbidden_library_fragments)
    )
    return tuple(errors)


def forbidden_bundle_entries(
    root: Path, fragments: tuple[str, ...]
) -> tuple[Path, ...]:
    """Return recursively bundled entries whose names contain forbidden fragments."""
    normalized = tuple(fragment.casefold() for fragment in fragments)
    matches = (
        entry
        for entry in root.rglob("*")
        if any(fragment in entry.name.casefold() for fragment in normalized)
        or (
            _has_forbidden_license_marker(entry.relative_to(root).as_posix())
            and not _is_exact_gpl_legal_file(entry)
        )
    )
    return tuple(sorted(matches, key=lambda path: path.as_posix()))


def _is_exact_gpl_legal_file(entry: Path) -> bool:
    return (
        entry.name in _GPL_LEGAL_FILENAMES
        and not entry.is_symlink()
        and entry.is_file()
    )


def provenance_path(wheel: Path) -> Path:
    """Return the canonical provenance sidecar path for *wheel*."""
    return wheel.with_name(f"{wheel.name}.provenance.json")


def verify_media_wheel(
    wheel: Path,
    manifest_path: Path,
    target: BuildTarget,
    *,
    fixture_dir: Path | None = None,
) -> VerificationReport:
    """Verify provenance, wheel metadata, runtime evidence, and native dependencies."""
    return _verify_input_paths(wheel, manifest_path, target, fixture_dir)


def _verify_input_paths(
    wheel: Path,
    manifest_path: Path,
    target: BuildTarget | None,
    fixture_dir: Path | None = None,
) -> VerificationReport:
    with tempfile.TemporaryDirectory(prefix="matteloop-media-inputs-") as raw:
        snapshots = _snapshot_inputs(wheel, manifest_path, Path(raw))
        manifest = load_manifest(snapshots.manifest)
        resolved_target = target or detect_target(
            python_tag=manifest.python_abi,
            deployment_target=manifest.macos_deployment_target,
        )
        return _verify_snapshots(snapshots, manifest, resolved_target, fixture_dir)


def _verify_snapshots(
    snapshots: _InputSnapshots,
    manifest: MediaStackManifest,
    target: BuildTarget,
    fixture_dir: Path | None,
) -> VerificationReport:
    expected = _expected_provenance(
        snapshots.wheel,
        snapshots.manifest,
        target,
        wheel_filename=snapshots.wheel_filename,
    )
    provenance = _load_provenance(snapshots.provenance)
    _fail_if(_provenance_errors(provenance, expected))
    _fail_if(_wheel_metadata_errors(snapshots.wheel, manifest, target))

    with tempfile.TemporaryDirectory(prefix="matteloop-media-verification-") as raw:
        extracted_root = Path(raw) / "wheel"
        extracted_root.mkdir()
        try:
            with ZipFile(snapshots.wheel) as archive:
                archive.extractall(extracted_root)
        except _ARCHIVE_ERRORS as error:
            _fail((f"wheel extraction failed: {error}",))
        payload = _inspect_runtime(extracted_root, fixture_dir)
        dependencies = _collect_dependencies(snapshots.wheel, target)
        evidence = _runtime_evidence(payload, dependencies)
        ffmpeg_version = next(
            source.version for source in manifest.sources if source.name == "ffmpeg"
        )
        errors = []
        if evidence.ffmpeg_version != ffmpeg_version:
            errors.append(f"FFmpeg version must be {ffmpeg_version}")
        errors.extend(configuration_errors(evidence, manifest.verification))
        errors.extend(
            f"forbidden bundled library entry: {entry.relative_to(extracted_root)}"
            for entry in forbidden_bundle_entries(
                extracted_root, manifest.verification.forbidden_library_fragments
            )
        )
        _fail_if(tuple(errors))

    return VerificationReport(
        identity=expected["identity"],
        manifest_sha256=expected["manifest_sha256"],
        target_id=target.target_id,
        python_tag=target.python_tag,
        wheel_filename=snapshots.wheel_filename,
        wheel_sha256=expected["wheel_sha256"],
        evidence=evidence,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the verifier CLI and publish a report only after complete success."""
    try:
        parser = _VerifierArgumentParser(description=__doc__)
        parser.add_argument("wheel", type=Path)
        parser.add_argument("--manifest", required=True, type=Path)
        parser.add_argument("--report", required=True, type=Path)
        arguments = parser.parse_args(argv)
        report = _verify_input_paths(arguments.wheel, arguments.manifest, None)
        _publish_report(arguments.report, report)
    except (MediaStackVerificationError, OSError, ValueError) as error:
        errors = getattr(error, "errors", (str(error),))
        print("Media stack verification failed:", file=sys.stderr)
        for message in errors:
            for line in str(message).splitlines() or ("",):
                print(f"- {line}", file=sys.stderr)
        return 1
    return 0


def _license_errors(licenses: tuple[str, ...]) -> tuple[str, ...]:
    if not licenses:
        return ("no FFmpeg library licences were reported",)
    errors: list[str] = []
    for license_text in licenses:
        normalized = license_text.casefold()
        if _has_forbidden_license_marker(normalized):
            errors.append(f"forbidden FFmpeg licence: {license_text}")
        elif "lgpl" not in normalized:
            errors.append(f"FFmpeg library is not LGPL: {license_text}")
    return tuple(errors)


def _missing_capability_errors(
    codecs: tuple[str, ...],
    formats: tuple[str, ...],
    required_codecs: tuple[str, ...],
    required_formats: tuple[str, ...],
) -> tuple[str, ...]:
    available_codecs = {codec.casefold() for codec in codecs}
    available_formats = {media_format.casefold() for media_format in formats}
    missing_codecs = tuple(
        codec for codec in required_codecs if codec.casefold() not in available_codecs
    )
    missing_formats = tuple(
        media_format
        for media_format in required_formats
        if media_format.casefold() not in available_formats
    )
    errors: list[str] = []
    if missing_codecs:
        errors.append(f"missing required codecs: {', '.join(missing_codecs)}")
    if missing_formats:
        errors.append(f"missing required formats: {', '.join(missing_formats)}")
    return tuple(errors)


def _dependency_errors(
    dependencies: tuple[str, ...], fragments: tuple[str, ...]
) -> tuple[str, ...]:
    errors: list[str] = []
    normalized_fragments = tuple(fragment.casefold() for fragment in fragments)
    for dependency in dependencies:
        normalized_line = dependency.casefold()
        basename = PurePosixPath(dependency.replace("\\", "/").split(" ", 1)[0]).name
        normalized_basename = basename.casefold()
        if _has_forbidden_license_marker(
            normalized_line
        ) or _has_forbidden_license_marker(normalized_basename):
            errors.append(f"forbidden native dependency: {dependency}")
            continue
        for fragment in normalized_fragments:
            if fragment in normalized_line or fragment in normalized_basename:
                errors.append(f"forbidden native dependency: {dependency}")
                break
    return tuple(errors)


def _has_forbidden_license_marker(value: str) -> bool:
    normalized = value.casefold()
    return (
        "nonfree" in normalized or _GPL_WITHOUT_L_PREFIX.search(normalized) is not None
    )


def _snapshot_inputs(wheel: Path, manifest: Path, destination: Path) -> _InputSnapshots:
    wheel_snapshot = _snapshot_file(
        wheel, destination / "wheel", f"wheel does not exist: {wheel}"
    )
    manifest_snapshot = _snapshot_file(
        manifest,
        destination / "manifest",
        f"manifest does not exist: {manifest}",
    )
    sidecar = provenance_path(wheel)
    provenance_snapshot = _snapshot_file(
        sidecar,
        destination / "provenance",
        f"provenance sidecar is missing: {sidecar}",
    )
    return _InputSnapshots(
        manifest_snapshot,
        wheel_snapshot,
        provenance_snapshot,
        wheel.name,
    )


def _snapshot_file(source: Path, directory: Path, missing_error: str) -> Path:
    if not source.is_file():
        _fail((missing_error,))
    destination = directory / source.name
    try:
        directory.mkdir()
        with source.open("rb") as input_file, destination.open("xb") as output_file:
            shutil.copyfileobj(input_file, output_file)
    except OSError as error:
        _fail((f"could not snapshot {source}: {error}",))
    return destination


def _expected_provenance(
    wheel: Path,
    manifest_path: Path,
    target: BuildTarget,
    *,
    wheel_filename: str | None = None,
) -> dict[str, str]:
    return {
        "identity": media_stack_identity(
            manifest_path,
            os_name=target.os_name,
            machine=target.machine,
            python_tag=target.python_tag,
            deployment_target=target.deployment_target,
            builder_revision=MEDIA_STACK_BUILDER_REVISION,
        ),
        "manifest_sha256": _sha256(manifest_path),
        "target_id": target.target_id,
        "python_tag": target.python_tag,
        "wheel_filename": wheel.name if wheel_filename is None else wheel_filename,
        "wheel_sha256": _sha256(wheel),
    }


def _load_provenance(path: Path) -> dict[str, str]:
    if not path.is_file():
        _fail((f"provenance sidecar is missing: {path}",))
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        _fail((f"provenance sidecar is invalid: {error}",))
    if not isinstance(payload, dict) or set(payload) != _PROVENANCE_FIELDS:
        _fail(("provenance sidecar has invalid fields",))
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in payload.items()
    ):
        _fail(("provenance sidecar fields must be strings",))
    typed = dict(payload)
    if raw != _canonical_json(typed):
        _fail(("provenance sidecar must use canonical JSON",))
    return typed


def _provenance_errors(
    actual: dict[str, str], expected: dict[str, str]
) -> tuple[str, ...]:
    return tuple(
        f"provenance {field} does not match the candidate wheel"
        for field in sorted(_PROVENANCE_FIELDS)
        if actual[field] != expected[field]
    )


def _wheel_metadata_errors(
    wheel: Path, manifest: MediaStackManifest, target: BuildTarget
) -> tuple[str, ...]:
    pyav_version = next(
        source.version for source in manifest.sources if source.name == "pyav"
    )
    errors = list(
        _wheel_filename_errors(wheel, pyav_version, manifest.pyav_wheel, target)
    )
    try:
        with ZipFile(wheel) as archive:
            metadata_names = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            wheel_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")
            ]
            if len(metadata_names) != 1 or len(wheel_names) != 1:
                return (*errors, "wheel must contain one METADATA and one WHEEL file")
            metadata = Parser().parsestr(
                archive.read(metadata_names[0]).decode("utf-8")
            )
            wheel_metadata = Parser().parsestr(
                archive.read(wheel_names[0]).decode("utf-8")
            )
    except (*_ARCHIVE_ERRORS, UnicodeError) as error:
        return (*errors, f"wheel metadata could not be read: {error}")
    if metadata.get("Name", "").casefold() != "av":
        errors.append("wheel distribution name must be av")
    if metadata.get("Version") != pyav_version:
        errors.append(f"wheel PyAV version must be {pyav_version}")
    tags = tuple(wheel_metadata.get_all("Tag", []))
    if not any(_tag_matches(tag, manifest.pyav_wheel, target) for tag in tags):
        expected_abi = _expected_wheel_abi(manifest.pyav_wheel)
        errors.append(f"wheel tags do not match {expected_abi}/{target.target_id}")
    return tuple(errors)


def _wheel_filename_errors(
    wheel: Path,
    pyav_version: str,
    wheel_tags: PyAVWheelTags,
    target: BuildTarget,
) -> tuple[str, ...]:
    parts = wheel.name.removesuffix(".whl").split("-")
    if not wheel.name.endswith(".whl") or len(parts) != 5:
        return ("candidate filename is not a supported wheel name",)
    distribution, version, python_tag, abi_tag, platform_tag = parts
    errors: list[str] = []
    if distribution.casefold() != "av" or version != pyav_version:
        errors.append(f"candidate must be the av {pyav_version} wheel")
    if python_tag != wheel_tags.python_tag or abi_tag != wheel_tags.abi_tag:
        errors.append(f"candidate wheel ABI must be {_expected_wheel_abi(wheel_tags)}")
    if not _platform_matches(platform_tag, target):
        errors.append(f"candidate wheel platform must be {target.target_id}")
    return tuple(errors)


def _tag_matches(
    tag: str, wheel_tags: PyAVWheelTags, target: BuildTarget
) -> bool:
    parts = tag.split("-", 2)
    return (
        len(parts) == 3
        and parts[0] == wheel_tags.python_tag
        and parts[1] == wheel_tags.abi_tag
        and _platform_matches(parts[2], target)
    )


def _expected_wheel_abi(wheel_tags: PyAVWheelTags) -> str:
    return f"{wheel_tags.python_tag}-{wheel_tags.abi_tag}"


def _platform_matches(platform_tag: str, target: BuildTarget) -> bool:
    if target.target_id == "macos-arm64":
        return platform_tag == "macosx_13_0_arm64"
    if target.target_id == "windows-x64":
        return platform_tag == "win_amd64"
    return False


def _inspect_runtime(root: Path, fixture_dir: Path | None) -> dict[str, Any]:
    environment = os.environ.copy()
    leading_paths = (str(root), str(_ROOT / "src"))
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        (*leading_paths, *((existing,) if existing else ()))
    )
    command = (
        sys.executable,
        "-c",
        _INSPECTION_SCRIPT,
        str(root),
        "" if fixture_dir is None else str(fixture_dir),
    )
    completed = _run(command, environment=environment)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        _fail((f"wheel inspection emitted invalid JSON: {error}",))
    if not isinstance(payload, dict):
        _fail(("wheel inspection did not emit a JSON object",))
    return payload


def _collect_dependencies(wheel: Path, target: BuildTarget) -> tuple[str, ...]:
    command: tuple[str, ...]
    if target.target_id == "macos-arm64":
        command = ("delocate-listdeps", "--all", str(wheel))
    elif target.target_id == "windows-x64":
        command = (sys.executable, "-m", "delvewheel", "show", str(wheel))
    else:
        _fail((f"unsupported dependency inspection target: {target.target_id}",))
    completed = _run(command)
    return tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())


def _run(
    command: tuple[str, ...], *, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
    except OSError as error:
        _fail((f"command could not run ({' '.join(command)}): {error}",))
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        _fail((f"command failed ({' '.join(command)}): {detail}",))
    return completed


def _runtime_evidence(
    payload: dict[str, Any], dependencies: tuple[str, ...]
) -> RuntimeEvidence:
    ffmpeg_version = payload.get("ffmpeg_version")
    library_meta = payload.get("library_meta")
    codecs = payload.get("codecs")
    formats = payload.get("formats")
    if not isinstance(ffmpeg_version, str) or not isinstance(library_meta, dict):
        _fail(("wheel inspection metadata is malformed",))
    if not _is_string_list(codecs) or not _is_string_list(formats):
        _fail(("wheel inspection capabilities are malformed",))
    configurations: list[str] = []
    licenses: list[str] = []
    for library_name in sorted(library_meta):
        metadata = library_meta[library_name]
        if not isinstance(library_name, str) or not isinstance(metadata, dict):
            _fail(("FFmpeg library metadata is malformed",))
        configuration = metadata.get("configuration")
        license_text = metadata.get("license")
        if not isinstance(configuration, str) or not isinstance(license_text, str):
            _fail((f"FFmpeg metadata is incomplete for {library_name}",))
        configurations.append(configuration)
        licenses.append(license_text)
    return RuntimeEvidence(
        ffmpeg_version=ffmpeg_version,
        configurations=tuple(configurations),
        licenses=tuple(licenses),
        codecs=tuple(codecs),
        formats=tuple(formats),
        dependencies=dependencies,
    )


def _is_string_list(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"


def _publish_report(path: Path, report: VerificationReport) -> None:
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(raw_path)
        output = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = -1
        with output:
            output.write(_canonical_json(asdict(report)))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _fail_if(errors: tuple[str, ...]) -> None:
    if errors:
        _fail(errors)


def _fail(errors: tuple[str, ...]) -> NoReturn:
    raise MediaStackVerificationError(errors)
