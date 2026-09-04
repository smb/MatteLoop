import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.build as native_build
from scripts.build import (
    artifact_size_bytes,
    branding_input_errors,
    build_command,
    expected_artifact,
    prepare_temporary_spec,
    prerequisite_errors,
    remove_previous_artifact,
    temporary_onnxruntime_dylib_alias,
)
from scripts.media_stack.builder import MediaStackArtifacts
from scripts.media_stack.manifest import VerificationContract
from scripts.media_stack.platforms import BuildTarget
from scripts.qt_source import QtSourceCompanion


def _installed_versions(os_name: str = "darwin") -> dict[str, str]:
    runtime = (
        {"onnxruntime-directml": "1.24.4"}
        if os_name == "win32"
        else {"onnxruntime": "1.29.0"}
    )
    return {
        "PySide6": "6.10.3",
        "PySide6_Addons": "6.10.3",
        "PySide6_Essentials": "6.10.3",
        "Nuitka": "2.8.10",
        "shiboken6": "6.10.3",
        **runtime,
    }


MACOS = BuildTarget("darwin", "arm64", "macos-arm64", "cp313", "13.0")
CONTRACT = VerificationContract((), (), (), ("x264", "x265", "openh264"))


def _project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    destination = root / "packaging" / "media-stack" / "manifest.toml"
    destination.parent.mkdir(parents=True)
    shutil.copy2(
        Path(__file__).resolve().parents[1]
        / "packaging"
        / "media-stack"
        / "manifest.toml",
        destination,
    )
    return root


def _wheel(path: Path, *, nested_av: bool = False, native: bool = True) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("av/__init__.py", "")
        if native:
            archive.writestr("av/_core.cpython-313-darwin.so", b"native")
        if nested_av:
            archive.writestr("other/av/__init__.py", "")
            archive.writestr("other/av/_core.pyd", b"native")
    return path


def _fake_onnxruntime_package(tmp_path: Path) -> Path:
    package = tmp_path / "site-packages" / "onnxruntime"
    directml = package / "capi" / "DirectML.dll"
    directml.parent.mkdir(parents=True)
    directml.write_bytes(b"directml")
    return package


