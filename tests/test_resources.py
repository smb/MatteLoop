from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import rembggui.resources as resources_module
from rembggui.resources import read_resource_bytes, resource_path


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "../manifest.json",
        "nested/manifest.json",
        r"nested\manifest.json",
        "/manifest.json",
        r"C:\manifest.json",
        "C:manifest.json",
        "NUL",
        "con.json",
        "COM1.txt",
        r"\\.\NUL",
    ],
)
def test_resource_path_rejects_noncanonical_plain_filenames(name: str) -> None:
    with pytest.raises(ValueError, match="plain filename"):
        resource_path(name)


def test_resource_path_resolves_one_explicit_runtime_copy(tmp_path: Path) -> None:
    expected = tmp_path / "resources" / "manifest.json"
    expected.parent.mkdir()
    expected.write_text("{}", encoding="utf-8")

    assert resource_path("manifest.json", runtime_root=tmp_path) == expected
    assert read_resource_bytes("manifest.json", runtime_root=tmp_path) == b"{}"


def test_resource_path_fails_closed_when_resource_is_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="resource not found"):
        resource_path("manifest.json", runtime_root=tmp_path)


def test_resource_path_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    candidate = tmp_path / "runtime" / "resources" / "manifest.json"
    candidate.parent.mkdir(parents=True)
    candidate.symlink_to(outside)

    with pytest.raises(RuntimeError, match="regular non-symlink"):
        resource_path("manifest.json", runtime_root=tmp_path / "runtime")


def test_resource_path_rejects_symlinked_resource_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "manifest.json").write_text("{}", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "resources").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="resource directory must be direct"):
        resource_path("manifest.json", runtime_root=runtime)


def test_resource_read_rejects_file_identity_swap_during_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    candidate = runtime / "resources" / "manifest.json"
    candidate.parent.mkdir(parents=True)
    candidate.write_text('{"version": 1}', encoding="utf-8")
    replacement = tmp_path / "replacement.json"
    replacement.write_text('{"version": 2}', encoding="utf-8")
    actual_open = os.open
    swapped = False

    def swapping_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and Path(path) == candidate:
            replacement.replace(candidate)
            swapped = True
        return actual_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(resources_module.os, "open", swapping_open)

    with pytest.raises(RuntimeError, match="changed during descriptor-bound open"):
        read_resource_bytes("manifest.json", runtime_root=runtime)


def test_resource_path_resolves_source_resource() -> None:
    assert resource_path("model-manifest.json") == (
        Path(__file__).resolve().parents[1] / "resources" / "model-manifest.json"
    )


def test_resource_path_fails_closed_when_source_resource_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_path = tmp_path / "src" / "rembggui" / "resources.py"
    monkeypatch.setattr(resources_module, "__file__", str(module_path))

    with pytest.raises(FileNotFoundError, match="resource not found"):
        resource_path("manifest.json")


def test_resource_path_resolves_frozen_standalone_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "bin" / "rembggui"
    expected = executable.parent / "resources" / "manifest.json"
    expected.parent.mkdir(parents=True)
    expected.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    assert resource_path("manifest.json") == expected


def test_resource_path_fails_closed_when_frozen_resource_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "bin" / "rembggui"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    with pytest.raises(FileNotFoundError, match="resource not found"):
        resource_path("manifest.json")


def test_resource_path_resolves_macos_bundle_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "App.app" / "Contents" / "MacOS" / "rembggui"
    expected = executable.parents[1] / "Resources" / "manifest.json"
    expected.parent.mkdir(parents=True)
    expected.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(resources_module, "__compiled__", object(), raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    assert resource_path("manifest.json") == expected


def test_resource_path_rejects_ambiguous_frozen_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "App.app" / "Contents" / "MacOS" / "rembggui"
    first = executable.parent / "resources" / "manifest.json"
    second = executable.parents[1] / "Resources" / "manifest.json"
    for candidate in (first, second):
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    with pytest.raises(RuntimeError, match="ambiguous resource"):
        resource_path("manifest.json")
