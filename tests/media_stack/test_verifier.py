import hashlib
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.media_stack.manifest import load_manifest, media_stack_identity
from scripts.media_stack.platforms import BuildTarget
from scripts.media_stack.verifier import (
    MediaStackVerificationError,
    RuntimeEvidence,
    VerificationReport,
    configuration_errors,
    forbidden_bundle_entries,
    main,
    provenance_path,
    verify_media_wheel,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "packaging" / "media-stack" / "manifest.toml"
MACOS = BuildTarget("darwin", "arm64", "macos-arm64", "cp313", "13.0")
WINDOWS = BuildTarget("win32", "AMD64", "windows-x64", "cp313", "")


def _evidence(**replacements: tuple[str, ...] | str) -> RuntimeEvidence:
    values: dict[str, tuple[str, ...] | str] = {
        "ffmpeg_version": "8.0.1",
        "configurations": ("--enable-shared --disable-gpl --disable-nonfree",),
        "licenses": ("LGPL version 2.1 or later",),
        "codecs": ("h264", "hevc", "libwebp_anim"),
        "formats": ("mov", "webp"),
        "dependencies": ("@rpath/libwebp.7.dylib",),
    }
    values.update(replacements)
    return RuntimeEvidence(**values)  # type: ignore[arg-type]


def _wheel_path(tmp_path: Path, target: BuildTarget = MACOS) -> Path:
    platform_tag = (
        "macosx_13_0_arm64" if target.target_id == "macos-arm64" else "win_amd64"
    )
    return tmp_path / f"av-16.1.0-cp313-cp313-{platform_tag}.whl"


def _write_fake_wheel(
    path: Path, *, include_av: bool = True, ffmpeg_version: str = "8.0.1"
) -> None:
    platform_tag = "macosx_13_0_arm64" if "macosx" in path.name else "win_amd64"
    with ZipFile(path, "w") as wheel:
        if include_av:
            wheel.writestr(
                "av/__init__.py",
                "\n".join(
                    (
                        "from . import _core",
                        '__version__ = "16.1.0"',
                        '__file_marker__ = "candidate-wheel"',
                        "__all__ = ()",
                        f'ffmpeg_version_info = "{ffmpeg_version}"',
                        'codecs_available = {"H264", "HEVC", "LIBWEBP_ANIM"}',
                        'formats_available = {"MOV", "WEBP"}',
                    )
                ),
            )
            wheel.writestr(
                "av/_core.py",
                "library_meta = {"
                '"libavcodec": {'
                '"configuration": "--ENABLE-SHARED --DISABLE-GPL", '
                '"license": "LGPL VERSION 2.1 OR LATER"'
                "}}\n",
            )
        wheel.writestr(
            "av-16.1.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: av\nVersion: 16.1.0\n",
        )
        wheel.writestr(
            "av-16.1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: test\n"
            "Root-Is-Purelib: false\n"
            f"Tag: cp313-cp313-{platform_tag}\n",
        )


def _provenance_payload(
    wheel: Path, target: BuildTarget, manifest_path: Path = MANIFEST
) -> dict[str, str]:
    return {
        "identity": media_stack_identity(
            manifest_path,
            os_name=target.os_name,
            machine=target.machine,
            python_tag=target.python_tag,
            deployment_target=target.deployment_target,
        ),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "target_id": target.target_id,
        "python_tag": target.python_tag,
        "wheel_filename": wheel.name,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }


def _write_provenance(
    wheel: Path,
    target: BuildTarget,
    manifest_path: Path = MANIFEST,
    **replacements: str,
) -> None:
    payload = _provenance_payload(wheel, target, manifest_path)
    payload.update(replacements)
    provenance_path(wheel).write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def test_configuration_accepts_lgpl_metadata_without_normalizing_evidence() -> None:
    evidence = _evidence(
        configurations=("--ENABLE-SHARED --DISABLE-GPL --DISABLE-NONFREE",),
        licenses=("LGPL VERSION 2.1 OR LATER",),
        codecs=("H264", "HEVC", "LIBWEBP_ANIM"),
        formats=("MOV", "WEBP"),
    )

    assert configuration_errors(evidence, load_manifest(MANIFEST).verification) == ()
    assert evidence.licenses == ("LGPL VERSION 2.1 OR LATER",)


@pytest.mark.parametrize(
    "token",
    ("--ENABLE-GPL", "--ENABLE-NONFREE", "LIBX264", "LIBX265", "LIBOPENH264"),
)
def test_configuration_rejects_every_forbidden_token_case_insensitively(
    token: str,
) -> None:
    evidence = _evidence(configurations=(f"--enable-shared {token}",))

    errors = configuration_errors(evidence, load_manifest(MANIFEST).verification)

    assert any(token.lower() in error.lower() for error in errors)


@pytest.mark.parametrize(
    "license_text",
    ("GPL version 3 or later", "nonfree and unredistributable"),
)
def test_configuration_rejects_gpl_and_nonfree_licenses(license_text: str) -> None:
    errors = configuration_errors(
        _evidence(licenses=(license_text,)), load_manifest(MANIFEST).verification
    )

    assert any(license_text in error for error in errors)


def test_configuration_reports_all_missing_codec_and_format_capabilities() -> None:
    errors = configuration_errors(
        _evidence(codecs=(), formats=()), load_manifest(MANIFEST).verification
    )

    combined = "\n".join(errors).lower()
    assert all(name in combined for name in ("h264", "hevc", "libwebp_anim"))
    assert all(name in combined for name in ("mov", "webp"))


def test_configuration_checks_dependency_basenames_and_full_lines() -> None:
    evidence = _evidence(
        dependencies=(
            "/opt/x264/libavcodec.62.dylib (compatibility version 62.0.0)",
            r"C:\wheel\libs\LIBX265.DLL",
        )
    )

    errors = configuration_errors(evidence, load_manifest(MANIFEST).verification)

    assert any("/opt/x264/" in error for error in errors)
    assert any("LIBX265.DLL" in error for error in errors)


@pytest.mark.parametrize(
    "dependency",
    (
        "/wheel/lib/libGPLCodec.dylib",
        "/wheel/nonfree/libavcodec.62.dylib",
    ),
)
def test_configuration_rejects_gpl_and_nonfree_native_dependency_evidence(
    dependency: str,
) -> None:
    errors = configuration_errors(
        _evidence(dependencies=(dependency,)), load_manifest(MANIFEST).verification
    )

    assert any(dependency in error for error in errors)


def test_configuration_accepts_lgpl_native_dependency_evidence() -> None:
    evidence = _evidence(dependencies=("/wheel/lib/libLGPLCodec.dylib",))

    assert configuration_errors(evidence, load_manifest(MANIFEST).verification) == ()


def test_bundle_scan_finds_forbidden_filenames_recursively(tmp_path: Path) -> None:
    forbidden = tmp_path / "av" / ".dylibs" / "libOpenH264.7.dylib"
    allowed = tmp_path / "av" / ".dylibs" / "libwebp.7.dylib"
    forbidden.parent.mkdir(parents=True)
    forbidden.write_bytes(b"forbidden")
    allowed.write_bytes(b"allowed")

    assert forbidden_bundle_entries(tmp_path, ("x264", "x265", "openh264")) == (
        forbidden,
    )


def test_bundle_scan_rejects_gpl_and_nonfree_but_accepts_lgpl(tmp_path: Path) -> None:
    gpl = tmp_path / "nested" / "libGPLCodec.dylib"
    nonfree = tmp_path / "nested" / "nonfree-codec.dll"
    lgpl = tmp_path / "nested" / "libLGPLCodec.dylib"
    gpl.parent.mkdir()
    for path in (gpl, nonfree, lgpl):
        path.write_bytes(b"library")

    assert forbidden_bundle_entries(tmp_path, ()) == (gpl, nonfree)


def test_runtime_evidence_and_report_are_frozen_and_slotted() -> None:
    evidence = _evidence()
    report = VerificationReport(
        identity="identity",
        manifest_sha256="a" * 64,
        target_id="macos-arm64",
        python_tag="cp313",
        wheel_filename="av.whl",
        wheel_sha256="b" * 64,
        evidence=evidence,
    )

    with pytest.raises(FrozenInstanceError):
        evidence.ffmpeg_version = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        report.identity = "changed"  # type: ignore[misc]
    assert not hasattr(evidence, "__dict__")
    assert not hasattr(report, "__dict__")


def test_provenance_path_appends_to_the_complete_wheel_name(tmp_path: Path) -> None:
    wheel = tmp_path / "candidate.whl"

    assert provenance_path(wheel) == tmp_path / "candidate.whl.provenance.json"


def test_verification_rejects_a_missing_provenance_sidecar(tmp_path: Path) -> None:
    wheel = _wheel_path(tmp_path)
    wheel.write_bytes(b"untrusted wheel")

    with pytest.raises(ValueError, match="provenance sidecar is missing"):
        verify_media_wheel(wheel, MANIFEST, MACOS)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    (
        ("identity", "wrong", "identity"),
        ("manifest_sha256", "0" * 64, "manifest_sha256"),
        ("target_id", "windows-x64", "target_id"),
        ("python_tag", "cp312", "python_tag"),
        ("wheel_filename", "other.whl", "wheel_filename"),
        ("wheel_sha256", "0" * 64, "wheel_sha256"),
    ),
)
def test_verification_rejects_each_mismatched_provenance_binding(
    tmp_path: Path, field: str, replacement: str, message: str
) -> None:
    wheel = _wheel_path(tmp_path)
    wheel.write_bytes(b"untrusted wheel")
    _write_provenance(wheel, MACOS, **{field: replacement})

    with pytest.raises(ValueError, match=message):
        verify_media_wheel(wheel, MANIFEST, MACOS)


