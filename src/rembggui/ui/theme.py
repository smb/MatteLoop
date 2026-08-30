"""Approved desktop theme and packaged-font loading."""

from __future__ import annotations

import stat
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from rembggui.resources import resource_path

UI_FONT = "IBM Plex Sans"
MONO_FONT = "IBM Plex Mono"
_FONT_FILES = (
    "IBMPlexSans-Regular.ttf",
    "IBMPlexSans-SemiBold.ttf",
    "IBMPlexMono-Regular.ttf",
)


def install_theme(
    application: QApplication, *, runtime_root: Path | None = None
) -> bool:
    """Install the approved palette and return whether every bundled font loaded."""
    loaded = load_packaged_fonts(runtime_root=runtime_root)
    application.setFont(QFont(UI_FONT if loaded else "Sans Serif", 10))
    application.setStyleSheet(
        """
        QWidget { background: #111315; color: #F3F5F7;
                  font-family: 'IBM Plex Sans', sans-serif; }
        QFrame#preview_stage { background: #0B0D0F; border: 1px solid #30353A; }
        QWidget#inspector { background: #171A1D; border-left: 1px solid #30353A; }
        QPushButton { background: #202428; border: 1px solid #30353A;
                      border-radius: 4px; min-height: 38px; padding: 0 12px; }
        QPushButton:hover { border-color: #C8FF63; }
        QPushButton:focus, QLabel:focus, QFrame:focus, QWidget:focus {
            outline: none; border: 2px solid #B7F34A;
        }
        QLabel#result_canvas[status='stale'] { color: #F3B849; }
        QLabel#result_canvas[status='error'] { color: #FF6B6B; }
        QLabel#result_canvas[checkerboard='true'] {
            background-color: #252A2E;
        }
        QFrame#success_banner_container { border-top: 1px solid #30353A; }
        QLabel#success_banner { color: #63D69A; }
        QPushButton[primaryAction='true'] { background: #B7F34A; color: #10140A;
                                      border-color: #B7F34A; }
        QLabel[secondary='true'] { color: #A3ABB2; }
        QLabel[mono='true'] { font-family: 'IBM Plex Mono', monospace; }
        QToolButton { text-align: left; padding: 8px 0; color: #F3F5F7; border: 0; }
        QScrollArea { border: 0; }
        """
    )
    return loaded


def load_packaged_fonts(*, runtime_root: Path | None = None) -> bool:
    """Load direct, non-symlink font files; a system-font fallback remains valid."""
    try:
        anchor = resource_path("model-manifest.json", runtime_root=runtime_root)
        directory = anchor.parent / "fonts"
        directory_status = directory.lstat()
        if not stat.S_ISDIR(directory_status.st_mode) or stat.S_ISLNK(
            directory_status.st_mode
        ):
            return False
        paths = tuple(directory / name for name in _FONT_FILES)
        for path in paths:
            status = path.lstat()
            if not stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode):
                return False
        return all(QFontDatabase.addApplicationFont(str(path)) >= 0 for path in paths)
    except (FileNotFoundError, OSError, RuntimeError):
        return False
