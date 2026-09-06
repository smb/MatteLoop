from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QListWidget, QListWidgetItem

from matteloop.ui.aligned_rows import (
    ACCESSIBLE_DESCRIPTION_ROLE,
    STATUS_ROLE,
    AlignedColumn,
    AlignedRow,
    AlignedRowDelegate,
    RowStatus,
    _status_color,
    install_aligned_row,
)
from matteloop.ui.theme import ACCENT_COLOR, SECONDARY_COLOR


def test_aligned_row_delegate_exposes_cached_columns_and_status_words(qtbot) -> None:
    widget = QListWidget()
    qtbot.addWidget(widget)
    item = QListWidgetItem()
    row = AlignedRow(
        "✓",
        RowStatus.CACHED,
        (AlignedColumn("U²-Net"), AlignedColumn("167.8 MiB", True)),
        "U²-Net; cached locally; human segmentation; MIT licence",
    )
    install_aligned_row(item, row)
    widget.addItem(item)

    index = widget.model().index(0, 0)
    delegate = AlignedRowDelegate(widget)
    assert delegate.row(index).columns == row.columns
    assert delegate.row(index).glyph == "✓"
    assert index.data(STATUS_ROLE) is RowStatus.CACHED
    assert "cached locally" in index.data(ACCESSIBLE_DESCRIPTION_ROLE)
    assert "cached locally" in index.data(Qt.ItemDataRole.AccessibleTextRole)


def test_aligned_row_delegate_exposes_uncached_columns_and_down_arrow(qtbot) -> None:
    widget = QListWidget()
    qtbot.addWidget(widget)
    item = QListWidgetItem()
    row = AlignedRow(
        "↓",
        RowStatus.UNCACHED,
        (AlignedColumn("BiRefNet Portrait"), AlignedColumn("927.6 MiB", True)),
        "BiRefNet Portrait; not cached yet; portrait matting; MIT licence",
    )
    install_aligned_row(item, row)
    widget.addItem(item)

    index = widget.model().index(0, 0)
    delegate = AlignedRowDelegate(widget)
    assert delegate.row(index).columns[1].right_aligned is True
    assert delegate.row(index).glyph == "↓"
    assert index.data(STATUS_ROLE) is RowStatus.UNCACHED
    assert "not cached yet" in index.data(ACCESSIBLE_DESCRIPTION_ROLE)


def test_row_accent_uses_status_token_when_status_copy_changes() -> None:
    row = AlignedRow(
        "✓",
        RowStatus.CACHED,
        (AlignedColumn("U²-Net"),),
        "U²-Net",
    )

    assert _status_color(row.status) == QColor(ACCENT_COLOR)
    assert _status_color(RowStatus.UNCACHED) == QColor(SECONDARY_COLOR)
