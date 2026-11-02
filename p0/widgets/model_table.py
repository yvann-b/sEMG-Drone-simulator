"""The model table: one row per ``Pm_i`` process.

Step 2 fills it with placeholder rows so the layout can be judged. Step 3 will
replace the placeholder storage with a ``QAbstractTableModel`` fed by a state
snapshot from the controller.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
)

#: Column index -> header label. Order matters: the view builds columns from it.
COLUMNS: tuple[str, ...] = (
    "Model",
    "Status",
    "Last label",
    "Confidence",
    "Age",
)

COL_MODEL, COL_STATUS, COL_LABEL, COL_CONFIDENCE, COL_AGE = range(len(COLUMNS))

#: Status text -> background colour. Anything unknown is drawn neutral.
STATUS_COLORS: dict[str, QColor] = {
    "running": QColor("#d9f2e0"),
    "training": QColor("#fdecc8"),
    "error": QColor("#fbd8d6"),
    "stopped": QColor("#e8e8e8"),
}

#: Placeholder content for step 2 — replaced by real model processes in step 3.
PLACEHOLDER_ROWS: tuple[tuple[str, str, str, str, str], ...] = (
    ("svm", "stopped", "—", "—", "—"),
    ("lda", "stopped", "—", "—", "—"),
    ("cnn", "stopped", "—", "—", "—"),
)


class ModelTable(QTableWidget):
    """Read-only table of every model process and its last classification.

    The table is non-editable and row-selecting: a single click picks a model
    (used later by the selector and by reload). Editing happens through code,
    never through the keyboard.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(0, len(COLUMNS), parent)

        self.setHorizontalHeaderLabels(COLUMNS)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)

        header = self.horizontalHeader()
        header.setSectionResizeMode(COL_MODEL, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_LABEL, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_CONFIDENCE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_AGE, QHeaderView.ResizeMode.ResizeToContents)

        self.set_placeholder_data()

    def set_placeholder_data(self) -> None:
        """Fill the table with the step-2 dummy rows."""
        self.setRowCount(0)
        for row in PLACEHOLDER_ROWS:
            self._append_row(row)

    def _append_row(self, values: tuple[str, str, str, str, str]) -> None:
        row = self.rowCount()
        self.insertRow(row)
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == COL_MODEL:
                item.setData(Qt.ItemDataRole.UserRole, value)
            if column in (COL_STATUS, COL_LABEL, COL_CONFIDENCE, COL_AGE):
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setItem(row, column, item)
        self._paint_status(row)

    def _paint_status(self, row: int) -> None:
        item = self.item(row, COL_STATUS)
        if item is None:
            return
        color = STATUS_COLORS.get(item.text().lower())
        if color is not None:
            item.setBackground(color)