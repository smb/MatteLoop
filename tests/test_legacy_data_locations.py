import hashlib
import os
from pathlib import Path

import pytest

from matteloop import paths as paths_module
from matteloop.jobs import workspace as workspace_module


def _patch_cache_locations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path]:
    new_base = tmp_path / "matteloop-cache"
    legacy_base = tmp_path / "rembggui-cache"
    bases = {"matteloop": new_base, "rembggui": legacy_base}
    monkeypatch.setattr(
        paths_module,
        "user_cache_dir",
        lambda application: str(bases[application]),
    )
    return new_base / "models", legacy_base / "models"


def test_legacy_model_cache_is_selected_when_new_cache_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    new_root, legacy_root = _patch_cache_locations(monkeypatch, tmp_path)
    legacy_root.mkdir(parents=True)

    assert paths_module.model_cache_root() == legacy_root
    assert not new_root.exists()


def test_new_model_cache_is_preferred_when_both_cache_locations_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    new_root, legacy_root = _patch_cache_locations(monkeypatch, tmp_path)
    new_root.mkdir(parents=True)
    legacy_root.mkdir(parents=True)

    assert paths_module.model_cache_root() == new_root


def test_legacy_workspace_is_discovered_when_new_workspace_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "exports"
    output.mkdir()
    legacy_root = output / ".rembggui-work"
    legacy_root.mkdir()
    monkeypatch.setattr(workspace_module, "_locality_fallback", lambda _path: None)

    layout = workspace_module._workspace_layout(output, create=False)

    assert layout.workspace_root == legacy_root


def test_new_workspace_is_preferred_when_both_workspace_locations_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "exports"
    output.mkdir()
    new_root = output / ".matteloop-work"
    legacy_root = output / ".rembggui-work"
    new_root.mkdir()
    legacy_root.mkdir()
    monkeypatch.setattr(workspace_module, "_locality_fallback", lambda _path: None)

    layout = workspace_module._workspace_layout(output, create=False)

    assert layout.workspace_root == new_root


def test_legacy_compiled_provider_cache_is_adopted_when_new_cache_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_cache_locations(monkeypatch, tmp_path)
    legacy_root = tmp_path / "rembggui-cache" / "coreml-cache"
    legacy_root.mkdir(parents=True)

    assert paths_module.cache_subdirectory("coreml-cache") == legacy_root


def test_new_thumbnail_cache_is_preferred_when_both_locations_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_cache_locations(monkeypatch, tmp_path)
    new_root = tmp_path / "matteloop-cache" / "thumbnails"
    new_root.mkdir(parents=True)
    (tmp_path / "rembggui-cache" / "thumbnails").mkdir(parents=True)

    assert paths_module.cache_subdirectory("thumbnails") == new_root


def test_legacy_fallback_workspace_is_adopted_for_a_nonlocal_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_cache_locations(monkeypatch, tmp_path)
    output = tmp_path / "exports"
    output.mkdir()
    digest = hashlib.sha256(os.fsencode(str(output))).hexdigest()
    legacy_root = tmp_path / "rembggui-cache" / "workspaces" / digest
    legacy_root.mkdir(parents=True)
    monkeypatch.setattr(
        workspace_module,
        "_default_local_filesystem_probe",
        lambda _bound: False,
    )

    layout = workspace_module._workspace_layout(output, create=True)

    assert layout.fallback_used
    assert layout.workspace_root == legacy_root
