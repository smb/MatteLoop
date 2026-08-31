"""Deterministic source and frozen-runtime resource discovery."""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from math import ceil, isfinite
from pathlib import Path, PureWindowsPath

_RESOURCE_DIRECTORY = "resources"
_MAX_RESOURCE_BYTES = 256 * 1024
_STATUS_ICON_SIZES = (24, 32, 48, 64)
_STATUS_ICON_NAMES = frozenset({"error", "stale", "preview"})
_WINDOWS_DEVICE_STEMS = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)


@dataclass(frozen=True, slots=True)
class StatusIconAsset:
    """One validated status icon file and its physical pixel size."""

    path: Path
    pixel_size: int


def status_icon_asset(
    name: str,
    requested_size: int,
    *,
    device_pixel_ratio: float = 1.0,
    runtime_root: Path | None = None,
) -> StatusIconAsset:
    """Resolve a packaged status icon at or above its requested pixel size.

    The logical minimum is 24 pixels.  On a high-DPI display the requested
    size is converted to physical pixels before choosing the closest bundled
    asset, so a 2x display selects the 48-pixel source for a 24-pixel icon.
    """
    if name not in _STATUS_ICON_NAMES:
        raise ValueError("unknown status icon")
    if type(requested_size) is not int or requested_size <= 0:
        raise ValueError("requested icon size must be a positive integer")
    if not isfinite(device_pixel_ratio) or device_pixel_ratio <= 0:
        raise ValueError("device pixel ratio must be positive and finite")

    logical_size = max(24, requested_size)
    physical_size = ceil(logical_size * max(1.0, device_pixel_ratio))
    pixel_size = next(
        (size for size in _STATUS_ICON_SIZES if size >= physical_size),
        _STATUS_ICON_SIZES[-1],
    )
    directories = _resource_directories(runtime_root)
    existing: list[Path] = []
    for directory in directories:
        icon_directory = directory / "icons"
        candidate = icon_directory / f"{name}-{pixel_size}.png"
        if _is_direct_regular_resource(icon_directory, candidate):
            existing.append(candidate)
    if not existing:
        raise FileNotFoundError(f"status icon not found: {name}-{pixel_size}.png")
    if len(existing) > 1:
        locations = ", ".join(str(candidate) for candidate in existing)
        raise RuntimeError(f"ambiguous status icon {name!r}: {locations}")
    return StatusIconAsset(existing[0], pixel_size)


def resource_path(name: str, *, runtime_root: Path | None = None) -> Path:
    """Resolve one packaged resource without guessing through parent depths.

    ``runtime_root`` is an explicit seam for frozen-runtime validation. Nuitka
    standalone binaries resolve data relative to the executable; source runs
    resolve data relative to the repository's stable ``src`` layout.

    The returned path is for trusted, read-only packaged resources. Call
    :func:`read_resource_bytes` when content must stay bound to the validated
    file identity through the read.
    """
    _validate_resource_name(name)

    directories = _resource_directories(runtime_root)
    candidates = tuple(directory / name for directory in directories)
    existing = tuple(
        candidate
        for directory, candidate in zip(directories, candidates, strict=True)
        if _is_direct_regular_resource(directory, candidate)
    )
    if not existing:
        raise FileNotFoundError(f"resource not found: {name}")
    if len(existing) > 1:
        locations = ", ".join(str(candidate) for candidate in existing)
        raise RuntimeError(f"ambiguous resource {name!r}: {locations}")
    return existing[0]


def read_resource_bytes(name: str, *, runtime_root: Path | None = None) -> bytes:
    """Read a small packaged resource from its validated file descriptor."""
    path = resource_path(name, runtime_root=runtime_root)
    descriptor = _open_validated_resource(path)
    try:
        size = os.fstat(descriptor).st_size
        if not 0 <= size <= _MAX_RESOURCE_BYTES:
            raise RuntimeError(f"packaged resource is too large: {name}")
        remaining = size + 1
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > _MAX_RESOURCE_BYTES:
            raise RuntimeError(f"packaged resource is too large: {name}")
        return content
    finally:
        os.close(descriptor)


