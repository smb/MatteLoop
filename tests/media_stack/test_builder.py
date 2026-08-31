import hashlib
import json
import os
import shlex
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
from scripts.media_stack.manifest import SourceSpec, media_stack_identity
from scripts.media_stack.platforms import BuildTarget

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "packaging" / "media-stack" / "manifest.toml"
MACOS = BuildTarget("darwin", "arm64", "macos-arm64", "cp313", "13.0")
WINDOWS = BuildTarget("win32", "AMD64", "windows-x64", "cp313", "")
WHEEL_NAME = "av-16.1.0-cp311-abi3-macosx_13_0_arm64.whl"
WINDOWS_WHEEL_NAME = "av-16.1.0-cp311-abi3-win_amd64.whl"


class RecordingRunner:
    def __init__(
        self,
        *,
        fail_stage: str | None = None,
        empty_compiler_evidence: bool = False,
        wheel_payload: bytes = b"verified repaired wheel",
    ) -> None:
        self.fail_stage = fail_stage
        self.empty_compiler_evidence = empty_compiler_evidence
        self.wheel_payload = wheel_payload
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
        stdout, stderr = self._outputs(stage, normalized)
        return subprocess.CompletedProcess(normalized, 0, stdout, stderr)

    def _outputs(self, stage: str, command: tuple[str, ...]) -> tuple[str, str]:
        if stage == "compiler" and self.empty_compiler_evidence:
            return "", ""
        if stage == "compiler" and command[0].casefold() == "cl":
            return "", "Microsoft (R) C/C++ Optimizing Compiler Version 19.44\n"
        if stage == "compiler":
            return "Apple clang version 17.0.0\n", ""
        if stage == "cmake":
            return "cmake version 4.1.1\n", ""
        return "", ""

    @staticmethod
    def _stage(command: tuple[str, ...]) -> str:
        joined = " ".join(command)
        if command[:2] in (("uv", "venv"), ("uv", "pip")):
            return "tools"
        if command == ("cmake", "--version"):
            return "cmake"
        if command[0] in {"cc", "clang-review", "cl"}:
            return "compiler"
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
            (output / Path(command[-1]).name).write_bytes(self.wheel_payload)
        elif stage == "verify":
            wheel = Path(command[2])
            provenance = json.loads(
                builder.provenance_path(wheel).read_text(encoding="utf-8")
            )
            report = Path(command[command.index("--report") + 1])
            report.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "evidence": {"dependencies": ["libavcodec.dylib", "libwebp.dylib"]},
                "identity": provenance["identity"],
                "manifest_sha256": provenance["manifest_sha256"],
                "python_tag": provenance["python_tag"],
                "target_id": provenance["target_id"],
                "wheel_filename": provenance["wheel_filename"],
                "wheel_sha256": provenance["wheel_sha256"],
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
            "cython": (
                (
                    "COPYING.txt",
                    b"The original Pyrex code as of 2006-04 is licensed under the "
                    b"following license.\n\nCython, which derives from Pyrex, is "
                    b"licensed under the Apache 2.0 Software License.\n",
                ),
                (
                    "LICENSE.txt",
                    b"Apache License\nVersion 2.0, January 2004\n",
                ),
            ),
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


def test_invalid_manifest_retains_a_named_invocation_staging_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    manifest = root / "packaging" / "media-stack" / "manifest.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("schema_version = 999\n", encoding="utf-8")

    with pytest.raises(MediaStackBuildError) as raised:
        ensure_media_stack(root, tmp_path / "cache", runner=RecordingRunner())

    error = raised.value
    assert error.stage == "manifest"
    assert error.staging_retained is True
    assert error.staging_dir.is_dir()
    assert error.staging_dir.name.startswith(".staging-")
    assert f"staging retained at {error.staging_dir}" in str(error)


def test_invalid_target_retains_a_named_invocation_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_target(**_kwargs: object) -> BuildTarget:
        raise ValueError("unsupported host")

    monkeypatch.setattr(builder, "detect_target", reject_target)

    with pytest.raises(MediaStackBuildError) as raised:
        ensure_media_stack(ROOT, tmp_path / "cache", runner=RecordingRunner())

    error = raised.value
    assert error.stage == "target"
    assert error.staging_retained is True
    assert error.staging_dir.is_dir()
    assert error.staging_dir.name.startswith(".staging-")
    assert f"staging retained at {error.staging_dir}" in str(error)


def test_staging_creation_failure_has_structured_truthful_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_mkdir = Path.mkdir

    def fail_staging_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path.name.startswith(".staging-"):
            raise PermissionError(13, "injected staging mkdir failure", str(path))
        real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_staging_mkdir)

    with pytest.raises(MediaStackBuildError) as raised:
        ensure_media_stack(ROOT, tmp_path / "cache", runner=RecordingRunner())

    error = raised.value
    assert error.stage == "staging"
    assert error.command == ("mkdir", str(error.staging_dir))
    assert error.returncode == 13
    assert error.staging_retained is False
    assert not error.staging_dir.exists()
    assert f"staging was not created at {error.staging_dir}" in str(error)
    assert "staging retained" not in str(error)


