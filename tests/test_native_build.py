from pathlib import Path

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


def _installed_versions() -> dict[str, str]:
    return {
        "av": "16.1.0",
        "PySide6": "6.10.3",
        "Nuitka": "2.8.10",
        "onnxruntime": "1.29.0",
    }


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
            "Nuitka": "2.8.9",
            "onnxruntime": "1.29.0",
        },
    )

    assert any("CPython 3.13 is required" in error for error in errors)
    assert any(
        "Missing build prerequisite: pyside6-deploy." in error for error in errors
    )
    assert any("Missing build prerequisite: PySide6." in error for error in errors)
    assert any("Nuitka 2.8.9 is installed" in error for error in errors)


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

    with temporary_onnxruntime_dylib_alias(
        os_name="darwin", capi_directory=tmp_path
    ):
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

    prepare_temporary_spec(source, destination, av_directory)

    assert f"--include-data-dir={av_directory}=av" in destination.read_text(
        encoding="utf-8"
    )
    assert f"--include-data-files={extension}=av/audio/frame.cpython-313-darwin.so" in (
        destination.read_text(encoding="utf-8")
    )
    assert f"--include-data-files={dylib}=av/.dylibs/libavutil.dylib" in (
        destination.read_text(encoding="utf-8")
    )
    assert f"--include-data-files={module}=av/__init__.py" in destination.read_text(
        encoding="utf-8"
    )
    assert "--include-data-dir" not in source.read_text(encoding="utf-8")


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
        source, destination, av_directory, os_name="win32"
    )

    temporary = destination.read_text(encoding="utf-8")
    assert "icon = assets/branding/matteloop/derived/matteloop.ico" in temporary
    assert ".icns" not in temporary


def test_native_build_verifies_committed_matteloop_branding_assets() -> None:
    assert branding_input_errors() == ()