def _is_direct_regular_resource(directory: Path, candidate: Path) -> bool:
    try:
        directory_status = directory.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise RuntimeError(
            f"resource directory could not be inspected: {error}"
        ) from error
    if stat.S_ISLNK(directory_status.st_mode) or not stat.S_ISDIR(
        directory_status.st_mode
    ):
        raise RuntimeError("resource directory must be direct and regular")

    try:
        candidate_status = candidate.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise RuntimeError(
            f"resource candidate could not be inspected: {error}"
        ) from error
    if stat.S_ISLNK(candidate_status.st_mode) or not stat.S_ISREG(
        candidate_status.st_mode
    ):
        raise RuntimeError("resource must be a regular non-symlink file")

    try:
        resolved_directory = directory.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RuntimeError(f"resource path could not be resolved: {error}") from error
    if resolved_candidate.parent != resolved_directory:
        raise RuntimeError("resource path escapes resource directory")
    return True


def _open_validated_resource(path: Path) -> int:
    directory = path.parent
    try:
        expected_directory = directory.lstat()
        expected_file = path.lstat()
    except OSError as error:
        raise RuntimeError(f"packaged resource changed before open: {error}") from error

    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | close_on_exec | no_follow
    descriptor: int | None = None
    directory_descriptor: int | None = None
    descriptor_transferred = False
    try:
        if os.open in os.supports_dir_fd:
            directory_flags = (
                os.O_RDONLY | close_on_exec | no_follow | getattr(os, "O_DIRECTORY", 0)
            )
            directory_descriptor = os.open(directory, directory_flags)
            if not _same_identity(os.fstat(directory_descriptor), expected_directory):
                raise RuntimeError("packaged resource directory changed during open")
            descriptor = os.open(path.name, file_flags, dir_fd=directory_descriptor)
        else:  # pragma: no cover - exercised by Windows native packaging
            descriptor = os.open(path, file_flags)
            if not _same_identity(directory.lstat(), expected_directory):
                raise RuntimeError("packaged resource directory changed during open")
        actual_file = os.fstat(descriptor)
        if not stat.S_ISREG(actual_file.st_mode) or not _same_identity(
            actual_file, expected_file
        ):
            raise RuntimeError("packaged resource changed during descriptor-bound open")
        descriptor_transferred = True
        return descriptor
    except OSError as error:
        raise RuntimeError(
            f"packaged resource could not be opened safely: {error}"
        ) from error
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)
        if descriptor is not None and not descriptor_transferred:
            os.close(descriptor)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _validate_resource_name(name: str) -> None:
    if type(name) is not str:
        raise ValueError("resource name must be one plain filename")
    windows_name = PureWindowsPath(name)
    device_stem = name.split(".", maxsplit=1)[0].casefold()
    if (
        not name
        or name in {".", ".."}
        or name != name.strip()
        or name.endswith(".")
        or not name.isprintable()
        or "/" in name
        or "\\" in name
        or ":" in name
        or Path(name).is_absolute()
        or windows_name.is_absolute()
        or bool(windows_name.drive)
        or bool(windows_name.root)
        or device_stem in _WINDOWS_DEVICE_STEMS
    ):
        raise ValueError("resource name must be one canonical plain filename")


def _resource_directories(runtime_root: Path | None) -> tuple[Path, ...]:
    if runtime_root is not None:
        return (Path(runtime_root).resolve() / _RESOURCE_DIRECTORY,)
    if getattr(sys, "frozen", False) or globals().get("__compiled__") is not None:
        executable_dir = Path(sys.executable).resolve().parent
        directories = [executable_dir / _RESOURCE_DIRECTORY]
        if executable_dir.name == "MacOS" and executable_dir.parent.name == "Contents":
            directories.append(executable_dir.parent / "Resources")
        return tuple(directories)
    source_directory = Path(__file__).resolve().parents[2] / _RESOURCE_DIRECTORY
    package_directory = Path(__file__).resolve().parent / _RESOURCE_DIRECTORY
    if package_directory == source_directory:
        return (source_directory,)
    return (source_directory, package_directory)
