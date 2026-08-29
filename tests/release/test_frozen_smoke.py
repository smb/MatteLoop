from __future__ import annotations

import configparser
import importlib.util
import json
import shlex
import socket
import subprocess
import sys
import urllib.request
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from PySide6.QtGui import QImage

from rembggui.jobs.models.catalog import ModelCatalog
from rembggui.smoke import SmokeResult, run_smoke

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_native_smoke_exercises_real_offline_runtime_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("native smoke attempted network access")

    monkeypatch.setattr(socket, "socket", forbidden_network)
    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_network)
    monkeypatch.setattr(subprocess, "Popen", forbidden_network)

    result = run_smoke(tmp_path, use_fake_model=True)

    assert result.qt_platform
    assert "png" in result.qt_image_formats
    assert "webp" in result.qt_image_formats
    assert result.video_decoded
    assert result.webp_frames == 2
    assert result.webp_has_alpha
    assert result.spawn_start_method == "spawn"
    assert result.shared_memory_roundtrip
    assert result.shared_memory_unlinked
    assert result.fake_session_used
    assert 1 <= result.peak_full_res_rgba_owners <= 3
    assert result.to_primitives()["peak_full_res_rgba_owners"] == (
        result.peak_full_res_rgba_owners
    )
    assert not tuple(tmp_path.iterdir())
    with pytest.raises(FrozenInstanceError):
        result.webp_frames = 3  # type: ignore[misc]


def test_smoke_result_contract_is_typed_and_immutable() -> None:
    assert SmokeResult.__dataclass_params__.frozen is True
    assert SmokeResult.__slots__


def test_smoke_result_exposes_the_brief_level_boundary_summary() -> None:
    result = SmokeResult(
        qt_platform="offscreen",
        qt_image_formats=("png", "webp"),
        video_decoded=True,
        webp_frames=2,
        webp_has_alpha=True,
        spawn_start_method="spawn",
        shared_memory_roundtrip=True,
        shared_memory_unlinked=True,
        fake_session_used=True,
        peak_full_res_rgba_owners=3,
    )

    assert result.qt
    assert result.pyav
    assert result.webp
    assert result.spawn
    assert result.shared_memory
    assert result.peak_rgba_frames == 3


def test_native_smoke_roundtrips_png_and_webp_through_qt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved_formats: list[str] = []
    original_save = QImage.save

    def observed_save(self: QImage, *args: object, **kwargs: object) -> bool:
        if len(args) >= 2 and isinstance(args[1], str):
            saved_formats.append(args[1].lower())
        return original_save(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(QImage, "save", observed_save)

    run_smoke(tmp_path, use_fake_model=False)

    assert saved_formats == ["png", "webp"]


def test_catalog_resources_resolve_from_a_frozen_runtime_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resource_dir = tmp_path / "resources"
    resource_dir.mkdir()
    source_dir = REPOSITORY_ROOT / "resources"
    for name in ("model-manifest.json", "model-provenance.json"):
        (resource_dir / name).write_bytes((source_dir / name).read_bytes())

    assert ModelCatalog.resource_path(runtime_root=tmp_path) == (
        resource_dir / "model-manifest.json"
    )
    assert ModelCatalog.provenance_path(runtime_root=tmp_path) == (
        resource_dir / "model-provenance.json"
    )
    assert ModelCatalog.load_resource(runtime_root=tmp_path).ids
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "rembggui"))
    assert ModelCatalog.resource_path() == resource_dir / "model-manifest.json"
    assert ModelCatalog.provenance_path() == resource_dir / "model-provenance.json"


