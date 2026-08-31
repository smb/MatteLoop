"""Approved desktop theme and packaged-font loading."""

from __future__ import annotations

import stat
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from rembggui.resources import resource_path

UI_FONT = "IBM Plex Sans"
MONO_FONT = "IBM Plex Mono"
BACKGROUND_COLOR = "#111315"
CANVAS_COLOR = "#0B0D0F"
INSPECTOR_COLOR = "#171A1D"
CONTROL_COLOR = "#202428"
TEXT_COLOR = "#F3F5F7"
ACCENT_COLOR = "#B7F34A"
HOVER_COLOR = "#C8FF63"
SECONDARY_COLOR = "#A3ABB2"
DISABLED_COLOR = "#687078"
DIVIDER_COLOR = "#30353A"
ERROR_COLOR = "#FF6B6B"
WARNING_COLOR = "#F3B849"
SUCCESS_COLOR = "#63D69A"
CHECKERBOARD_DARK = "#252A2E"
CHECKERBOARD_LIGHT = "#343A3F"
PRIMARY_ACTION_TEXT_COLOR = "#10140A"
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
        f"""
        QWidget {{ background: {BACKGROUND_COLOR}; color: {TEXT_COLOR};
                  font-family: '{UI_FONT}', sans-serif; font-size: 10pt; }}
        QFrame#preview_stage {{ background: {CANVAS_COLOR};
                                border: 1px solid {DIVIDER_COLOR}; }}
        QLabel#original_canvas, QLabel#result_canvas {{ background: {CANVAS_COLOR}; }}
        QWidget#inspector {{ background: {INSPECTOR_COLOR};
                             border-left: 1px solid {DIVIDER_COLOR}; }}
        QWidget#inspector_content {{ background: {INSPECTOR_COLOR}; }}
        QPushButton {{ background: {CONTROL_COLOR}; border: 1px solid {DIVIDER_COLOR};
                       border-radius: 4px; min-height: 40px; padding: 0 12px; }}
        QPushButton:hover {{ border-color: {HOVER_COLOR}; }}
        QPushButton:focus, QComboBox:focus, QAbstractSpinBox:focus,
        QLineEdit:focus, QCheckBox:focus, QToolButton:focus,
        QLabel#result_canvas:focus, QLabel#success_banner:focus {{
            outline: none; border: 2px solid {ACCENT_COLOR};
        }}
        QPushButton:disabled, QComboBox:disabled, QAbstractSpinBox:disabled,
        QLineEdit:disabled, QCheckBox:disabled {{ color: {DISABLED_COLOR}; }}
        QComboBox, QAbstractSpinBox, QLineEdit {{ background: {CONTROL_COLOR};
            border: 1px solid {DIVIDER_COLOR}; border-radius: 4px;
            min-height: 32px; padding: 0 8px;
            selection-background-color: {ACCENT_COLOR}; }}
        QComboBox::drop-down {{ border: 0; width: 28px; }}
        QLabel#result_canvas[status='stale'] {{ color: {WARNING_COLOR}; }}
        QLabel#result_canvas[status='error'] {{ color: {ERROR_COLOR}; }}
        QLabel#result_canvas[checkerboard='true'] {{
            background-color: {CHECKERBOARD_DARK};
        }}
        QFrame#success_banner_container {{ border-top: 1px solid {DIVIDER_COLOR}; }}
        QLabel#success_banner {{ color: {SUCCESS_COLOR}; }}
        QPushButton[primaryAction='true'] {{ background: {ACCENT_COLOR};
            color: {PRIMARY_ACTION_TEXT_COLOR}; border-color: {ACCENT_COLOR}; }}
        QLabel[secondary='true'] {{ color: {SECONDARY_COLOR}; }}
        QLabel[mono='true'], QAbstractSpinBox[mono='true'], QLineEdit[mono='true'] {{
            font-family: '{MONO_FONT}', monospace;
        }}
        QLabel#model_status[status='ready'] {{ color: {SUCCESS_COLOR}; }}
        QLabel#model_status[status='downloading'] {{ color: {WARNING_COLOR}; }}
        QLabel#model_status[status='not_cached'] {{ color: {SECONDARY_COLOR}; }}
        QToolButton {{ text-align: left; min-height: 40px; padding: 0 8px;
                       color: {TEXT_COLOR}; border: 0; font-weight: 600; }}
        QToolButton:hover {{ color: {HOVER_COLOR};
                              background: {CONTROL_COLOR}; }}
        QToolButton:checked {{ color: {TEXT_COLOR}; }}
        QScrollArea {{ border: 0; background: {INSPECTOR_COLOR}; }}
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