def test_compliance_uses_digest_bound_cython_source_and_substantive_licences(
    tmp_path: Path,
) -> None:
    artifacts = ensure_media_stack(ROOT, tmp_path, runner=RecordingRunner())

    with tarfile.open(artifacts.compliance_archive) as archive:
        names = archive.getnames()
        assert "tool-sources/cython-3.3.0.tar.gz" in names
        assert (
            archive.extractfile("licences/cython/COPYING.txt")
            .read()
            .startswith(b"The original Pyrex code as of 2006-04")
        )
        assert (
            b"Apache License\nVersion 2.0"
            in archive.extractfile("licences/cython/LICENSE.txt").read()
        )
        assert "licences/cython/METADATA" not in names


def test_cache_hit_is_reverified_without_recompiling(tmp_path: Path) -> None:
    first_runner = RecordingRunner()
    first = ensure_media_stack(ROOT, tmp_path, runner=first_runner)
    second_runner = RecordingRunner()

    second = ensure_media_stack(ROOT, tmp_path, runner=second_runner)

    assert second == first
    assert second_runner.stage_names == ["verify"]
    assert not any(command[0] == "uv" for command, _kwargs in second_runner.calls)


def test_cache_hit_rejects_compliance_bytes_outside_the_bound_artifact_set(
    tmp_path: Path,
) -> None:
    artifacts = ensure_media_stack(ROOT, tmp_path, runner=RecordingRunner())
    artifacts.compliance_archive.write_bytes(b"replaced after verified build")
    runner = RecordingRunner()

    with pytest.raises(MediaStackBuildError) as raised:
        ensure_media_stack(ROOT, tmp_path, runner=runner)

    assert raised.value.stage == "artifact-set"
    assert "validate" in raised.value.command
    assert "verify" not in runner.stage_names


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


def test_macos_repair_inherits_environment_with_staged_libraries_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/existing/libraries")
    monkeypatch.setenv("MATTELOOP_REPAIR_SENTINEL", "preserved")
    runner = RecordingRunner()

    artifacts = ensure_media_stack(ROOT, tmp_path, runner=runner)

    repair_command, repair_kwargs = next(
        (command, kwargs)
        for command, kwargs in runner.calls
        if any("delocate-wheel" in argument for argument in command[:2])
    )
    tool_python = Path(repair_command[0])
    delocate_script = Path(repair_command[1])
    assert tool_python.name == "python"
    assert delocate_script == tool_python.with_name("delocate-wheel")
    assert repair_command[2] == "-w"
    raw_wheel = Path(repair_command[-1])
    staged_libraries = raw_wheel.parent.parent / "prefix" / "lib"
    environment = repair_kwargs["env"]
    assert environment["DYLD_LIBRARY_PATH"] == (
        f"{staged_libraries}{os.pathsep}/existing/libraries"
    )
    assert environment["MACOSX_DEPLOYMENT_TARGET"] == "13.0"
    assert environment["MATTELOOP_REPAIR_SENTINEL"] == "preserved"
    with tarfile.open(artifacts.compliance_archive) as archive:
        commands = archive.extractfile("build/commands.txt").read().decode()
    repair_line = next(
        line for line in commands.splitlines() if "delocate-wheel" in line
    )
    repair_evidence = shlex.split(repair_line.removeprefix("$ "))
    assert repair_evidence[:3] == [
        "env",
        "DYLD_LIBRARY_PATH=${STAGING}/prefix/lib",
        "MACOSX_DEPLOYMENT_TARGET=13.0",
    ]
    assert "/existing/libraries" not in commands
    assert "MATTELOOP_REPAIR_SENTINEL" not in commands
    assert "preserved" not in commands


def test_media_builder_revision_invalidates_prior_repair_evidence(
    tmp_path: Path,
) -> None:
    artifacts = ensure_media_stack(ROOT, tmp_path, runner=RecordingRunner())
    expected = media_stack_identity(
        MANIFEST,
        os_name=MACOS.os_name,
        machine=MACOS.machine,
        python_tag=MACOS.python_tag,
        deployment_target=MACOS.deployment_target,
        builder_revision=2,
    )

    assert artifacts.identity == expected


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
        commands = archive.extractfile("build/commands.txt").read().decode()
    assert "DYLD_LIBRARY_PATH" not in commands
    assert "MACOSX_DEPLOYMENT_TARGET" not in commands


