"""Optional on-disk diagnostics for a frozen build with no console."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from platformdirs import user_log_dir

from matteloop.paths import NEW_CACHE_NAME

_MAX_BYTES = 1 << 20
_BACKUPS = 2


def log_file() -> Path:
    """Return the rotating diagnostics file for this installation."""
    return Path(user_log_dir(NEW_CACHE_NAME)) / "matteloop.log"


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
    return target
