"""Accessible disclosure-button behavior shared by inspector sections."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QSettings, Qt
from PySide6.QtWidgets import QSizePolicy, QToolButton


def read_bool(settings: QSettings, name: str, default: bool) -> bool:
    """Read a settings value expected to be an exact bool, degrading to default."""
    value = settings.value(name, default)
    return value if type(value) is bool else default


def configure_disclosure(
    button: QToolButton, key: str, title: str, expanded: bool
) -> None:
    """Configure one inspector header and keep its state spoken and visible."""
    button.setText(title.replace("&", "&&"))
    button.setCheckable(True)
    button.setObjectName(f"{key}_disclosure")
    button.setAccessibleName(title)
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    # The whole header bar is the target, not only the width of its text.
    button.setSizePolicy(
        QSizePolicy.Policy.Expanding, button.sizePolicy().verticalPolicy()
    )
    button.toggled.connect(
        lambda value: update_disclosure(button, title, value)
    )
    button.setChecked(expanded)
    update_disclosure(button, title, expanded)


def update_disclosure(button: QToolButton, title: str, expanded: bool) -> None:
    """Set the disclosure arrow and non-visual state wording together."""
    state = (
        QCoreApplication.translate("InspectorDisclosure", "expanded")
        if expanded
        else QCoreApplication.translate("InspectorDisclosure", "collapsed")
    )
    button.setArrowType(
        Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
    )
    button.setAccessibleDescription(f"{title}: {state}")
    button.setToolTip(f"{title} ({state})")