def test_macos_compiler_evidence_probes_configured_cc_and_cmake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CC", "clang-review")
    runner = RecordingRunner()

    artifacts = ensure_media_stack(ROOT, tmp_path, runner=runner)

    assert any(command == ("clang-review", "--version") for command, _ in runner.calls)
    assert any(command == ("cmake", "--version") for command, _ in runner.calls)
    with tarfile.open(artifacts.compliance_archive) as archive:
        evidence = archive.extractfile("build/compiler-versions.txt").read()
    assert b"$ clang-review --version" in evidence
    assert b"Apple clang version 17.0.0" in evidence
    assert b"$ cmake --version" in evidence
    assert b"cmake version 4.1.1" in evidence


def test_windows_compiler_evidence_probes_msvc_and_cmake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(builder, "detect_target", lambda **_kwargs: WINDOWS)
    runner = RecordingRunner()

    artifacts = ensure_media_stack(ROOT, tmp_path, runner=runner)

    assert any(command == ("cl",) for command, _ in runner.calls)
    assert any(command == ("cmake", "--version") for command, _ in runner.calls)
    with tarfile.open(artifacts.compliance_archive) as archive:
        evidence = archive.extractfile("build/compiler-versions.txt").read()
    assert b"$ cl" in evidence
    assert b"stderr:\nMicrosoft (R) C/C++ Optimizing Compiler" in evidence
    assert b"$ cmake --version" in evidence


def test_empty_compiler_probe_is_a_structured_evidence_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CC", "clang-review")
    runner = RecordingRunner(empty_compiler_evidence=True)

    with pytest.raises(MediaStackBuildError) as raised:
        ensure_media_stack(ROOT, tmp_path, runner=runner)

    error = raised.value
    assert error.stage == "compiler-evidence"
    assert error.command == ("clang-review", "--version")
    assert error.returncode == 1
    assert error.staging_retained is True
    assert error.staging_dir.is_dir()
    assert not tuple(tmp_path.glob("*/finished/*.whl"))


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


def test_failed_finished_directory_switch_restores_the_prior_complete_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = ensure_media_stack(
        ROOT, tmp_path, runner=RecordingRunner(wheel_payload=b"first wheel")
    )
    finished = first.wheel.parent
    before = {path.name: path.read_bytes() for path in finished.iterdir()}
    real_replace = builder.os.replace

    def fail_mid_switch(source: object, destination: object) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        sequential_midpoint = (
            destination_path.parent == finished
            and destination_path.name == "verification-report.json"
        )
        directory_switch = (
            source_path.name.startswith(".finished-candidate-")
            and destination_path == finished
        )
        if sequential_midpoint or directory_switch:
            raise OSError(5, "injected finished switch failure")
        real_replace(source, destination)

    monkeypatch.setattr(builder.os, "replace", fail_mid_switch)

    with pytest.raises(MediaStackBuildError) as raised:
        ensure_media_stack(
            ROOT,
            tmp_path,
            force=True,
            runner=RecordingRunner(wheel_payload=b"second wheel"),
        )

    assert raised.value.stage == "promote"
    assert {path.name: path.read_bytes() for path in finished.iterdir()} == before
    assert not tuple(finished.parent.glob(".finished-backup-*"))
    assert not tuple(finished.parent.glob(".finished-candidate-*"))
    failed_candidate = raised.value.staging_dir / "failed-finished-candidate"
    assert {path.name for path in failed_candidate.iterdir()} == set(before)


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


def test_artifact_set_binds_every_finished_output_to_verified_identity(
    tmp_path: Path,
) -> None:
    artifacts = ensure_media_stack(ROOT, tmp_path, runner=RecordingRunner())

    raw = artifacts.artifact_set.read_text(encoding="utf-8")
    payload = json.loads(raw)
    provenance = json.loads(artifacts.provenance.read_text(encoding="utf-8"))
    report = json.loads(artifacts.report.read_text(encoding="utf-8"))

    assert raw == json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    assert payload == {
        "compliance_archive": {
            "filename": artifacts.compliance_archive.name,
            "sha256": hashlib.sha256(
                artifacts.compliance_archive.read_bytes()
            ).hexdigest(),
        },
        "identity": artifacts.identity,
        "manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        "provenance": {
            "filename": artifacts.provenance.name,
            "identity": provenance["identity"],
            "sha256": hashlib.sha256(artifacts.provenance.read_bytes()).hexdigest(),
        },
        "python_abi": "cp313",
        "schema_version": 1,
        "target_id": "macos-arm64",
        "verification_report": {
            "filename": artifacts.report.name,
            "identity": report["identity"],
            "sha256": hashlib.sha256(artifacts.report.read_bytes()).hexdigest(),
        },
        "wheel": {
            "filename": artifacts.wheel.name,
            "sha256": hashlib.sha256(artifacts.wheel.read_bytes()).hexdigest(),
        },
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
            artifacts.artifact_set,
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
        artifact_set=finished / "artifact-set.json",
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
                "artifact_set": str(artifacts.artifact_set),
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
