from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QListWidget, QListWidgetItem

from matteloop.ui.aligned_rows import (
    ACCESSIBLE_DESCRIPTION_ROLE,
    STATUS_ROLE,
    AlignedColumn,
    AlignedRow,
    AlignedRowDelegate,
    install_aligned_row,
)


def test_aligned_row_delegate_exposes_cached_columns_and_status_words(qtbot) -> None:
    widget = QListWidget()
    qtbot.addWidget(widget)
    item = QListWidgetItem()
    row = AlignedRow(
        "✓",
        "cached",
        (AlignedColumn("U²-Net"), AlignedColumn("167.8 MiB", True)),
        "U²-Net; cached locally; human segmentation; MIT licence",
    )
    install_aligned_row(item, row)
    widget.addItem(item)

    index = widget.model().index(0, 0)
    delegate = AlignedRowDelegate(widget)
    assert delegate.row(index).columns == row.columns
    assert delegate.row(index).glyph == "✓"
    assert index.data(STATUS_ROLE) == "cached"
    assert "cached locally" in index.data(ACCESSIBLE_DESCRIPTION_ROLE)
    assert "cached locally" in index.data(Qt.ItemDataRole.AccessibleTextRole)


def test_aligned_row_delegate_exposes_uncached_columns_and_down_arrow(qtbot) -> None:
    widget = QListWidget()
    qtbot.addWidget(widget)
    item = QListWidgetItem()
    row = AlignedRow(
        "↓",
        "uncached",
        (AlignedColumn("BiRefNet Portrait"), AlignedColumn("927.6 MiB", True)),
        "BiRefNet Portrait; not cached yet; portrait matting; MIT licence",
    )
    install_aligned_row(item, row)
    widget.addItem(item)

    index = widget.model().index(0, 0)
    delegate = AlignedRowDelegate(widget)
    assert delegate.row(index).columns[1].right_aligned is True
    assert delegate.row(index).glyph == "↓"
    assert index.data(STATUS_ROLE) == "uncached"
    assert "not cached yet" in index.data(ACCESSIBLE_DESCRIPTION_ROLE)
