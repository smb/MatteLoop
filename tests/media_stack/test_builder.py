import hashlib
import json
import subprocess
import sys
import tarfile
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from scripts.media_stack import builder
from scripts.media_stack.builder import (
    MediaStackArtifacts,
    MediaStackBuildError,
    ensure_media_stack,
    main,
)
from scripts.media_stack.manifest import SourceSpec
from scripts.media_stack.platforms import BuildTarget

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "packaging" / "media-stack" / "manifest.toml"
MACOS = BuildTarget("darwin", "arm64", "macos-arm64", "cp313", "13.0")
WINDOWS = BuildTarget("win32", "AMD64", "windows-x64", "cp313", "")
WHEEL_NAME = "av-16.1.0-cp313-cp313-macosx_13_0_arm64.whl"
WINDOWS_WHEEL_NAME = "av-16.1.0-cp313-cp313-win_amd64.whl"


class RecordingRunner:
    def __init__(self, *, fail_stage: str | None = None) -> None:
        self.fail_stage = fail_stage
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    @property
    def stage_names(self) -> list[str]:
        stages = [self._stage(command) for command, _kwargs in self.calls]
        primary = [
            stage
            for stage in stages
            if stage in {"libwebp", "ffmpeg", "pyav", "repair", "verify"}
        ]
        return [
            stage
            for index, stage in enumerate(primary)
            if primary[:index][-1:] != [stage]
        ]

    def __call__(
        self, command: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        normalized = tuple(str(part) for part in command)
        self.calls.append((normalized, kwargs))
        assert kwargs["check"] is False
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        stage = self._stage(normalized)
        if stage == self.fail_stage:
            return subprocess.CompletedProcess(normalized, 9, "", "declared failure")
        self._materialize(stage, normalized)
        stdout = "Apple clang version 17.0.0\n" if stage == "toolchain" else ""
        return subprocess.CompletedProcess(normalized, 0, stdout, "")

    @staticmethod
    def _stage(command: tuple[str, ...]) -> str:
        joined = " ".join(command)
        if command[:2] in (("uv", "venv"), ("uv", "pip")):
            return "tools"
        if command == ("cmake", "--version"):
            return "toolchain"
        if command[0] == "cmake":
            return "libwebp"
        if (
            ("/configure" in joined and "--disable-gpl" in joined)
            or command[0] == "make"
            or (command[0] == "msys2" and "make -C" in joined)
        ):
            return "ffmpeg"
        if "-m build" in joined or "bdist_wheel" in command:
            return "pyav"
        if "delocate-wheel" in joined or "delvewheel repair" in joined:
            return "repair"
        if "verify_media_stack.py" in joined:
            return "verify"
        raise AssertionError(f"unexpected command: {command!r}")

    def _materialize(self, stage: str, command: tuple[str, ...]) -> None:
        if command[:2] == ("uv", "venv"):
            environment = Path(command[-1])
            for python in (
                environment / "bin" / "python",
                environment / "Scripts" / "python.exe",
            ):
                python.parent.mkdir(parents=True)
                python.write_text("", encoding="utf-8")
        elif command[:2] == ("uv", "pip"):
            self._materialize_tool_licences(Path(command[4]), command[5:])
        elif stage == "pyav":
            option = "--outdir" if "--outdir" in command else "--dist-dir"
            wheel_name = WHEEL_NAME if option == "--outdir" else WINDOWS_WHEEL_NAME
            output = Path(command[command.index(option) + 1])
            output.mkdir(parents=True, exist_ok=True)
            (output / wheel_name).write_bytes(b"unrepaired wheel")
        elif stage == "repair":
            option = "-w" if "-w" in command else "--wheel-dir"
            output = Path(command[command.index(option) + 1])
            output.mkdir(parents=True, exist_ok=True)
            (output / Path(command[-1]).name).write_bytes(b"verified repaired wheel")
        elif stage == "verify":
            report = Path(command[command.index("--report") + 1])
            report.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "evidence": {"dependencies": ["libavcodec.dylib", "libwebp.dylib"]},
                "identity": "runner-verified",
            }
            report.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )

    @staticmethod
    def _materialize_tool_licences(
        tool_python: Path, requirements: Sequence[str]
    ) -> None:
        environment = tool_python.parents[1]
        site_packages = (
            environment / "Lib" / "site-packages"
            if tool_python.parent.name == "Scripts"
            else environment / "lib" / "python3.13" / "site-packages"
        )
        packages = [
            ("build-1.6.0", "LICENSE"),
            ("setuptools-84.0.0", "LICENSE"),
            ("wheel-0.48.0", "LICENSE.txt"),
        ]
        if "delocate==0.13.0" in requirements:
            packages.append(("delocate-0.13.0", "LICENSE"))
        if "delvewheel==1.13.0" in requirements:
            packages.append(("delvewheel-1.13.0", "LICENSE"))
        for package, filename in packages:
            licence = site_packages / f"{package}.dist-info" / "licenses" / filename
            licence.parent.mkdir(parents=True, exist_ok=True)
            licence.write_text(f"{package} licence\n", encoding="utf-8")
        metadata = site_packages / "Cython-3.3.0.dist-info" / "METADATA"
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_text(
            "Metadata-Version: 2.4\nName: Cython\nVersion: 3.3.0\n"
            "License: Apache-2.0\n",
            encoding="utf-8",
        )


