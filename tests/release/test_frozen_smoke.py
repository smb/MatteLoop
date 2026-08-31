from __future__ import annotations

import configparser
import importlib.util
import json
import shlex
import shutil
import socket
import subprocess
import sys
import tomllib
import urllib.request
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from PySide6.QtGui import QImage

import matteloop.smoke as smoke_module
from matteloop.jobs.models.catalog import ModelCatalog
from matteloop.smoke import SmokeResult, run_smoke

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_native_smoke_exercises_real_offline_runtime_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert smoke_module._SMOKE_VIDEO_ENCODER == "mpeg4"

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


def test_native_smoke_resolves_every_v1_session_class_without_weights(
    tmp_path: Path,
) -> None:
    """The bundle excludes rembg.bg, so session loading needs its own evidence.

    The smoke otherwise runs a fake session, which would let a build that cannot
    resolve a single real model still report ok.
    """
    from matteloop.core.parameters import V1_MODEL_IDS
    from matteloop.jobs.rembg_runtime import (
        V1_SESSION_MODULE_COUNT,
        load_rembg_session_classes,
    )
    from matteloop.jobs.segmentation_host import _resolve_rembg_session_class

    result = run_smoke(tmp_path, use_fake_model=True)

    assert result.rembg_session_classes == V1_SESSION_MODULE_COUNT
    assert V1_SESSION_MODULE_COUNT == len(V1_MODEL_IDS)

    catalog = ModelCatalog.load_resource()
    classes = load_rembg_session_classes()
    for model_id in V1_MODEL_IDS:
        assert _resolve_rembg_session_class(
            model_id,
            classes,
            catalog.rembg_version,
            catalog.rembg_version,
        ) is not None


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
        rembg_session_classes=13,
        peak_full_res_rgba_owners=3,
    )

    assert result.qt
    assert result.pyav
    assert result.webp
    assert result.spawn
    assert result.shared_memory
    assert result.peak_rgba_frames == 3


def test_pipe_construction_failure_closes_and_unlinks_created_shared_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSharedMemory:
        names: set[str] = set()
        created_name = "smoke-segment"
        close_calls = 0
        unlink_calls = 0

        def __init__(
            self,
            name: str | None = None,
            create: bool = False,
            size: int = 0,
        ) -> None:
            del size
            self.name = name or self.created_name
            if create:
                self.names.add(self.name)
            elif self.name not in self.names:
                raise FileNotFoundError(self.name)

        def close(self) -> None:
            type(self).close_calls += 1

        def unlink(self) -> None:
            type(self).unlink_calls += 1
            if self.name not in self.names:
                raise FileNotFoundError(self.name)
            self.names.remove(self.name)

    class FailingPipeContext:
        def Pipe(self, duplex: bool) -> tuple[object, object]:
            del duplex
            raise RuntimeError("pipe construction failed")

    monkeypatch.setattr(smoke_module, "SharedMemory", FakeSharedMemory)
    monkeypatch.setattr(
        smoke_module.multiprocessing,
        "get_context",
        lambda method: FailingPipeContext(),
    )

    with pytest.raises(RuntimeError, match="pipe construction failed"):
        smoke_module._run_spawn_shared_memory(use_fake_model=True)

    with pytest.raises(FileNotFoundError):
        FakeSharedMemory(name=FakeSharedMemory.created_name)
    assert FakeSharedMemory.close_calls == 1
    assert FakeSharedMemory.unlink_calls == 1


