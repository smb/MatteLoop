#!/usr/bin/env python3
"""Build the unsigned native bundle for the current supported platform."""

from __future__ import annotations

import argparse
import configparser
import contextlib
import importlib.metadata
import platform as platform_module
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST_PATH = ROOT / "dist"

_PINNED_DISTRIBUTIONS = {
    "av": "16.1.0",
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
    parser.parse_args(argv)

    errors = [*prerequisite_errors(), *packaging_input_errors()]
    if errors:
        print("Native build prerequisites are not satisfied:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    deploy_path = deploy_executable()
    artifact = expected_artifact(sys.platform)
    print("Building unsigned native bundle with pyside6-deploy…", flush=True)
    try:
        remove_previous_artifact(sys.platform)
        with temporary_onnxruntime_dylib_alias():
            with tempfile.TemporaryDirectory(
                prefix=".matteloop-build-", dir=ROOT
            ) as raw:
                temporary_spec = Path(raw) / "pysidedeploy.spec"
                prepare_temporary_spec(
                    ROOT / "packaging" / "pysidedeploy.spec",
                    temporary_spec,
                    _distribution_directory("av"),
                    os_name=sys.platform,
                )
                completed = subprocess.run(
                    build_command(deploy_path, temporary_spec), cwd=ROOT, check=False
                )
    except (OSError, RuntimeError) as error:
        print(
            f"Native build preparation or launch failed: {error}",
            file=sys.stderr,
        )
        return 1
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
    smoke = subprocess.run(
        [str(Path(sys.executable)), "packaging/smoke_child.py", "dist"],
        cwd=ROOT,
        check=False,
    )
    if smoke.returncode != 0:
        print(
            "Native build produced a bundle that failed the offline smoke test "
            f"(exit status {smoke.returncode}).",
            file=sys.stderr,
        )
        return smoke.returncode or 1
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
    if distribution == "av":
        return actual == "16.1.0"
    return actual.startswith("6.10.")


def _onnxruntime_capi_directory() -> Path:
    distribution = importlib.metadata.distribution("onnxruntime")
    return Path(distribution.locate_file("onnxruntime/capi"))


def _distribution_directory(name: str) -> Path:
    distribution = importlib.metadata.distribution(name)
    directory = Path(distribution.locate_file(name))
    if not directory.is_dir():
        raise RuntimeError(
            f"installed {name} package directory is missing: {directory}"
        )
    return directory


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


if __name__ == "__main__":
    raise SystemExit(main())
