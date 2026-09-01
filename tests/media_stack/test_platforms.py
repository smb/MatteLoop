from pathlib import Path

import pytest

from scripts.media_stack.platforms import (
    BuildTarget,
    detect_target,
    ffmpeg_commands,
    libwebp_commands,
    pyav_build_command,
    repair_wheel_command,
)

MACOS = BuildTarget("darwin", "arm64", "macos-arm64", "cp313", "13.0")
WINDOWS = BuildTarget("win32", "AMD64", "windows-x64", "cp313", "")


@pytest.mark.parametrize(
    ("os_name", "machine", "expected"),
    [
        ("darwin", "arm64", MACOS),
        ("win32", "AMD64", WINDOWS),
    ],
)
def test_target_detection_accepts_only_supported_platform_pairs(
    os_name: str, machine: str, expected: BuildTarget
) -> None:
    assert (
        detect_target(os_name, machine, python_tag="cp313", deployment_target="13.0")
        == expected
    )


@pytest.mark.parametrize(
    ("os_name", "machine"),
    [("darwin", "x86_64"), ("win32", "arm64"), ("linux", "x86_64")],
)
def test_target_detection_rejects_unsupported_platform_pairs(
    os_name: str, machine: str
) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        detect_target(os_name, machine, python_tag="cp313", deployment_target="13.0")


def test_libwebp_configures_shared_library_without_cli_or_example_targets() -> None:
    commands = libwebp_commands(
        MACOS, Path("libwebp"), Path("build/libwebp"), Path("prefix")
    )
    configure = commands[0]

    assert configure[:3] == ("cmake", "-S", "libwebp")
    assert {
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_SHARED_LIBS=ON",
        "-DWEBP_LINK_STATIC=OFF",
        "-DWEBP_BUILD_ANIM_UTILS=OFF",
        "-DWEBP_BUILD_CWEBP=OFF",
        "-DWEBP_BUILD_DWEBP=OFF",
        "-DWEBP_BUILD_GIF2WEBP=OFF",
        "-DWEBP_BUILD_IMG2WEBP=OFF",
        "-DWEBP_BUILD_VWEBP=OFF",
        "-DWEBP_BUILD_WEBPINFO=OFF",
        "-DWEBP_BUILD_LIBWEBPMUX=ON",
        "-DWEBP_BUILD_WEBPMUX=OFF",
        "-DWEBP_BUILD_EXTRAS=OFF",
    }.issubset(configure)
    assert commands[1] == ("cmake", "--build", "build/libwebp", "--config", "Release")
    assert commands[2] == (
        "cmake",
        "--install",
        "build/libwebp",
        "--config",
        "Release",
    )


def test_libwebp_build_and_install_use_the_same_explicit_config_on_every_target() -> (
    None
):
    # A CMake multi-config generator (Visual Studio, used on Windows) defaults
    # --build to Debug and --install to Release when neither passes --config,
    # so cmake --install fails looking for a Release binary that was never
    # built. Both steps must always name the same, explicit configuration.
    for target in (MACOS, WINDOWS):
        commands = libwebp_commands(
            target, Path("libwebp"), Path("build/libwebp"), Path("prefix")
        )
        build_config = commands[1][commands[1].index("--config") + 1]
        install_config = commands[2][commands[2].index("--config") + 1]
        assert build_config == install_config == "Release"


def test_macos_ffmpeg_configures_shared_lgpl_libraries_against_the_prefix() -> None:
    commands = ffmpeg_commands(
        MACOS, Path("ffmpeg"), Path("build/ffmpeg"), Path("prefix")
    )
    configure = commands[0]
    configure_text = configure[-1]

    assert configure[0] == "env"
    assert "PKG_CONFIG_PATH=prefix/lib/pkgconfig" in configure
    assert "MACOSX_DEPLOYMENT_TARGET=13.0" in configure
    assert "../../ffmpeg/configure" in configure_text
    assert all(
        option in configure_text
        for option in {
            "--disable-static",
            "--enable-shared",
            "--disable-programs",
            "--disable-doc",
            "--disable-autodetect",
            "--disable-gpl",
            "--disable-nonfree",
            "--enable-libwebp",
            "--arch=arm64",
            "--target-os=darwin",
            "--extra-cflags=-Iprefix/include",
            "--extra-ldflags=-Lprefix/lib",
        }
    )
    forbidden = {
        "--enable-gpl",
        "--enable-nonfree",
        "libx264",
        "libx265",
        "libopenh264",
    }
    assert not forbidden.intersection(configure_text.split())


def test_macos_ffmpeg_persists_deployment_target_in_compile_and_link_flags() -> None:
    commands = ffmpeg_commands(
        MACOS, Path("ffmpeg"), Path("build/ffmpeg"), Path("prefix")
    )
    configure_text = commands[0][-1]

    assert "--extra-cflags=-mmacosx-version-min=13.0" in configure_text
    assert "--extra-ldflags=-mmacosx-version-min=13.0" in configure_text


def test_windows_ffmpeg_uses_msys2_and_the_msvc_toolchain() -> None:
    commands = ffmpeg_commands(
        WINDOWS, Path("ffmpeg"), Path("build/ffmpeg"), Path("prefix")
    )
    configure = commands[0]

    assert configure[:2] == ("msys2", "-c")
    assert "--toolchain=msvc" in configure[2]
    assert "../../ffmpeg/configure" in configure[2]
    assert "PKG_CONFIG_PATH=prefix/lib/pkgconfig" in configure[2]
    assert commands[1][:2] == ("msys2", "-c")
    assert commands[2][:2] == ("msys2", "-c")


def test_windows_ffmpeg_does_not_add_macos_deployment_flags() -> None:
    commands = ffmpeg_commands(
        WINDOWS, Path("ffmpeg"), Path("build/ffmpeg"), Path("prefix")
    )
    configure_text = commands[0][2]

    assert "-mmacosx-version-min" not in configure_text


def test_pyav_build_commands_use_the_platform_specific_build_interface() -> None:
    macos = pyav_build_command(
        MACOS, Path("av"), Path("prefix"), Path("wheels"), Path("tool/python")
    )
    windows = pyav_build_command(
        WINDOWS, Path("av"), Path("prefix"), Path("wheels"), Path("tool/python")
    )

    assert macos == (
        "env",
        "PKG_CONFIG_PATH=prefix/lib/pkgconfig",
        "MACOSX_DEPLOYMENT_TARGET=13.0",
        "tool/python",
        "-m",
        "build",
        "--wheel",
        "--no-isolation",
        "--outdir",
        "wheels",
        "av",
    )
    assert windows == (
        "tool/python",
        "av/setup.py",
        "build_ext",
        "--ffmpeg-dir=prefix",
        "bdist_wheel",
        "--dist-dir",
        "wheels",
    )


def test_wheel_repair_uses_the_platform_specific_dependency_tool() -> None:
    wheel = Path("wheels/av.whl")

    assert repair_wheel_command(
        MACOS, wheel, Path("prefix"), Path("repaired"), Path("tool/python")
    ) == ("tool/delocate-wheel", "-w", "repaired", "wheels/av.whl")
    assert repair_wheel_command(
        WINDOWS, wheel, Path("prefix"), Path("repaired"), Path("tool/python")
    ) == (
        "tool/python",
        "-m",
        "delvewheel",
        "repair",
        "--add-path",
        "prefix/bin",
        "--wheel-dir",
        "repaired",
        "wheels/av.whl",
    )