def test_pipe_failure_preserves_primary_and_reports_all_cleanup_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CleanupFailingSharedMemory:
        name = "smoke-segment"

        def __init__(self, create: bool, size: int) -> None:
            del create, size

        def close(self) -> None:
            raise RuntimeError("close failed")

        def unlink(self) -> None:
            raise RuntimeError("unlink failed")

    class FailingPipeContext:
        def Pipe(self, duplex: bool) -> tuple[object, object]:
            del duplex
            raise RuntimeError("pipe construction failed")

    monkeypatch.setattr(
        smoke_module,
        "SharedMemory",
        CleanupFailingSharedMemory,
    )
    monkeypatch.setattr(
        smoke_module.multiprocessing,
        "get_context",
        lambda method: FailingPipeContext(),
    )

    with pytest.raises(RuntimeError, match="pipe construction failed") as exc:
        smoke_module._run_spawn_shared_memory(use_fake_model=True)

    assert exc.value.__notes__ == [
        "shared-memory close failed: RuntimeError: close failed",
        "shared-memory unlink failed: RuntimeError: unlink failed",
    ]


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("send", "send close failed"),
        ("recv", "receive failed"),
        ("child", "spawned smoke child failed"),
        ("timeout", "timed out"),
    ],
)
def test_post_start_failures_close_every_handle_and_stop_the_child(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    message: str,
) -> None:
    class FakeSharedMemory:
        names: set[str] = set()
        name = "smoke-segment"
        close_calls = 0
        unlink_calls = 0

        def __init__(
            self,
            name: str | None = None,
            create: bool = False,
            size: int = 0,
        ) -> None:
            self.name = name or type(self).name
            if create:
                self.names.add(self.name)
                self.buf = memoryview(bytearray(size))
            elif self.name not in self.names:
                raise FileNotFoundError(self.name)

        def close(self) -> None:
            type(self).close_calls += 1

        def unlink(self) -> None:
            type(self).unlink_calls += 1
            self.names.remove(self.name)

    class FakeEndpoint:
        def __init__(self, *, receiving: bool) -> None:
            self.receiving = receiving
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            if failure == "send" and not self.receiving and self.close_calls == 1:
                raise RuntimeError("send close failed")

        def poll(self, timeout: float) -> bool:
            del timeout
            return failure != "timeout"

        def recv_bytes(self) -> bytes:
            if failure == "recv":
                raise RuntimeError("receive failed")
            return json.dumps(
                {"error": "child failed"}
                if failure == "child"
                else {"input_sha256": "unused", "output_sha256": "unused"}
            ).encode()

    class FakeProcess:
        alive = False
        terminate_calls = 0
        join_calls = 0
        close_calls = 0
        exitcode = 0

        def start(self) -> None:
            type(self).alive = True

        def is_alive(self) -> bool:
            return type(self).alive

        def terminate(self) -> None:
            type(self).terminate_calls += 1
            type(self).alive = False

        def kill(self) -> None:
            type(self).alive = False

        def join(self, timeout: float) -> None:
            del timeout
            type(self).join_calls += 1
            if failure == "child":
                type(self).alive = False

        def close(self) -> None:
            type(self).close_calls += 1

    receive = FakeEndpoint(receiving=True)
    send = FakeEndpoint(receiving=False)

    class FakeContext:
        def Pipe(self, duplex: bool) -> tuple[FakeEndpoint, FakeEndpoint]:
            del duplex
            return receive, send

        def Process(self, **kwargs: object) -> FakeProcess:
            del kwargs
            return FakeProcess()

    monkeypatch.setattr(smoke_module, "SharedMemory", FakeSharedMemory)
    monkeypatch.setattr(
        smoke_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    with pytest.raises(RuntimeError, match=message):
        smoke_module._run_spawn_shared_memory(use_fake_model=True)

    assert receive.close_calls == 1
    assert send.close_calls == (2 if failure == "send" else 1)
    assert FakeProcess.terminate_calls == (0 if failure == "child" else 1)
    assert FakeProcess.join_calls >= 1
    assert FakeProcess.close_calls == 1
    assert FakeSharedMemory.close_calls == 1
    assert FakeSharedMemory.unlink_calls == 1
    assert not FakeSharedMemory.names


def test_process_start_failure_closes_endpoints_process_and_shared_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSharedMemory:
        names = {"smoke-segment"}
        name = "smoke-segment"
        close_calls = 0
        unlink_calls = 0

        def __init__(
            self,
            name: str | None = None,
            create: bool = False,
            size: int = 0,
        ) -> None:
            self.name = name or type(self).name
            if create:
                self.buf = memoryview(bytearray(size))
            elif self.name not in self.names:
                raise FileNotFoundError(self.name)

        def close(self) -> None:
            type(self).close_calls += 1

        def unlink(self) -> None:
            type(self).unlink_calls += 1
            self.names.remove(self.name)

    class FakeEndpoint:
        close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    class StartFailingProcess:
        close_calls = 0

        def start(self) -> None:
            raise RuntimeError("start failed")

        def close(self) -> None:
            type(self).close_calls += 1

    receive = FakeEndpoint()
    send = FakeEndpoint()

    class FakeContext:
        def Pipe(self, duplex: bool) -> tuple[FakeEndpoint, FakeEndpoint]:
            del duplex
            return receive, send

        def Process(self, **kwargs: object) -> StartFailingProcess:
            del kwargs
            return StartFailingProcess()

    monkeypatch.setattr(smoke_module, "SharedMemory", FakeSharedMemory)
    monkeypatch.setattr(
        smoke_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    with pytest.raises(RuntimeError, match="start failed"):
        smoke_module._run_spawn_shared_memory(use_fake_model=True)

    assert receive.close_calls == 1
    assert send.close_calls == 1
    assert StartFailingProcess.close_calls == 1
    assert FakeSharedMemory.close_calls == 1
    assert FakeSharedMemory.unlink_calls == 1
    assert not FakeSharedMemory.names


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
    monkeypatch.setattr(sys, "executable", str(tmp_path / "matteloop"))
    assert ModelCatalog.resource_path() == resource_dir / "model-manifest.json"
    assert ModelCatalog.provenance_path() == resource_dir / "model-provenance.json"


def test_pyside_deploy_spec_parses_to_required_native_bundle_contract() -> None:
    spec_path = REPOSITORY_ROOT / "packaging" / "pysidedeploy.spec"
    parser = configparser.ConfigParser(
        comment_prefixes=("/",), strict=False, allow_no_value=True
    )
    parser.read(spec_path, encoding="utf-8")

    assert parser.get("app", "title") == "MatteLoop"
    assert parser.get("app", "project_dir") == "."
    assert parser.get("app", "input_file") == "packaging/entrypoint.py"
    assert parser.get("app", "exec_directory") == "dist"
    assert parser.get("python", "packages") == "Nuitka==2.8.10"
    assert set(parser.get("qt", "modules").split(",")) >= {
        "Core",
        "DBus",
        "Gui",
        "Network",
        "Widgets",
    }
    assert set(parser.get("qt", "plugins").split(",")) == {
        "imageformats",
        "platforms",
    }
    assert parser.get("nuitka", "mode") == "standalone"

    args = set(shlex.split(parser.get("nuitka", "extra_args")))
    assert "--include-package=matteloop" in args
    assert "--include-module=matteloop.smoke_child" in args
    assert "--include-module=rembg.sessions.base" in args
    assert {
        "--include-module=rembg.sessions.birefnet_general",
        "--include-module=rembg.sessions.birefnet_general_lite",
        "--include-module=rembg.sessions.birefnet_portrait",
        "--include-module=rembg.sessions.birefnet_dis",
        "--include-module=rembg.sessions.birefnet_hrsod",
        "--include-module=rembg.sessions.birefnet_cod",
        "--include-module=rembg.sessions.birefnet_massive",
        "--include-module=rembg.sessions.dis_anime",
        "--include-module=rembg.sessions.dis_general_use",
        "--include-module=rembg.sessions.silueta",
        "--include-module=rembg.sessions.u2net",
        "--include-module=rembg.sessions.u2netp",
        "--include-module=rembg.sessions.u2net_human_seg",
    } <= args
    assert "--include-package=rembg.sessions" not in args
    assert "--include-package=onnxruntime" in args
    assert "--nofollow-import-to=av" in args
    assert "--nofollow-import-to=pymatting" in args
    assert "--nofollow-import-to=numba" in args
    assert "--nofollow-import-to=llvmlite" in args
    assert "--nofollow-import-to=scipy" in args
    assert "--nofollow-import-to=skimage" in args
    assert "--noinclude-dlls=*libqpdf*" in args
    assert "--noinclude-dlls=*QtPdf*" in args
    assert "--disable-cache=ccache" in args
    assert "--include-module=PIL._imaging" in args
    assert "--include-module=PIL._webp" in args
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


def test_native_bundle_includes_project_license_notices() -> None:
    parser = configparser.ConfigParser(
        comment_prefixes=("/",), strict=False, allow_no_value=True
    )
    parser.read(
        REPOSITORY_ROOT / "packaging" / "pysidedeploy.spec",
        encoding="utf-8",
    )

    args = set(shlex.split(parser.get("nuitka", "extra_args")))

    assert "--include-data-files=LICENSE=LICENSE" in args
    assert (
        "--include-data-files=THIRD_PARTY_NOTICES.md=THIRD_PARTY_NOTICES.md"
    ) in args


def test_pyside_deploy_accepts_spec_in_dry_run_mode(tmp_path: Path) -> None:
    executable_name = (
        "pyside6-deploy.exe" if sys.platform == "win32" else "pyside6-deploy"
    )
    deploy = Path(sys.executable).with_name(executable_name)
    assert deploy.is_file()
    temporary_spec = tmp_path / "pysidedeploy.spec"
    shutil.copy2(REPOSITORY_ROOT / "packaging" / "pysidedeploy.spec", temporary_spec)
    completed = subprocess.run(
        [
            str(deploy),
            "-c",
            str(temporary_spec),
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
    assert {(item["target"], item["os"]) for item in includes} == {
        ("windows-2022-x64", "windows-2022"),
        ("macos-15-arm64", "macos-15"),
    }
    assert job["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "matteloop-native-release",
        "cancel-in-progress": False,
    }

    steps = job["steps"]
    setup = next(step for step in steps if step.get("id") == "setup-uv")
    assert setup["with"] == {
        "enable-cache": False,
        "python-version": "3.13",
        "version": "0.11.32",
    }
    commands = "\n".join(str(step.get("run", "")) for step in steps)
    assert "uv sync --frozen --all-groups" in commands
    assert "--no-cache" not in commands
    assert "python scripts/build.py" in commands
    assert "xvfb-run" not in commands
    build = next(
        step for step in steps if step["name"] == "Build native standalone bundle"
    )
    assert build["run"] == "uv run --frozen --no-sync python scripts/build.py"

    upload = next(step for step in steps if step.get("id") == "upload")
    assert upload["with"]["name"] == "MatteLoop-unsigned-${{ matrix.target }}"
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
        "matteloop_packaging_smoke", launcher_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.spawn_smoke_target.__module__ == "matteloop.smoke_child"


def test_linux_patch_tool_is_exactly_pinned_in_project_and_lock() -> None:
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text())
    assert (
        "patchelf==0.17.2.4; sys_platform == 'linux'"
        in project["dependency-groups"]["dev"]
    )

    lock = tomllib.loads((REPOSITORY_ROOT / "uv.lock").read_text())
    patch_tool = next(
        package for package in lock["package"] if package["name"] == "patchelf"
    )
    assert patch_tool["version"] == "0.17.2.4"
    application = next(
        package for package in lock["package"] if package["name"] == "matteloop"
    )
    assert {
        "name": "patchelf",
        "marker": "sys_platform == 'linux'",
    } in application["dev-dependencies"]["dev"]
    assert {
        "name": "patchelf",
        "marker": "sys_platform == 'linux'",
        "specifier": "==0.17.2.4",
    } in application["metadata"]["requires-dev"]["dev"]
