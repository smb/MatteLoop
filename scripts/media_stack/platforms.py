"""Pure native media-stack command construction for supported build targets."""

import os
import platform
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BuildTarget:
    os_name: str
    machine: str
    target_id: str
    python_tag: str
    deployment_target: str


def detect_target(
    os_name: str | None = None,
    machine: str | None = None,
    *,
    python_tag: str,
    deployment_target: str,
) -> BuildTarget:
    """Describe the running host when it is a supported native build target."""
    current_os = sys.platform if os_name is None else os_name
    current_machine = platform.machine() if machine is None else machine
    if (current_os, current_machine) == ("darwin", "arm64"):
        return BuildTarget(
            current_os,
            current_machine,
            "macos-arm64",
            python_tag,
            deployment_target,
        )
    if (current_os, current_machine) == ("win32", "AMD64"):
        return BuildTarget(
            current_os,
            current_machine,
            "windows-x64",
            python_tag,
            "",
        )
    raise ValueError(f"unsupported media-stack host: {current_os}/{current_machine}")


def libwebp_commands(
    target: BuildTarget, source_dir: Path, build_dir: Path, prefix: Path
) -> tuple[tuple[str, ...], ...]:
    """Return the CMake commands for a shared, library-only libwebp build."""
    configure = [
        "cmake",
        "-S",
        str(source_dir),
        "-B",
        str(build_dir),
        f"-DCMAKE_INSTALL_PREFIX={prefix}",
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
    ]
    if target.target_id == "macos-arm64":
        configure.extend(
            (
                "-DCMAKE_OSX_ARCHITECTURES=arm64",
                f"-DCMAKE_OSX_DEPLOYMENT_TARGET={target.deployment_target}",
            )
        )
    return (
        tuple(configure),
        ("cmake", "--build", str(build_dir)),
        ("cmake", "--install", str(build_dir)),
    )


def ffmpeg_commands(
    target: BuildTarget, source_dir: Path, build_dir: Path, prefix: Path
) -> tuple[tuple[str, ...], ...]:
    """Return commands for an LGPL shared FFmpeg build against only libwebp."""
    configure = _ffmpeg_configure_arguments(target, source_dir, build_dir, prefix)
    if target.target_id == "windows-x64":
        configured = (
            "env",
            f"PKG_CONFIG_PATH={prefix / 'lib' / 'pkgconfig'}",
            *configure,
        )
        return (
            ("msys2", "-c", _in_directory(build_dir, configured)),
            ("msys2", "-c", _shell_command(("make", "-C", str(build_dir)))),
            (
                "msys2",
                "-c",
                _shell_command(("make", "-C", str(build_dir), "install")),
            ),
        )
    environment = (
        "env",
        f"PKG_CONFIG_PATH={prefix / 'lib' / 'pkgconfig'}",
        f"MACOSX_DEPLOYMENT_TARGET={target.deployment_target}",
    )
    return (
        (*environment, "sh", "-c", _in_directory(build_dir, configure)),
        ("make", "-C", str(build_dir)),
        ("make", "-C", str(build_dir), "install"),
    )


def pyav_build_command(
    target: BuildTarget,
    source_dir: Path,
    prefix: Path,
    output_dir: Path,
    python: Path,
) -> tuple[str, ...]:
    """Return the pinned-tool-environment command that produces a PyAV wheel."""
    if target.target_id == "windows-x64":
        return (
            str(python),
            str(source_dir / "setup.py"),
            "build_ext",
            f"--ffmpeg-dir={prefix}",
            "bdist_wheel",
            "--dist-dir",
            str(output_dir),
        )
    environment = ["env", f"PKG_CONFIG_PATH={prefix / 'lib' / 'pkgconfig'}"]
    if target.target_id == "macos-arm64":
        environment.append(f"MACOSX_DEPLOYMENT_TARGET={target.deployment_target}")
    return (
        *environment,
        str(python),
        "-m",
        "build",
        "--wheel",
        "--no-isolation",
        "--outdir",
        str(output_dir),
        str(source_dir),
    )


def repair_wheel_command(
    target: BuildTarget,
    wheel: Path,
    prefix: Path,
    output_dir: Path,
    python: Path,
) -> tuple[str, ...]:
    """Return the target-specific command that bundles native wheel dependencies."""
    if target.target_id == "windows-x64":
        return (
            str(python),
            "-m",
            "delvewheel",
            "repair",
            "--add-path",
            str(prefix / "bin"),
            "--wheel-dir",
            str(output_dir),
            str(wheel),
        )
    return (
        str(python.parent / "delocate-wheel"),
        "-w",
        str(output_dir),
        str(wheel),
    )


def _ffmpeg_configure_arguments(
    target: BuildTarget, source_dir: Path, build_dir: Path, prefix: Path
) -> tuple[str, ...]:
    arguments = [
        Path(os.path.relpath(source_dir / "configure", start=build_dir)).as_posix(),
        f"--prefix={prefix}",
        "--disable-static",
        "--enable-shared",
        "--disable-programs",
        "--disable-doc",
        "--disable-autodetect",
        "--disable-gpl",
        "--disable-nonfree",
        "--enable-libwebp",
        f"--extra-cflags=-I{prefix / 'include'}",
        f"--extra-ldflags=-L{prefix / 'lib'}",
    ]
    if target.target_id == "macos-arm64":
        arguments.extend(("--arch=arm64", "--target-os=darwin"))
    if target.target_id == "windows-x64":
        arguments.append("--toolchain=msvc")
    return tuple(arguments)


def _in_directory(directory: Path, command: tuple[str, ...]) -> str:
    return f"cd {shlex.quote(str(directory))} && exec {_shell_command(command)}"


def _shell_command(command: tuple[str, ...]) -> str:
    return shlex.join(command)
