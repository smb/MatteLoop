from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

from matteloop.logs import configure_logging, log_file


@pytest.fixture(autouse=True)
def _detach_handlers() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in before:
            handler.close()
            root.removeHandler(handler)
    root.setLevel(level)


def test_configured_logging_records_a_failure_a_frozen_build_cannot_print(
    tmp_path: Path,
) -> None:
    target = tmp_path / "logs" / "matteloop.log"

    assert configure_logging(target) == target
    logging.getLogger("matteloop.test").error("preview failed: %s", "tls handshake")

    assert "preview failed: tls handshake" in target.read_text(encoding="utf-8")


def test_logging_can_be_switched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MATTELOOP_LOG_LEVEL", "off")
    target = tmp_path / "matteloop.log"

    assert configure_logging(target) is None
    assert not target.exists()


def test_an_unwritable_destination_never_stops_the_application(
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("not a directory", encoding="utf-8")

    assert configure_logging(blocked / "nested" / "matteloop.log") is None


def test_log_file_lives_beside_the_products_other_user_data() -> None:
    assert log_file().name == "matteloop.log"
    assert "matteloop" in str(log_file()).casefold()


def test_a_native_crash_leaves_thread_stacks_behind(tmp_path: Path) -> None:
    # A segmentation fault kills the interpreter before logging runs, so the
    # log file stays empty and a packaged build leaves the user nothing to
    # report. Crash a real subprocess rather than trusting the wiring.
    target = tmp_path / "logs" / "matteloop.log"
    script = (
        "import faulthandler, sys;"
        "sys.path.insert(0, 'src');"
        "from matteloop.logs import configure_logging;"
        f"configure_logging(__import__('pathlib').Path({str(target)!r}));"
        "faulthandler._sigsegv()"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    report = target.with_suffix(".fault")
    assert report.is_file(), "no crash report was written"
    written = report.read_text(encoding="utf-8")
    assert "Current thread" in written
    assert "faulthandler._sigsegv" in written or "<module>" in written


@pytest.mark.parametrize("value", ["", "0", "-1", "not-a-number", "nan", "inf"])
def test_hang_dump_ignores_non_positive_or_malformed_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    import matteloop.logs as logs

    cancelled = 0
    armed: list[float] = []

    def cancel() -> None:
        nonlocal cancelled
        cancelled += 1

    monkeypatch.setattr(logs.faulthandler, "cancel_dump_traceback_later", cancel)
    monkeypatch.setattr(logs.faulthandler, "enable", lambda **_kwargs: None)
    monkeypatch.setattr(
        logs.faulthandler,
        "dump_traceback_later",
        lambda seconds, **_kwargs: armed.append(seconds),
    )
    monkeypatch.setenv("MATTELOOP_HANG_DUMP", value)

    logs._enable_fault_reports(tmp_path / "invalid.fault")

    assert cancelled == 1
    assert armed == []
    report = logs._fault_report
    if hasattr(report, "close"):
        report.close()


def test_hang_dump_arms_repeated_calls_with_a_positive_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import matteloop.logs as logs

    cancelled = 0
    armed: list[tuple[float, object]] = []

    def cancel() -> None:
        nonlocal cancelled
        cancelled += 1

    monkeypatch.setattr(logs.faulthandler, "cancel_dump_traceback_later", cancel)
    monkeypatch.setattr(logs.faulthandler, "enable", lambda **_kwargs: None)
    monkeypatch.setattr(
        logs.faulthandler,
        "dump_traceback_later",
        lambda seconds, **kwargs: armed.append((seconds, kwargs["file"])),
    )
    monkeypatch.setenv("MATTELOOP_HANG_DUMP", "2.5")

    logs._enable_fault_reports(tmp_path / "first.fault")
    logs._enable_fault_reports(tmp_path / "second.fault")

    assert cancelled == 2
    assert [seconds for seconds, _file in armed] == [2.5, 2.5]
    assert all(kwargs_file is not None for _seconds, kwargs_file in armed)
    report = logs._fault_report
    if hasattr(report, "close"):
        report.close()