def test_pyside_deploy_spec_parses_to_required_native_bundle_contract() -> None:
    spec_path = REPOSITORY_ROOT / "packaging" / "pysidedeploy.spec"
    parser = configparser.ConfigParser(
        comment_prefixes=("/",), strict=False, allow_no_value=True
    )
    parser.read(spec_path, encoding="utf-8")

    assert parser.get("app", "title") == "rembgGUI"
    assert parser.get("app", "project_dir") == "."
    assert parser.get("app", "input_file") == "packaging/entrypoint.py"
    assert parser.get("app", "exec_directory") == "dist"
    assert parser.get("python", "packages") == "Nuitka==2.8.10"
    assert set(parser.get("qt", "plugins").split(",")) == {
        "imageformats",
        "platforms",
    }
    assert parser.get("nuitka", "mode") == "standalone"

    args = set(shlex.split(parser.get("nuitka", "extra_args")))
    assert "--include-package=rembggui" in args
    assert "--include-module=rembggui.smoke_child" in args
    assert "--include-package=rembg.sessions" in args
    assert "--include-package=onnxruntime" in args
    assert "--include-package=av" in args
    assert "--include-package-data=av" in args
    assert "--include-module=PIL.PngImagePlugin" in args
    assert "--include-module=PIL.WebPImagePlugin" in args
    assert (
        "--include-data-files=resources/model-manifest.json="
        "resources/model-manifest.json"
    ) in args
    assert (
        "--include-data-files=resources/model-provenance.json="
        "resources/model-provenance.json"
    ) in args
    assert "--nofollow-import-to=tests" in args
    assert "--noinclude-data-files=**/*.onnx" in args
    assert "--noinclude-data-files=**/tests/**" in args
    assert "--noinclude-data-files=**/*token*" in args


def test_pyside_deploy_accepts_spec_in_dry_run_mode() -> None:
    executable_name = (
        "pyside6-deploy.exe" if sys.platform == "win32" else "pyside6-deploy"
    )
    deploy = Path(sys.executable).with_name(executable_name)
    assert deploy.is_file()
    completed = subprocess.run(
        [
            str(deploy),
            "-c",
            str(REPOSITORY_ROOT / "packaging" / "pysidedeploy.spec"),
            "--dry-run",
            "--force",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    command = completed.stdout + completed.stderr
    assert "--enable-plugin=pyside6" in command
    assert "--mode=standalone" in command or "--standalone" in command


def test_release_workflow_has_only_manual_unsigned_native_builds() -> None:
    workflow_path = REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))

    assert workflow["on"] == {"workflow_dispatch": {}}
    job = workflow["jobs"]["native-package"]
    includes = job["strategy"]["matrix"]["include"]
    assert {(item["os"], item["arch"]) for item in includes} == {
        ("windows-2022", "x64"),
        ("macos-15-intel", "x64"),
        ("macos-15", "arm64"),
        ("ubuntu-22.04", "x64"),
    }
    assert job["permissions"] == {"contents": "read"}

    steps = job["steps"]
    setup = next(step for step in steps if step.get("id") == "setup-uv")
    assert setup["with"] == {
        "enable-cache": False,
        "python-version": "3.13",
        "version": "0.11.32",
    }
    commands = "\n".join(str(step.get("run", "")) for step in steps)
    assert "uv sync --frozen --all-groups --no-cache" in commands
    assert (
        "pyside6-deploy packaging/entrypoint.py -c packaging/pysidedeploy.spec --force"
    ) in commands
    assert "packaging/smoke_child.py" in commands
    assert "xvfb-run" in commands

    upload = next(step for step in steps if step.get("id") == "upload")
    assert upload["with"]["name"] == "rembgGUI-unsigned-${{ matrix.target }}"
    assert upload["with"]["path"] == "dist"
    serialized = json.dumps(workflow).lower()
    assert "secrets." not in serialized
    assert "codesign" not in serialized
    assert "signing" not in serialized
    assert "notar" not in serialized
    assert "publish" not in serialized


def test_packaging_smoke_launcher_is_importable_without_shadowing_packaging() -> None:
    launcher_path = REPOSITORY_ROOT / "packaging" / "smoke_child.py"
    spec = importlib.util.spec_from_file_location(
        "rembggui_packaging_smoke", launcher_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.spawn_smoke_target.__module__ == "rembggui.smoke_child"
