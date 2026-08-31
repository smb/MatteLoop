"""Shared aligned-row painting for model and durable cut-set lists."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from matteloop.ui.theme import ACCENT_COLOR, SECONDARY_COLOR

ROW_DATA_ROLE = int(Qt.ItemDataRole.UserRole) + 1
ACCESSIBLE_DESCRIPTION_ROLE = int(Qt.ItemDataRole.UserRole) + 2
STATUS_ROLE = int(Qt.ItemDataRole.UserRole) + 3
type ModelIndex = QModelIndex | QPersistentModelIndex


@dataclass(frozen=True, slots=True)
class AlignedColumn:
    text: str
    right_aligned: bool = False


@dataclass(frozen=True, slots=True)
class AlignedRow:
    """Text and semantics for one delegate-painted row."""

    glyph: str
    status: str
    columns: tuple[AlignedColumn, ...]
    accessible_description: str

    @property
    def display_text(self) -> str:
        return self.columns[0].text if self.columns else ""


def install_aligned_row(
    item: object, row: AlignedRow, *, index: int | None = None
) -> None:
    """Attach row data to a QListWidgetItem or QComboBox item model item."""
    values = {
        ROW_DATA_ROLE: row,
        STATUS_ROLE: row.status,
        Qt.ItemDataRole.ToolTipRole: row.accessible_description,
        ACCESSIBLE_DESCRIPTION_ROLE: row.accessible_description,
        Qt.ItemDataRole.AccessibleTextRole: row.accessible_description,
    }
    if index is None:
        setter = getattr(item, "setData", None)
        if not callable(setter):
            raise TypeError("item must support setData")
        setter(Qt.ItemDataRole.DisplayRole, row.display_text)
        for role, value in values.items():
            setter(role, value)
        return
    setter = getattr(item, "setItemData", None)
    if not callable(setter):
        raise TypeError("indexed item must support setItemData")
    for role, value in values.items():
        setter(index, value, role)


def status_icon(row: AlignedRow) -> QIcon:
    """Create the compact status icon used by the closed model combo box."""
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(_status_color(row.status))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, row.glyph)
    painter.end()
    return QIcon(pixmap)


class AlignedRowDelegate(QStyledItemDelegate):
    """Paint a glyph plus columns with a stable, comparable right edge."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: ModelIndex,
    ) -> None:
        row = self.row(index)
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(
                option.rect, option.palette.brush(QPalette.ColorRole.Highlight)
            )
        rect = option.rect.adjusted(12, 0, -12, 0)
        widths = self._column_widths(index, len(row.columns), option)
        glyph_width = 24
        gap = 12
        painter.setPen(_status_color(row.status))
        painter.drawText(
            rect.left(), rect.top(), glyph_width, rect.height(),
            Qt.AlignmentFlag.AlignCenter, row.glyph,
        )
        left = rect.left() + glyph_width + gap
        for column, width in zip(row.columns, widths, strict=False):
            column_rect = rect.adjusted(left - rect.left(), 0, 0, 0)
            column_rect.setLeft(left)
            column_rect.setWidth(width)
            painter.setPen(option.palette.color(QPalette.ColorRole.Text))
            alignment = (
                Qt.AlignmentFlag.AlignRight
                if column.right_aligned
                else Qt.AlignmentFlag.AlignLeft
            ) | Qt.AlignmentFlag.AlignVCenter
            text = option.fontMetrics.elidedText(
                column.text, Qt.TextElideMode.ElideRight, max(0, width)
            )
            painter.drawText(column_rect, alignment, text)
            left += width + gap
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: ModelIndex) -> QSize:
        row = self.row(index)
        widths = self._column_widths(index, len(row.columns), option)
        return QSize(
            sum(widths) + 24 + 12 * (len(widths) + 1),
            max(32, option.fontMetrics.height() + 14),
        )

    @staticmethod
    def row(index: ModelIndex) -> AlignedRow:
        row = index.data(ROW_DATA_ROLE)
        if not isinstance(row, AlignedRow):
            return AlignedRow("", "", (AlignedColumn(str(index.data() or "")),), "")
        return row

    def _column_widths(
        self,
        index: ModelIndex,
        count: int,
        option: QStyleOptionViewItem,
    ) -> tuple[int, ...]:
        if count == 0:
            return ()
        model = index.model()
        widths = [0] * count
        for row_index in range(model.rowCount()):
            row = self.row(model.index(row_index, index.column()))
            for column_index, column in enumerate(row.columns[:count]):
                widths[column_index] = max(
                    widths[column_index],
                    option.fontMetrics.horizontalAdvance(column.text) + 8,
                )
        available = max(0, option.rect.width() - 24 - 12 * (count + 1))
        if sum(widths) > available:
            widths[-1] = max(48, available - sum(widths[:-1]))
        elif sum(widths) < available:
            widths[-1] += available - sum(widths)
        return tuple(widths)


def _status_color(status: str) -> QColor:
    token = (
        ACCENT_COLOR
        if status in {"cached", "edited", "pinned"}
        else SECONDARY_COLOR
    )
    return QColor(token)