def test_verification_rejects_noncanonical_provenance_json(tmp_path: Path) -> None:
    wheel = _wheel_path(tmp_path)
    wheel.write_bytes(b"untrusted wheel")
    provenance_path(wheel).write_text(
        json.dumps(_provenance_payload(wheel, MACOS), indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="canonical JSON"):
        verify_media_wheel(wheel, MANIFEST, MACOS)


@pytest.mark.parametrize(
    "archive_error",
    (RuntimeError("encrypted wheel entry"), NotImplementedError("unsupported ZIP")),
)
def test_verification_converts_expected_archive_extraction_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    archive_error: Exception,
) -> None:
    from scripts.media_stack import verifier

    wheel = _wheel_path(tmp_path)
    _write_fake_wheel(wheel)
    _write_provenance(wheel, MACOS)

    def fail_extraction(*_args: object, **_kwargs: object) -> None:
        raise archive_error

    monkeypatch.setattr(verifier.ZipFile, "extractall", fail_extraction)

    with pytest.raises(MediaStackVerificationError, match="wheel extraction failed"):
        verify_media_wheel(wheel, MANIFEST, MACOS)


def test_verification_inspects_the_extracted_wheel_with_current_cpython(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.media_stack import verifier

    wheel = _wheel_path(tmp_path)
    _write_fake_wheel(wheel)
    _write_provenance(wheel, MACOS)
    real_run = subprocess.run
    dependency_commands: list[tuple[str, ...]] = []

    def run(
        command: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[0] == "delocate-listdeps":
            dependency_commands.append(command)
            return subprocess.CompletedProcess(
                command, 0, stdout="@rpath/LIBWEBP.7.DYLIB\n", stderr=""
            )
        return real_run(command, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(verifier.subprocess, "run", run)

    report = verify_media_wheel(wheel, MANIFEST, MACOS)

    assert len(dependency_commands) == 1
    assert dependency_commands[0][:2] == ("delocate-listdeps", "--all")
    assert Path(dependency_commands[0][-1]) != wheel
    assert Path(dependency_commands[0][-1]).name == wheel.name
    assert report.evidence == _evidence(
        configurations=("--ENABLE-SHARED --DISABLE-GPL",),
        licenses=("LGPL VERSION 2.1 OR LATER",),
        codecs=("H264", "HEVC", "LIBWEBP_ANIM"),
        formats=("MOV", "WEBP"),
        dependencies=("@rpath/LIBWEBP.7.DYLIB",),
    )
    assert report.wheel_filename == wheel.name


def test_verification_uses_private_snapshots_after_original_paths_are_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.media_stack import verifier

    manifest = tmp_path / "manifest.toml"
    manifest.write_bytes(MANIFEST.read_bytes())
    wheel = _wheel_path(tmp_path)
    _write_fake_wheel(wheel)
    _write_provenance(wheel, MACOS, manifest)
    expected = _provenance_payload(wheel, MACOS, manifest)
    real_load_provenance = verifier._load_provenance
    real_run = subprocess.run
    dependency_wheels: list[Path] = []

    def load_then_replace_originals(path: Path) -> dict[str, str]:
        payload = real_load_provenance(path)
        manifest.write_text("replaced manifest", encoding="utf-8")
        wheel.write_bytes(b"replaced wheel")
        provenance_path(wheel).write_text("{}\n", encoding="utf-8")
        return payload

    def run(
        command: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[0] == "delocate-listdeps":
            dependency_wheels.append(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return real_run(command, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(verifier, "_load_provenance", load_then_replace_originals)
    monkeypatch.setattr(verifier.subprocess, "run", run)

    report = verify_media_wheel(wheel, manifest, MACOS)

    assert report.identity == expected["identity"]
    assert report.manifest_sha256 == expected["manifest_sha256"]
    assert report.wheel_sha256 == expected["wheel_sha256"]
    assert report.wheel_filename == wheel.name
    assert len(dependency_wheels) == 1
    assert dependency_wheels[0] != wheel
    assert dependency_wheels[0].name == wheel.name


def test_verification_never_falls_back_to_the_installed_av_package(
    tmp_path: Path,
) -> None:
    wheel = _wheel_path(tmp_path)
    _write_fake_wheel(wheel, include_av=False)
    _write_provenance(wheel, MACOS)

    with pytest.raises(ValueError, match="extracted wheel root"):
        verify_media_wheel(wheel, MANIFEST, MACOS)


def test_verification_rejects_an_unpinned_runtime_ffmpeg_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.media_stack import verifier

    wheel = _wheel_path(tmp_path)
    _write_fake_wheel(wheel, ffmpeg_version="7.1.2")
    _write_provenance(wheel, MACOS)
    real_run = subprocess.run

    def run(
        command: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[0] == "delocate-listdeps":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return real_run(command, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(verifier.subprocess, "run", run)

    with pytest.raises(ValueError, match="FFmpeg version must be 8.0.1"):
        verify_media_wheel(wheel, MANIFEST, MACOS)


def test_windows_dependency_inventory_uses_delvewheel_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.media_stack import verifier

    wheel = _wheel_path(tmp_path, WINDOWS)
    _write_fake_wheel(wheel)
    _write_provenance(wheel, WINDOWS)
    real_run = subprocess.run
    dependency_commands: list[tuple[str, ...]] = []

    def run(
        command: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[:3] == (sys.executable, "-m", "delvewheel"):
            dependency_commands.append(command)
            return subprocess.CompletedProcess(
                command, 0, stdout="av.libs\\libwebp-7.dll\n", stderr=""
            )
        return real_run(command, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(verifier.subprocess, "run", run)

    verify_media_wheel(wheel, MANIFEST, WINDOWS)

    assert len(dependency_commands) == 1
    assert dependency_commands[0][:4] == (
        sys.executable,
        "-m",
        "delvewheel",
        "show",
    )
    assert Path(dependency_commands[0][-1]) != wheel
    assert Path(dependency_commands[0][-1]).name == wheel.name


def test_cli_writes_canonical_report_only_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.media_stack import verifier

    wheel = _wheel_path(tmp_path)
    report_path = tmp_path / "report.json"
    report = VerificationReport(
        identity="identity",
        manifest_sha256="a" * 64,
        target_id="macos-arm64",
        python_tag="cp313",
        wheel_filename=wheel.name,
        wheel_sha256="b" * 64,
        evidence=_evidence(licenses=("LGPL Original Case",)),
    )
    monkeypatch.setattr(verifier, "_verify_input_paths", lambda *_args: report)

    status = main(
        [str(wheel), "--manifest", str(MANIFEST), "--report", str(report_path)]
    )

    assert status == 0
    raw = report_path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert (
        raw == json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")) + "\n"
    )
    assert json.loads(raw)["evidence"]["licenses"] == ["LGPL Original Case"]


def test_cli_reports_bulleted_errors_without_publishing_a_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.media_stack import verifier

    wheel = _wheel_path(tmp_path)
    report_path = tmp_path / "report.json"
    monkeypatch.setattr(verifier, "detect_target", lambda **_kwargs: MACOS)

    status = main(
        [str(wheel), "--manifest", str(MANIFEST), "--report", str(report_path)]
    )

    assert status == 1
    assert capsys.readouterr().err == (
        f"Media stack verification failed:\n- wheel does not exist: {wheel}\n"
    )
    assert not report_path.exists()


def test_cli_publication_failure_preserves_an_existing_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.media_stack import verifier

    wheel = _wheel_path(tmp_path)
    report_path = tmp_path / "report.json"
    report_path.write_text("previous report\n", encoding="utf-8")
    report = VerificationReport(
        identity="identity",
        manifest_sha256="a" * 64,
        target_id="macos-arm64",
        python_tag="cp313",
        wheel_filename=wheel.name,
        wheel_sha256="b" * 64,
        evidence=_evidence(),
    )
    monkeypatch.setattr(verifier, "_verify_input_paths", lambda *_args: report)

    def refuse_replace(_source: Path, _destination: Path) -> None:
        raise OSError("publication refused")

    monkeypatch.setattr(verifier.os, "replace", refuse_replace)

    status = main(
        [str(wheel), "--manifest", str(MANIFEST), "--report", str(report_path)]
    )

    assert status == 1
    assert report_path.read_text(encoding="utf-8") == "previous report\n"
    assert not tuple(tmp_path.glob(".report.json.*.tmp"))
    assert capsys.readouterr().err.endswith("- publication refused\n")


def test_cli_formats_argument_errors_under_the_canonical_heading(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = main([])

    assert status == 1
    assert capsys.readouterr().err == (
        "Media stack verification failed:\n"
        "- the following arguments are required: wheel, --manifest, --report\n"
    )


def test_cli_prefixes_every_physical_diagnostic_line_with_a_bullet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.media_stack import verifier

    def fail(*_args: object, **_kwargs: object) -> VerificationReport:
        raise MediaStackVerificationError(("first diagnostic\nsecond diagnostic",))

    monkeypatch.setattr(verifier, "_verify_input_paths", fail)

    status = main(
        [
            str(tmp_path / "wheel.whl"),
            "--manifest",
            str(MANIFEST),
            "--report",
            str(tmp_path / "report.json"),
        ]
    )

    assert status == 1
    assert capsys.readouterr().err == (
        "Media stack verification failed:\n- first diagnostic\n- second diagnostic\n"
    )


def test_cli_wrapper_is_directly_executable() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_media_stack.py"), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "--manifest" in completed.stdout
    assert completed.stderr == ""
