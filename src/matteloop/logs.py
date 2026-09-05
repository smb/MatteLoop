"""Optional on-disk diagnostics for a frozen build with no console."""

from __future__ import annotations

import faulthandler
import io
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from platformdirs import user_log_dir

from matteloop.paths import CACHE_NAME

_MAX_BYTES = 1 << 20
_BACKUPS = 2
_fault_report: object | None = None


def log_file() -> Path:
    """Return the rotating diagnostics file for this installation."""
    return Path(user_log_dir(CACHE_NAME)) / "matteloop.log"


def configure_logging(destination: Path | None = None) -> Path | None:
    """Attach a rotating file handler unless logging is switched off.

    MATTELOOP_LOG_LEVEL sets the level and accepts "off" to write nothing.
    A frozen build has no console, so a failure that never reaches the UI
    has nowhere else to go.
    """
    level_name = os.environ.get("MATTELOOP_LOG_LEVEL", "INFO").strip().upper()
    if level_name in {"OFF", "NONE", "0"}:
        return None
    level = getattr(logging, level_name, logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO
    target = log_file() if destination is None else destination
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            target, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
        )
    except OSError:
        return None
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    _enable_fault_reports(target.with_suffix(".fault"))
    return target


def _enable_fault_reports(target: Path) -> None:
    """Write thread stacks when the process dies in native code.

    A native fault kills the interpreter before logging runs, so the log
    file stays empty and a packaged build, having no console, leaves the
    user nothing to report. faulthandler writes from the signal handler
    itself, which is why it gets its own file rather than the rotating
    handler's.
    """
    global _fault_report
    try:
        report = target.open("a", buffering=1, encoding="utf-8")
    except OSError:
        return
    faulthandler.enable(file=report, all_threads=True)
    if isinstance(_fault_report, io.TextIOBase):
        _fault_report.close()
    _fault_report = report