def _artifacts(root: Path, *, identity: str = "identity") -> MediaStackArtifacts:
    wheel = _wheel(root / "av-16.1.0.whl")
    provenance = wheel.with_name(f"{wheel.name}.provenance.json")
    manifest = _project_manifest(root)
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    wheel_sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
    provenance_payload = {
        "identity": identity,
        "manifest_sha256": manifest_sha256,
        "python_tag": "cp313",
        "target_id": "macos-arm64",
        "wheel_filename": wheel.name,
        "wheel_sha256": wheel_sha256,
    }
    provenance.write_text(_canonical_json(provenance_payload), encoding="utf-8")
    compliance = root / f"MatteLoop-media-sources-macos-arm64-{identity}.tar.gz"
    compliance.write_bytes(b"exact compliance archive")
    report = root / "verification-report.json"
    report.write_text(_canonical_json(provenance_payload), encoding="utf-8")
    artifact_set = wheel.with_name(f"{wheel.name}.artifact-set.json")
    artifact_set.write_text(
        _canonical_json(
            {
                "compliance_archive": {
                    "filename": compliance.name,
                    "sha256": hashlib.sha256(compliance.read_bytes()).hexdigest(),
                },
                "identity": identity,
                "manifest_sha256": manifest_sha256,
                "provenance": {
                    "filename": provenance.name,
                    "identity": identity,
                    "sha256": hashlib.sha256(provenance.read_bytes()).hexdigest(),
                },
                "python_abi": "cp313",
                "schema_version": 1,
                "target_id": "macos-arm64",
                "verification_report": {
                    "filename": report.name,
                    "identity": identity,
                    "sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                },
                "wheel": {"filename": wheel.name, "sha256": wheel_sha256},
            }
        ),
        encoding="utf-8",
    )
    return MediaStackArtifacts(
        wheel, provenance, compliance, report, artifact_set, identity
    )


def _project_manifest(root: Path) -> Path:
    local = root / "project" / "packaging" / "media-stack" / "manifest.toml"
    if local.is_file():
        return local
    return (
        Path(__file__).resolve().parents[1]
        / "packaging"
        / "media-stack"
        / "manifest.toml"
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"


def _qt_companion(root: Path) -> QtSourceCompanion:
    identity = "qt-identity"
    archive = root / f"MatteLoop-qt-sources-6.10.3-{identity}.tar.gz"
    archive.write_bytes(b"exact Qt source companion")
    checksum = archive.with_name(f"{archive.name}.sha256")
    checksum.write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n",
        encoding="utf-8",
    )
    return QtSourceCompanion(archive, checksum, identity)


def _verified_report(artifacts: MediaStackArtifacts) -> SimpleNamespace:
    provenance = json.loads(artifacts.provenance.read_text(encoding="utf-8"))
    return SimpleNamespace(**provenance)


def test_native_build_rejects_deferred_linux_packaging(tmp_path: Path) -> None:
    errors = prerequisite_errors(
        os_name="linux",
        machine="x86_64",
        python_version=(3, 13),
        deploy_path=tmp_path / "pyside6-deploy",
        installed_versions=_installed_versions(),
    )

    assert errors == (
        "Linux and other platforms are deferred; native packaging supports "
        "macOS arm64 and Windows x64 only.",
        "Missing build prerequisite: pyside6-deploy. Run `uv sync --all-groups` "
        "from the project root.",
    )


def test_native_build_reports_missing_and_wrong_pinned_tools(tmp_path: Path) -> None:
    errors = prerequisite_errors(
        os_name="darwin",
        machine="arm64",
        python_version=(3, 12),
        deploy_path=tmp_path / "pyside6-deploy",
        installed_versions={
            "PySide6": None,
            "PySide6_Addons": "6.10.3",
            "PySide6_Essentials": "6.10.3",
            "Nuitka": "2.8.9",
            "onnxruntime": "1.29.0",
            "shiboken6": "6.10.3",
        },
    )

    assert any("CPython 3.13 is required" in error for error in errors)
    assert any(
        "Missing build prerequisite: pyside6-deploy." in error for error in errors
    )
    assert any("Missing build prerequisite: PySide6." in error for error in errors)
    assert any("Nuitka 2.8.9 is installed" in error for error in errors)


def test_native_build_requires_every_qt_distribution_at_exact_6103(
    tmp_path: Path,
) -> None:
    deploy = tmp_path / "pyside6-deploy"
    deploy.write_bytes(b"tool")
    versions = _installed_versions() | {
        "PySide6": "6.10.4",
        "PySide6_Addons": "6.11.0",
        "PySide6_Essentials": "6.10.2",
        "shiboken6": None,
    }

    errors = prerequisite_errors(
        os_name="darwin",
        machine="arm64",
        python_version=(3, 13),
        deploy_path=deploy,
        installed_versions=versions,
    )

    assert any("PySide6 6.10.4 is installed" in error for error in errors)
    assert any("PySide6_Addons 6.11.0 is installed" in error for error in errors)
    assert any("PySide6_Essentials 6.10.2 is installed" in error for error in errors)
    assert any("Missing build prerequisite: shiboken6" in error for error in errors)


def test_native_build_does_not_require_development_pyav_distribution(
    tmp_path: Path,
) -> None:
    deploy = tmp_path / "pyside6-deploy"
    deploy.write_bytes(b"tool")

    assert (
        prerequisite_errors(
            os_name="darwin",
            machine="arm64",
            python_version=(3, 13),
            deploy_path=deploy,
            installed_versions=_installed_versions(),
        )
        == ()
    )


def test_native_build_requires_windows_directml_distribution(tmp_path: Path) -> None:
    deploy = tmp_path / "pyside6-deploy"
    deploy.write_bytes(b"tool")
    versions = _installed_versions("win32") | {"onnxruntime-directml": None}

    errors = prerequisite_errors(
        os_name="win32",
        machine="AMD64",
        python_version=(3, 13),
        deploy_path=deploy,
        installed_versions=versions,
    )

    assert any(
        "Missing build prerequisite: onnxruntime-directml" in error for error in errors
    )
    assert not any(
        error.startswith("Missing build prerequisite: onnxruntime.") for error in errors
    )


def test_native_build_cli_exposes_verified_media_selection_flags() -> None:
    root = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [sys.executable, "scripts/build.py", "--help"],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--rebuild-media-stack" in completed.stdout
    assert "--media-wheel" in completed.stdout


def test_native_build_identifies_platform_bundle_directories(tmp_path: Path) -> None:
    assert expected_artifact("darwin", tmp_path) == tmp_path / "MatteLoop.app"
    assert expected_artifact("win32", tmp_path) == tmp_path / "MatteLoop.dist"


def test_native_build_uses_configured_entrypoint_without_overriding_the_spec(
    tmp_path: Path,
) -> None:
    command = build_command(tmp_path / "pyside6-deploy", tmp_path / "native.spec")

    assert command == [
        str(tmp_path / "pyside6-deploy"),
        "-c",
        str(tmp_path / "native.spec"),
        "--force",
        "--keep-deployment-files",
    ]


def test_native_build_reports_bundle_size(tmp_path: Path) -> None:
    bundle = tmp_path / "MatteLoop.app"
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    (bundle / "Contents" / "MacOS" / "matteloop").write_bytes(b"bundle")
    (bundle / "Contents" / "Info.plist").write_bytes(b"metadata")

    assert artifact_size_bytes(bundle) == len(b"bundle") + len(b"metadata")


def test_native_build_removes_only_the_current_platform_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "MatteLoop.app"
    bundle.mkdir()
    (bundle / "old-output").write_bytes(b"stale")
    other = tmp_path / "keep.txt"
    other.write_bytes(b"keep")

    remove_previous_artifact("darwin", tmp_path)

    assert not bundle.exists()
    assert other.read_bytes() == b"keep"


def test_native_build_temporarily_repairs_missing_onnxruntime_soname(
    tmp_path: Path,
) -> None:
    versioned = tmp_path / "libonnxruntime.1.29.0.dylib"
    versioned.write_bytes(b"runtime")
    alias = tmp_path / "libonnxruntime.1.dylib"

    with temporary_onnxruntime_dylib_alias(os_name="darwin", capi_directory=tmp_path):
        assert alias.is_symlink()
        assert alias.resolve() == versioned

    assert not alias.exists()
    assert versioned.exists()


def test_native_build_adds_raw_pyav_wheel_only_to_temporary_spec(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.spec"
    destination = tmp_path / "temporary.spec"
    source.write_text("extra_args =\n\t--nofollow-import-to=av\n", encoding="utf-8")
    av_directory = tmp_path / "site-packages" / "av"
    av_directory.mkdir(parents=True)
    extension = av_directory / "audio" / "frame.cpython-313-darwin.so"
    extension.parent.mkdir()
    extension.write_bytes(b"extension")
    module = av_directory / "__init__.py"
    module.write_text("__version__ = 'test'\n", encoding="utf-8")
    dylib = av_directory / ".dylibs" / "libavutil.dylib"
    dylib.parent.mkdir()
    dylib.write_bytes(b"dylib")

    prepare_temporary_spec(source, destination, av_directory, os_name="darwin")

    # Nuitka's --include-data-files/--include-data-dir source is matched as a
    # glob-style pattern, and on Windows a raw backslash-separated path is
    # silently mangled there (each backslash is consumed as an escape
    # character) -- reproduced on the real Windows runner as a "does not
    # match any files" FATAL error citing a garbled path. Forward slashes
    # via as_posix() are required on every platform, not just Windows.
    assert f"--include-data-dir={av_directory.as_posix()}=av" in destination.read_text(
        encoding="utf-8"
    )
    assert (
        f"--include-data-files={extension.as_posix()}"
        "=av/audio/frame.cpython-313-darwin.so"
    ) in destination.read_text(encoding="utf-8")
    assert f"--include-data-files={dylib.as_posix()}=av/.dylibs/libavutil.dylib" in (
        destination.read_text(encoding="utf-8")
    )
    assert (
        f"--include-data-files={module.as_posix()}=av/__init__.py"
    ) in destination.read_text(encoding="utf-8")
    assert "--include-data-dir" not in source.read_text(encoding="utf-8")


def test_native_build_includes_directml_only_in_windows_temporary_spec(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.spec"
    windows_destination = tmp_path / "windows.spec"
    macos_destination = tmp_path / "macos.spec"
    source.write_text("extra_args =\n\t--nofollow-import-to=av\n", encoding="utf-8")
    av_directory = tmp_path / "av"
    av_directory.mkdir()
    (av_directory / "__init__.py").write_text("", encoding="utf-8")
    onnxruntime_directory = tmp_path / "site-packages" / "onnxruntime"
    directml = onnxruntime_directory / "capi" / "DirectML.dll"
    directml.parent.mkdir(parents=True)
    directml.write_bytes(b"directml")

    prepare_temporary_spec(
        source,
        windows_destination,
        av_directory,
        os_name="win32",
        onnxruntime_package_directory=onnxruntime_directory,
    )
    prepare_temporary_spec(
        source,
        macos_destination,
        av_directory,
        os_name="darwin",
        onnxruntime_package_directory=onnxruntime_directory,
    )

    windows_args = windows_destination.read_text(encoding="utf-8")
    macos_args = macos_destination.read_text(encoding="utf-8")
    expected = (
        f"--include-data-files={directml.as_posix()}=onnxruntime/capi/DirectML.dll"
    )
    assert expected in windows_args
    assert expected not in macos_args


def test_native_build_reports_missing_windows_directml_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.spec"
    destination = tmp_path / "temporary.spec"
    source.write_text("extra_args =\n\t--nofollow-import-to=av\n", encoding="utf-8")
    av_directory = tmp_path / "av"
    av_directory.mkdir()
    (av_directory / "__init__.py").write_text("", encoding="utf-8")
    onnxruntime_directory = tmp_path / "onnxruntime"

    with pytest.raises(
        ValueError,
        match="Windows packaging requires onnxruntime/capi/DirectML.dll",
    ):
        prepare_temporary_spec(
            source,
            destination,
            av_directory,
            os_name="win32",
            onnxruntime_package_directory=onnxruntime_directory,
        )


def test_native_build_pyav_spec_paths_never_contain_backslashes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.spec"
    destination = tmp_path / "temporary.spec"
    source.write_text("extra_args =\n\t--nofollow-import-to=av\n", encoding="utf-8")
    av_directory = tmp_path / "site-packages" / "av"
    av_directory.mkdir(parents=True)
    (av_directory / "__init__.py").write_text("", encoding="utf-8")
    (av_directory / "filter").mkdir()
    (av_directory / "filter" / "link.cpython-313-win_amd64.pyd").write_bytes(b"ext")

    prepare_temporary_spec(
        source,
        destination,
        av_directory,
        os_name="win32",
        onnxruntime_package_directory=_fake_onnxruntime_package(tmp_path),
    )

    for line in destination.read_text(encoding="utf-8").splitlines():
        if line.startswith("\t--include-data-"):
            assert "\\" not in line, line


def test_native_build_selects_the_windows_icon_in_temporary_spec(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.spec"
    destination = tmp_path / "temporary.spec"
    source.write_text(
        "[app]\nicon = assets/branding/matteloop/derived/matteloop.icns\n"
        "extra_args =\n\t--nofollow-import-to=av\n",
        encoding="utf-8",
    )
    av_directory = tmp_path / "av"
    av_directory.mkdir()
    (av_directory / "__init__.py").write_text("", encoding="utf-8")

    prepare_temporary_spec(
        source,
        destination,
        av_directory,
        os_name="win32",
        onnxruntime_package_directory=_fake_onnxruntime_package(tmp_path),
    )

    temporary = destination.read_text(encoding="utf-8")
    assert "icon = assets/branding/matteloop/derived/matteloop.ico" in temporary
    assert ".icns" not in temporary


def test_native_build_verifies_committed_matteloop_branding_assets() -> None:
    assert branding_input_errors() == ()


def test_default_media_preparation_uses_verified_builder_output(
    tmp_path: Path,
) -> None:
    root = _project_root(tmp_path)
    artifacts = _artifacts(tmp_path)
    calls: list[tuple[Path, Path, bool]] = []

    def ensure(
        candidate_root: Path, cache: Path, *, force: bool
    ) -> MediaStackArtifacts:
        calls.append((candidate_root, cache, force))
        return artifacts

    prepared = native_build.prepare_media_stack(
        tmp_path / "extracted",
        root=root,
        target=MACOS,
        ensure=ensure,
    )

    assert calls == [(root, root / ".matteloop-build-cache" / "media-stack", False)]
    assert prepared.av_directory == tmp_path / "extracted" / "av"
    assert prepared.compliance_archive == artifacts.compliance_archive
    assert prepared.target == MACOS


def test_rebuild_media_preparation_only_forces_the_verified_builder(
    tmp_path: Path,
) -> None:
    root = _project_root(tmp_path)
    artifacts = _artifacts(tmp_path)
    forced: list[bool] = []

    def ensure(_root: Path, _cache: Path, *, force: bool) -> MediaStackArtifacts:
        forced.append(force)
        return artifacts

    native_build.prepare_media_stack(
        tmp_path / "extracted",
        root=root,
        target=MACOS,
        rebuild=True,
        ensure=ensure,
    )

    assert forced == [True]


def test_explicit_media_wheel_is_verified_and_extracted_without_installing_it(
    tmp_path: Path,
) -> None:
    root = _project_root(tmp_path)
    artifacts = _artifacts(tmp_path, identity="explicit")
    venv_marker = root / ".venv" / "untouched"
    venv_marker.parent.mkdir()
    venv_marker.write_text("development environment", encoding="utf-8")
    verified: list[tuple[Path, Path, BuildTarget]] = []

    def verify(wheel: Path, manifest: Path, target: BuildTarget) -> SimpleNamespace:
        assert wheel.with_name(f"{wheel.name}.provenance.json").is_file()
        verified.append((wheel, manifest, target))
        return _verified_report(artifacts)

    def unexpected_builder(*_args: object, **_kwargs: object) -> MediaStackArtifacts:
        raise AssertionError("explicit wheel unexpectedly requested a rebuild")

    prepared = native_build.prepare_media_stack(
        tmp_path / "extracted",
        root=root,
        media_wheel=artifacts.wheel,
        target=MACOS,
        ensure=unexpected_builder,
        verify=verify,
    )

    assert verified == [
        (
            artifacts.wheel,
            root / "packaging" / "media-stack" / "manifest.toml",
            MACOS,
        )
    ]
    assert prepared.av_directory.is_dir()
    assert venv_marker.read_text(encoding="utf-8") == "development environment"


def test_explicit_media_wheel_rejects_tampered_compliance_archive(
    tmp_path: Path,
) -> None:
    root = _project_root(tmp_path)
    artifacts = _artifacts(tmp_path, identity="explicit")
    artifacts.compliance_archive.write_bytes(b"unbound replacement")

    with pytest.raises(ValueError, match="artifact set"):
        native_build.prepare_media_stack(
            tmp_path / "extracted",
            root=root,
            media_wheel=artifacts.wheel,
            target=MACOS,
            verify=lambda *_args: _verified_report(artifacts),
        )


@pytest.mark.parametrize(
    "field_path",
    (
        "identity",
        "manifest_sha256",
        "python_abi",
        "target_id",
        "wheel.filename",
        "wheel.sha256",
        "provenance.filename",
        "provenance.identity",
        "provenance.sha256",
        "verification_report.filename",
        "verification_report.identity",
        "verification_report.sha256",
        "compliance_archive.filename",
        "compliance_archive.sha256",
    ),
)
def test_explicit_media_wheel_rejects_every_tampered_artifact_set_field(
    tmp_path: Path, field_path: str
) -> None:
    root = _project_root(tmp_path)
    artifacts = _artifacts(tmp_path, identity="explicit")
    binding = artifacts.artifact_set
    payload = json.loads(binding.read_text(encoding="utf-8"))
    parent = payload
    parts = field_path.split(".")
    for part in parts[:-1]:
        parent = parent[part]
    parent[parts[-1]] = "tampered"
    binding.write_text(_canonical_json(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="artifact set"):
        native_build.prepare_media_stack(
            tmp_path / "extracted",
            root=root,
            media_wheel=artifacts.wheel,
            target=MACOS,
            verify=lambda *_args: _verified_report(artifacts),
        )


@pytest.mark.parametrize("nested_av", (False, True))
def test_wheel_extraction_requires_exactly_one_top_level_av_package(
    tmp_path: Path, nested_av: bool
) -> None:
    wheel = tmp_path / "candidate.whl"
    if nested_av:
        _wheel(wheel, nested_av=True)
    else:
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("package/__init__.py", "")

    with pytest.raises(ValueError, match="exactly one top-level av package"):
        native_build.extract_wheel_package(wheel, tmp_path / "extracted")


def test_wheel_extraction_requires_a_native_pyav_extension(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path / "candidate.whl", native=False)

    with pytest.raises(ValueError, match="native .so or .pyd"):
        native_build.extract_wheel_package(wheel, tmp_path / "extracted")


def test_wheel_extraction_accepts_standard_explicit_directory_records(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("av/", b"")
        archive.writestr("av/video/", b"")
        archive.writestr("av/__init__.py", "")
        archive.writestr("av/video/frame.py", "")
        archive.writestr("av/_core.so", b"native")

    package = native_build.extract_wheel_package(wheel, tmp_path / "extracted")

    assert package == tmp_path / "extracted" / "av"
    assert (package / "video" / "frame.py").is_file()


@pytest.mark.parametrize(
    "unsafe_name",
    (
        "../outside.py",
        "/absolute.py",
        "C:\\outside.py",
        "av\\..\\outside.py",
        "av//",
        "av//sub/",
        "av//empty.py",
        "av/./dot.py",
    ),
)
def test_wheel_extraction_rejects_unsafe_member_paths_before_writing(
    tmp_path: Path, unsafe_name: str
) -> None:
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("av/__init__.py", "")
        archive.writestr("av/_core.so", b"native")
        archive.writestr(unsafe_name, b"unsafe")
    destination = tmp_path / "extracted"

    with pytest.raises(ValueError, match="unsafe wheel member path"):
        native_build.extract_wheel_package(wheel, destination)

    assert not destination.exists()
    assert not (tmp_path / "outside.py").exists()


def test_wheel_extraction_rejects_directory_file_collisions_before_writing(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("av/", b"")
        archive.writestr("AV", b"collision")
        archive.writestr("av/__init__.py", "")
        archive.writestr("av/_core.so", b"native")
    destination = tmp_path / "extracted"

    with pytest.raises(ValueError, match="duplicate wheel member path"):
        native_build.extract_wheel_package(wheel, destination)

    assert not destination.exists()


@pytest.mark.parametrize("file_type", (stat.S_IFLNK, stat.S_IFIFO))
def test_wheel_extraction_rejects_links_and_special_files_before_writing(
    tmp_path: Path, file_type: int
) -> None:
    wheel = tmp_path / "candidate.whl"
    unsafe = zipfile.ZipInfo("av/unsafe")
    unsafe.create_system = 3
    unsafe.external_attr = (file_type | 0o777) << 16
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("av/__init__.py", "")
        archive.writestr("av/_core.so", b"native")
        archive.writestr(unsafe, b"target")
    destination = tmp_path / "extracted"

    with pytest.raises(ValueError, match="unsafe wheel member type"):
        native_build.extract_wheel_package(wheel, destination)

    assert not destination.exists()


def test_wheel_extraction_rejects_normalized_case_collisions_before_writing(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("av/__init__.py", "")
        archive.writestr("av/_core.so", b"native")
        archive.writestr("av/module.py", b"first")
        archive.writestr("AV\\MODULE.py", b"second")
    destination = tmp_path / "extracted"

    with pytest.raises(ValueError, match="duplicate wheel member path"):
        native_build.extract_wheel_package(wheel, destination)

    assert not destination.exists()


def test_bundle_media_gate_aggregates_forbidden_and_gpl_entries(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "MatteLoop.app"
    first = artifact / "Contents" / "Frameworks" / "libx264.dylib"
    second = artifact / "Contents" / "MacOS" / "nonfree-codec.dylib"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"forbidden")
    second.write_bytes(b"forbidden")

    errors = native_build.bundle_media_errors(artifact, MACOS, CONTRACT)

    assert len(errors) == 2
    assert any("libx264.dylib" in error for error in errors)
    assert any("nonfree-codec.dylib" in error for error in errors)


def test_finished_bundle_media_failure_skips_smoke_and_reports_every_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact = tmp_path / "dist" / "MatteLoop.app"
    for name in ("libx264.dylib", "nonfree-codec.dylib"):
        path = artifact / "Contents" / "Frameworks" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"forbidden")
    prepared = SimpleNamespace(
        av_directory=tmp_path / "extracted" / "av",
        compliance_archive=tmp_path / "sources.tar.gz",
        target=MACOS,
        contract=CONTRACT,
        identity="identity",
    )
    commands = _stub_native_main(monkeypatch, tmp_path, artifact, prepared)

    assert native_build.main([]) == 1
    assert len(commands) == 1
    stderr = capsys.readouterr().err
    assert "libx264.dylib" in stderr
    assert "nonfree-codec.dylib" in stderr


def test_successful_native_build_uses_extracted_av_and_publishes_compliance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "dist" / "MatteLoop.app"
    executable = artifact / "Contents" / "MacOS" / "matteloop"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"bundle")
    compliance = tmp_path / "MatteLoop-media-sources-macos-arm64-identity.tar.gz"
    compliance.write_bytes(b"exact compliance archive")
    extracted_av = tmp_path / "extracted" / "av"
    qt_companion = _qt_companion(tmp_path)
    prepared = SimpleNamespace(
        av_directory=extracted_av,
        compliance_archive=compliance,
        target=MACOS,
        contract=CONTRACT,
        identity="identity",
    )
    received_av: list[Path] = []
    commands = _stub_native_main(
        monkeypatch,
        tmp_path,
        artifact,
        prepared,
        received_av=received_av,
        qt_companion=qt_companion,
    )

    assert native_build.main([]) == 0
    published = tmp_path / "dist" / compliance.name
    checksum = published.with_name(f"{published.name}.sha256")
    assert received_av == [extracted_av]
    assert len(commands) == 2
    assert published.read_bytes() == compliance.read_bytes()
    assert checksum.read_text(encoding="utf-8") == (
        f"{hashlib.sha256(compliance.read_bytes()).hexdigest()}  {published.name}\n"
    )
    published_qt = artifact.parent / qt_companion.archive.name
    published_qt_checksum = published_qt.with_name(f"{published_qt.name}.sha256")
    assert published_qt.read_bytes() == qt_companion.archive.read_bytes()
    assert published_qt_checksum.read_text(encoding="utf-8") == (
        f"{hashlib.sha256(qt_companion.archive.read_bytes()).hexdigest()}  "
        f"{published_qt.name}\n"
    )


def test_native_build_stops_before_deploy_when_qt_companion_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "dist" / "MatteLoop.app"
    executable = artifact / "Contents" / "MacOS" / "matteloop"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"bundle")
    compliance = tmp_path / "MatteLoop-media-sources-macos-arm64-identity.tar.gz"
    compliance.write_bytes(b"media sources")
    prepared = SimpleNamespace(
        av_directory=tmp_path / "av",
        compliance_archive=compliance,
        target=MACOS,
        contract=CONTRACT,
        identity="identity",
    )
    commands = _stub_native_main(monkeypatch, tmp_path, artifact, prepared)

    def reject(*_args: object, **_kwargs: object) -> QtSourceCompanion:
        raise ValueError("Qt source companion checksum mismatch")

    monkeypatch.setattr(
        native_build, "ensure_qt_source_companion", reject, raising=False
    )

    assert native_build.main([]) == 1
    assert commands == []


def test_native_build_rejects_invalid_qt_companion_before_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "dist" / "MatteLoop.app"
    executable = artifact / "Contents" / "MacOS" / "matteloop"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"bundle")
    compliance = tmp_path / "MatteLoop-media-sources-macos-arm64-identity.tar.gz"
    compliance.write_bytes(b"media sources")
    prepared = SimpleNamespace(
        av_directory=tmp_path / "av",
        compliance_archive=compliance,
        target=MACOS,
        contract=CONTRACT,
        identity="identity",
    )
    qt_companion = _qt_companion(tmp_path)
    qt_companion.checksum.write_text("corrupt checksum\n", encoding="utf-8")
    commands = _stub_native_main(
        monkeypatch,
        tmp_path,
        artifact,
        prepared,
        qt_companion=qt_companion,
    )

    assert native_build.main([]) == 1
    assert commands == []


def test_failed_smoke_does_not_publish_new_compliance_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "dist" / "MatteLoop.app"
    executable = artifact / "Contents" / "MacOS" / "matteloop"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"bundle")
    compliance = tmp_path / "MatteLoop-media-sources-macos-arm64-identity.tar.gz"
    compliance.write_bytes(b"candidate evidence")
    qt_companion = _qt_companion(tmp_path)
    prepared = SimpleNamespace(
        av_directory=tmp_path / "extracted" / "av",
        compliance_archive=compliance,
        target=MACOS,
        contract=CONTRACT,
        identity="identity",
    )
    _stub_native_main(
        monkeypatch,
        tmp_path,
        artifact,
        prepared,
        returncodes=(0, 7),
        qt_companion=qt_companion,
    )

    assert native_build.main([]) == 7
    published = artifact.parent / compliance.name
    assert not published.exists()
    assert not published.with_name(f"{published.name}.sha256").exists()
    assert not tuple(artifact.parent.glob(f".{published.name}.*"))
    published_qt = artifact.parent / qt_companion.archive.name
    assert not published_qt.exists()
    assert not published_qt.with_name(f"{published_qt.name}.sha256").exists()


def test_native_build_success_requires_both_source_checksum_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "dist" / "MatteLoop.app"
    executable = artifact / "Contents" / "MacOS" / "matteloop"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"bundle")
    compliance = tmp_path / "MatteLoop-media-sources-macos-arm64-identity.tar.gz"
    compliance.write_bytes(b"media sources")
    qt_companion = _qt_companion(tmp_path)
    prepared = SimpleNamespace(
        av_directory=tmp_path / "av",
        compliance_archive=compliance,
        target=MACOS,
        contract=CONTRACT,
        identity="identity",
    )
    _stub_native_main(
        monkeypatch,
        tmp_path,
        artifact,
        prepared,
        qt_companion=qt_companion,
    )
    real_publish = native_build.publish_compliance_evidence

    def omit_qt_pair(evidence: object) -> None:
        if "qt-sources" not in str(evidence.archive):
            real_publish(evidence)

    monkeypatch.setattr(native_build, "publish_compliance_evidence", omit_qt_pair)

    assert native_build.main([]) == 1


def test_failed_second_compliance_publication_restores_prior_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "dist" / "MatteLoop.app"
    executable = artifact / "Contents" / "MacOS" / "matteloop"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"bundle")
    compliance = tmp_path / "MatteLoop-media-sources-macos-arm64-identity.tar.gz"
    compliance.write_bytes(b"new archive")
    published = artifact.parent / compliance.name
    checksum = published.with_name(f"{published.name}.sha256")
    published.write_bytes(b"prior archive")
    checksum.write_bytes(b"prior checksum")
    prepared = SimpleNamespace(
        av_directory=tmp_path / "extracted" / "av",
        compliance_archive=compliance,
        target=MACOS,
        contract=CONTRACT,
        identity="identity",
    )
    _stub_native_main(monkeypatch, tmp_path, artifact, prepared)
    real_replace = os.replace

    def fail_checksum_publication(source: object, destination: object) -> None:
        source_path = Path(source)
        if Path(destination) == checksum and source_path.name.endswith(".tmp"):
            raise OSError("injected checksum publication failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_checksum_publication)

    assert native_build.main([]) == 1
    assert published.read_bytes() == b"prior archive"
    assert checksum.read_bytes() == b"prior checksum"
    assert not tuple(artifact.parent.glob(f".{published.name}.*"))
    assert not tuple(artifact.parent.glob(f".{checksum.name}.*"))


def _stub_native_main(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    artifact: Path,
    prepared: SimpleNamespace,
    *,
    received_av: list[Path] | None = None,
    returncodes: tuple[int, ...] = (0, 0),
    qt_companion: QtSourceCompanion | None = None,
) -> list[list[str]]:
    commands: list[list[str]] = []
    monkeypatch.setattr(native_build, "ROOT", root)
    monkeypatch.setattr(native_build, "DIST_PATH", root / "dist")
    monkeypatch.setattr(native_build, "prerequisite_errors", lambda: ())
    monkeypatch.setattr(native_build, "packaging_input_errors", lambda: ())
    monkeypatch.setattr(native_build, "deploy_executable", lambda: root / "deploy")
    monkeypatch.setattr(native_build, "expected_artifact", lambda _os: artifact)
    monkeypatch.setattr(native_build, "remove_previous_artifact", lambda _os: None)
    monkeypatch.setattr(native_build, "prepare_media_stack", lambda *_a, **_k: prepared)
    selected_qt_companion = qt_companion or _qt_companion(root)
    monkeypatch.setattr(
        native_build,
        "ensure_qt_source_companion",
        lambda *_a, **_k: selected_qt_companion,
    )
    monkeypatch.setattr(native_build, "installed_qt_distribution_inventory", lambda: {})
    monkeypatch.setattr(
        native_build,
        "temporary_onnxruntime_dylib_alias",
        lambda: contextlib.nullcontext(),
    )

    def prepare_spec(
        _source: Path, _destination: Path, av: Path, **_kw: object
    ) -> None:
        if received_av is not None:
            received_av.append(av)

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, returncodes[len(commands) - 1])

    monkeypatch.setattr(native_build, "prepare_temporary_spec", prepare_spec)
    monkeypatch.setattr(native_build.subprocess, "run", run)
    return commands


def test_failure_diagnostics_copy_staging_prefix_to_a_fixed_glob_free_path(
    tmp_path: Path,
) -> None:
    # actions/upload-artifact's glob does not match a dot-prefixed directory
    # name like .staging-<uuid> by default and silently uploads nothing; the
    # destination must be a fixed, non-random path CI can reference directly.
    staging_dir = tmp_path / ".staging-deadbeef"
    prefix = staging_dir / "prefix" / "lib"
    prefix.mkdir(parents=True)
    (prefix / "avformat.lib").write_bytes(b"stub")
    log_dir = staging_dir / "build" / "ffmpeg"
    log_dir.mkdir(parents=True)
    (log_dir / "config.log").write_text("configure output", encoding="utf-8")
    root = tmp_path / "repo"
    root.mkdir()
    error = RuntimeError("stage 'pyav' failed")
    error.staging_dir = staging_dir  # type: ignore[attr-defined]

    native_build._copy_failure_diagnostics(error, root=root)

    destination = root / "build-failure-diagnostics"
    assert (destination / "prefix" / "lib" / "avformat.lib").read_bytes() == b"stub"
    assert (destination / "build" / "ffmpeg" / "config.log").read_text(
        encoding="utf-8"
    ) == "configure output"


def test_failure_diagnostics_are_a_noop_without_a_staging_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    native_build._copy_failure_diagnostics(RuntimeError("no staging_dir"), root=root)
    native_build._copy_failure_diagnostics(ValueError("nope"), root=root)

    assert not (root / "build-failure-diagnostics").exists()


def test_native_build_bundles_the_delvewheel_sibling_dll_directory(
    tmp_path: Path,
) -> None:
    # delvewheel stores the FFmpeg DLLs in a sibling av.libs/ directory, not
    # inside av/ the way delocate does; leaving it out gave the Windows
    # bundle "DLL load failed while importing _core" at smoke time.
    source = tmp_path / "source.spec"
    destination = tmp_path / "temporary.spec"
    source.write_text("extra_args =\n\t--nofollow-import-to=av\n", encoding="utf-8")
    av_directory = tmp_path / "extracted" / "av"
    av_directory.mkdir(parents=True)
    (av_directory / "__init__.py").write_text("", encoding="utf-8")
    libs = tmp_path / "extracted" / "av.libs"
    libs.mkdir()
    dll = libs / "avcodec-62-abcdef.dll"
    dll.write_bytes(b"dll")
    (libs / ".load-order-av-16.1.0").write_text("avcodec\n", encoding="utf-8")

    prepare_temporary_spec(
        source,
        destination,
        av_directory,
        os_name="win32",
        onnxruntime_package_directory=_fake_onnxruntime_package(tmp_path),
    )

    temporary = destination.read_text(encoding="utf-8")
    assert f"--include-data-dir={libs.as_posix()}=av.libs" in temporary
    assert (
        f"--include-data-files={dll.as_posix()}=av.libs/avcodec-62-abcdef.dll"
    ) in temporary


def test_deploy_command_keeps_deployment_files_for_recovery() -> None:
    # Without --keep-deployment-files, pyside6-deploy deletes
    # packaging/deployment/ itself right after building, before
    # _recover_long_command_deploy_mismatch ever gets a chance to inspect it.
    command = build_command(Path("pyside6-deploy"))
    assert "--keep-deployment-files" in command


def test_recovers_the_real_nuitka_output_when_pyside_deploy_mislabels_it(
    tmp_path: Path,
) -> None:
    # On Windows, once the assembled Nuitka command line exceeds 7000
    # characters, pyside6-deploy compiles an intermediate deploy_main.py
    # instead of entrypoint.py, so Nuitka's own standalone output is named
    # deploy_main.dist -- but pyside6-deploy's finalize() step still looks
    # for entrypoint.dist and gives up without copying anything. Reproduced
    # on the real Windows runner as "Executable not found ...
    # entrypoint.dist" despite Nuitka itself compiling successfully.
    deployment_staging = tmp_path / "packaging" / "deployment"
    real_output = deployment_staging / "deploy_main.dist"
    (real_output / "nested").mkdir(parents=True)
    (real_output / "matteloop.exe").write_bytes(b"exe")
    (real_output / "nested" / "asset.dat").write_bytes(b"data")
    artifact = tmp_path / "dist" / "MatteLoop.dist"

    native_build._recover_long_command_deploy_mismatch(artifact, deployment_staging)

    assert (artifact / "matteloop.exe").read_bytes() == b"exe"
    assert (artifact / "nested" / "asset.dat").read_bytes() == b"data"


def test_recovery_never_overwrites_an_artifact_pyside_deploy_already_produced(
    tmp_path: Path,
) -> None:
    deployment_staging = tmp_path / "packaging" / "deployment"
    (deployment_staging / "deploy_main.dist").mkdir(parents=True)
    artifact = tmp_path / "dist" / "MatteLoop.dist"
    artifact.mkdir(parents=True)
    (artifact / "matteloop.exe").write_bytes(b"already there")

    native_build._recover_long_command_deploy_mismatch(artifact, deployment_staging)

    assert (artifact / "matteloop.exe").read_bytes() == b"already there"
    assert not (artifact / "deploy_main.dist").exists()


def test_recovery_is_a_noop_without_a_deployment_staging_directory(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "dist" / "MatteLoop.dist"

    native_build._recover_long_command_deploy_mismatch(
        artifact, tmp_path / "packaging" / "deployment"
    )

    assert not artifact.exists()


def test_recovery_refuses_to_guess_between_ambiguous_dist_directories(
    tmp_path: Path,
) -> None:
    deployment_staging = tmp_path / "packaging" / "deployment"
    (deployment_staging / "deploy_main.dist").mkdir(parents=True)
    (deployment_staging / "stale.dist").mkdir(parents=True)
    artifact = tmp_path / "dist" / "MatteLoop.dist"

    native_build._recover_long_command_deploy_mismatch(artifact, deployment_staging)

    assert not artifact.exists()


def test_native_build_recovers_a_mislabeled_bundle_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end wiring check: pyside6-deploy "succeeds" (exit 0) but never
    # creates the expected artifact, because it mislabeled its own output as
    # deploy_main.dist instead of MatteLoop.dist. The recovery step inside
    # _run_native_build must notice, copy the real output into place, and
    # let the rest of the pipeline (media-error scan, compliance, size
    # report) proceed normally.
    artifact = tmp_path / "dist" / "MatteLoop.dist"
    real_output = tmp_path / "packaging" / "deployment" / "deploy_main.dist"
    compliance = tmp_path / "MatteLoop-media-sources-macos-arm64-identity.tar.gz"
    compliance.write_bytes(b"exact compliance archive")
    qt_companion = _qt_companion(tmp_path)
    prepared = SimpleNamespace(
        av_directory=tmp_path / "extracted" / "av",
        compliance_archive=compliance,
        target=MACOS,
        contract=CONTRACT,
        identity="identity",
    )
    _stub_native_main(
        monkeypatch, tmp_path, artifact, prepared, qt_companion=qt_companion
    )

    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if len(calls) == 1:
            # Simulates pyside6-deploy: Nuitka itself succeeds and writes its
            # standalone output to deploy_main.dist, but pyside6-deploy's own
            # finalize() mislooks for entrypoint.dist and never copies
            # anything to the real artifact path.
            real_output.mkdir(parents=True)
            (real_output / "matteloop.exe").write_bytes(b"bundle")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(native_build.subprocess, "run", run)

    assert native_build.main([]) == 0
    assert (artifact / "matteloop.exe").read_bytes() == b"bundle"
    assert not (tmp_path / "packaging" / "deployment").exists()