@pytest.fixture(autouse=True)
def native_target_and_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builder, "detect_target", lambda **_kwargs: MACOS)

    def fake_source(source: SourceSpec, cache_dir: Path) -> Path:
        archive = cache_dir / Path(urlsplit(source.url).path).name
        if archive.exists():
            return archive
        archive.parent.mkdir(parents=True, exist_ok=True)
        files = {
            "ffmpeg": (
                ("COPYING.LGPLv2.1", b"LGPL 2.1"),
                ("COPYING.LGPLv3", b"LGPL 3"),
            ),
            "libwebp": (("COPYING", b"libwebp licence"),),
            "pyav": (("LICENSE.txt", b"PyAV licence"),),
        }[source.name]
        with tarfile.open(archive, "w:gz") as source_archive:
            root = tarfile.TarInfo(source.archive_root)
            root.type = tarfile.DIRTYPE
            source_archive.addfile(root)
            for name, contents in files:
                member = tarfile.TarInfo(f"{source.archive_root}/{name}")
                member.size = len(contents)
                source_archive.addfile(member, BytesIO(contents))
        return archive

    monkeypatch.setattr(builder, "ensure_source", fake_source)


def test_cache_miss_runs_native_build_verification_and_compliance_in_order(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()

    artifacts = ensure_media_stack(ROOT, tmp_path, runner=runner)

    assert runner.stage_names == ["libwebp", "ffmpeg", "pyav", "repair", "verify"]
    assert artifacts.compliance_archive.is_file()
    assert artifacts.report.is_file()


def test_cache_hit_is_reverified_without_recompiling(tmp_path: Path) -> None:
    first_runner = RecordingRunner()
    first = ensure_media_stack(ROOT, tmp_path, runner=first_runner)
    second_runner = RecordingRunner()

    second = ensure_media_stack(ROOT, tmp_path, runner=second_runner)

    assert second == first
    assert second_runner.stage_names == ["verify"]
    assert not any(command[0] == "uv" for command, _kwargs in second_runner.calls)


def test_tool_environment_installs_only_the_target_specific_manifest_pins(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()

    ensure_media_stack(ROOT, tmp_path, runner=runner)

    venv = next(
        command for command, _kwargs in runner.calls if command[:2] == ("uv", "venv")
    )
    install = next(
        command
        for command, _kwargs in runner.calls
        if command[:3] == ("uv", "pip", "install")
    )
    assert venv[2:4] == ("--python", sys.executable)
    assert install[3] == "--python"
    assert install[5:] == (
        "build==1.6.0",
        "setuptools==84.0.0",
        "Cython==3.3.0",
        "wheel==0.48.0",
        "delocate==0.13.0",
    )


def test_windows_build_uses_delvewheel_and_archives_its_licence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(builder, "detect_target", lambda **_kwargs: WINDOWS)
    runner = RecordingRunner()

    artifacts = ensure_media_stack(ROOT, tmp_path, runner=runner)

    install = next(
        command
        for command, _kwargs in runner.calls
        if command[:3] == ("uv", "pip", "install")
    )
    assert install[-1] == "delvewheel==1.13.0"
    assert "delocate==0.13.0" not in install
    assert artifacts.wheel.name == WINDOWS_WHEEL_NAME
    assert "windows-x64" in artifacts.compliance_archive.name
    with tarfile.open(artifacts.compliance_archive) as archive:
        assert "licences/delvewheel/LICENSE" in archive.getnames()


def test_force_rebuild_runs_compilation_again(tmp_path: Path) -> None:
    first = ensure_media_stack(ROOT, tmp_path, runner=RecordingRunner())
    archive_before = first.compliance_archive.read_bytes()
    runner = RecordingRunner()

    rebuilt = ensure_media_stack(ROOT, tmp_path, force=True, runner=runner)

    assert runner.stage_names == ["libwebp", "ffmpeg", "pyav", "repair", "verify"]
    assert rebuilt.compliance_archive.read_bytes() == archive_before


def test_failed_verification_retains_staging_without_promoting_wheel(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(fail_stage="verify")

    with pytest.raises(MediaStackBuildError) as raised:
        ensure_media_stack(ROOT, tmp_path, runner=runner)

    assert raised.value.stage == "verify"
    assert raised.value.returncode == 9
    assert raised.value.staging_dir.is_dir()
    assert (raised.value.staging_dir / "repaired" / WHEEL_NAME).is_file()
    assert not tuple(tmp_path.glob("*/finished/*.whl"))


def test_failed_force_rebuild_preserves_prior_verified_artifacts(
    tmp_path: Path,
) -> None:
    first = ensure_media_stack(ROOT, tmp_path, runner=RecordingRunner())
    paths = (first.wheel, first.provenance, first.compliance_archive, first.report)
    before = {path: path.read_bytes() for path in paths}

    with pytest.raises(MediaStackBuildError):
        ensure_media_stack(
            ROOT, tmp_path, force=True, runner=RecordingRunner(fail_stage="verify")
        )

    assert {path: path.read_bytes() for path in before} == before


def test_provenance_binds_manifest_target_abi_filename_and_digest(
    tmp_path: Path,
) -> None:
    artifacts = ensure_media_stack(ROOT, tmp_path, runner=RecordingRunner())

    provenance = json.loads(artifacts.provenance.read_text(encoding="utf-8"))
    assert provenance == {
        "identity": artifacts.identity,
        "manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        "python_tag": "cp313",
        "target_id": "macos-arm64",
        "wheel_filename": WHEEL_NAME,
        "wheel_sha256": hashlib.sha256(artifacts.wheel.read_bytes()).hexdigest(),
    }


def test_artifact_paths_are_canonical_finished_cache_paths(tmp_path: Path) -> None:
    artifacts = ensure_media_stack(ROOT, tmp_path, runner=RecordingRunner())
    expected_parent = (tmp_path / artifacts.identity / "finished").resolve()

    assert all(
        path.is_absolute() and path.parent == expected_parent
        for path in (
            artifacts.wheel,
            artifacts.provenance,
            artifacts.compliance_archive,
            artifacts.report,
        )
    )


def test_cli_json_prints_sorted_absolute_artifact_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    finished = (tmp_path / "finished").resolve()
    artifacts = MediaStackArtifacts(
        wheel=finished / "wheel.whl",
        provenance=finished / "wheel.whl.provenance.json",
        compliance_archive=finished / "sources.tar.gz",
        report=finished / "report.json",
        identity="identity",
    )
    monkeypatch.setattr(
        builder, "ensure_media_stack", lambda *_args, **_kwargs: artifacts
    )

    assert main(["--json", "--cache-dir", str(tmp_path)]) == 0
    assert (
        capsys.readouterr().out
        == json.dumps(
            {
                "compliance_archive": str(artifacts.compliance_archive),
                "identity": "identity",
                "provenance": str(artifacts.provenance),
                "report": str(artifacts.report),
                "wheel": str(artifacts.wheel),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def test_cli_reports_failed_stage_and_returns_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    error = MediaStackBuildError("verify", ("verify", "wheel.whl"), 9, tmp_path)
    monkeypatch.setattr(
        builder,
        "ensure_media_stack",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    assert main(["--cache-dir", str(tmp_path)]) == 1
    assert "verify" in capsys.readouterr().err


def test_cli_wrapper_is_directly_executable() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_media_stack.py"), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "--cache-dir" in completed.stdout
    assert completed.stderr == ""
