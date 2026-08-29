from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from importlib.metadata import version
from pathlib import Path

import pytest

from rembggui.app import main

_GUARDED_HEADLESS_SCRIPT = """
import builtins
import os
import socket
import sys

FORBIDDEN_ROOTS = {"PySide6", "rembg", "onnxruntime"}
MODEL_PATH_MARKERS = (".u2net", "onnx", "rembg")

class ForbiddenImportFinder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition(".")[0] in FORBIDDEN_ROOTS:
            raise RuntimeError(f"forbidden import: {fullname}")
        return None

sys.meta_path.insert(0, ForbiddenImportFinder())

def forbidden_network(*args, **kwargs):
    raise RuntimeError("network access is forbidden in headless smoke commands")

socket.socket = forbidden_network
socket.create_connection = forbidden_network

original_open = builtins.open
def guarded_open(file, *args, **kwargs):
    try:
        candidate = os.fspath(file).lower()
    except TypeError:
        candidate = ""
    if any(marker in candidate for marker in MODEL_PATH_MARKERS):
        raise RuntimeError(f"model access is forbidden: {candidate}")
    return original_open(file, *args, **kwargs)

builtins.open = guarded_open

from rembggui.app import main

raise SystemExit(main([sys.argv[1]]))
"""


def _run_guarded_smoke_command(
    tmp_path: Path, argument: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "DISPLAY": "rembggui-smoke-display-must-not-open",
        "HOME": str(tmp_path),
        "QT_QPA_PLATFORM": "rembggui-smoke-platform-must-not-initialize",
        "U2NET_HOME": str(tmp_path / "model-guard"),
    }
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            textwrap.dedent(_GUARDED_HEADLESS_SCRIPT),
            argument,
        ],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )


def test_main_reports_version_without_opening_qt(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip().startswith("rembgGUI ")


def test_main_smoke_test_prints_machine_readable_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from rembggui import smoke

    monkeypatch.setattr(
        smoke,
        "run_smoke",
        lambda work_dir, use_fake_model=True: smoke.SmokeResult(
            qt_platform="offscreen",
            qt_image_formats=("png", "webp"),
            video_decoded=True,
            webp_frames=2,
            webp_has_alpha=True,
            spawn_start_method="spawn",
            shared_memory_roundtrip=True,
            shared_memory_unlinked=True,
            fake_session_used=True,
            peak_full_res_rgba_owners=2,
        ),
    )
    monkeypatch.setenv("REMBGGUI_SMOKE_WORK_DIR", str(tmp_path))

    assert main(["--smoke-test"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["result"]["webp_frames"] == 2


def test_main_smoke_test_prints_structured_nonzero_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from rembggui import smoke

    def fail(_work_dir: Path, use_fake_model: bool = True) -> smoke.SmokeResult:
        del use_fake_model
        raise RuntimeError("intentional smoke failure")

    monkeypatch.setattr(smoke, "run_smoke", fail)
    monkeypatch.setenv("REMBGGUI_SMOKE_WORK_DIR", str(tmp_path))

    assert main(["--smoke-test"]) != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "error": {
            "message": "intentional smoke failure",
            "type": "RuntimeError",
        },
        "ok": False,
    }


@pytest.mark.parametrize(
    ("argument", "expected_output"),
    [
        ("--version", f"rembgGUI {version('rembggui')}"),
    ],
)
def test_headless_commands_run_in_fresh_guarded_interpreters(
    tmp_path: Path,
    argument: str,
    expected_output: str,
):
    result = _run_guarded_smoke_command(tmp_path, argument)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected_output
