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
        # 0 rows for now, one column per entry in COLUMNS. Pass len(COLUMNS)
        # rather than 5 so adding a column is a one-line change in COLUMNS.
        super().__init__(0, len(COLUMNS), parent)

        self.setHorizontalHeaderLabels(COLUMNS)

        # --- how the user may interact -------------------------------
        # Double-clicking a cell would normally open an editor. Model rows are
        # fed by code, never typed by hand, so editing is disabled outright.
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # A click selects the whole row (a model), not the single cell under
        # the cursor, and only one row at a time: the selection is "which model
        # is this", there is no multi-model action anywhere.
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # Zebra striping: with 5 columns the eye loses the row between two
        # distant cells. The alternation keeps a line readable across.
        self.setAlternatingRowColors(True)
        # Row numbers on the left would be noise: a row is identified by its
        # model name, not by its index.
        self.verticalHeader().setVisible(False)

        # --- column widths -------------------------------------------
        # ResizeToContents: the column fits its widest text and grows with it.
        # Stretch: the column absorbs all the leftover width, so exactly one
        # column may use it -- here the free-text one, Last label. Give a
        # second column Stretch and Qt would share the slack between them.
        header = self.horizontalHeader()
        header.setSectionResizeMode(COL_MODEL, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_LABEL, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_CONFIDENCE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_AGE, QHeaderView.ResizeMode.ResizeToContents)

        # Step 2 only: real data arrives in step 3, from the controller.
        self.set_placeholder_data()

    def set_placeholder_data(self) -> None:
        """Fill the table with the step-2 dummy rows.

        Testing aid only: this is what step 3 replaces with real rows coming
        from the controller. Clearing first (``setRowCount(0)``) makes the
        method safe to call twice -- otherwise each call would append again.
        """
        self.setRowCount(0)
        for row in PLACEHOLDER_ROWS:
            self._append_row(row)

    def _append_row(self, values: tuple[str, str, str, str, str]) -> None:
        """Add one brand-new row at the bottom and fill its cells.

        Nothing is edited here: the row does not exist yet, it is *created*.
        ``rowCount()`` is the number of existing rows, which is also the index
        where the next one goes -- hence a new row always lands at the end.
        """
        row = self.rowCount()
        self.insertRow(row)
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            # Keep the raw model name on the item itself. The visible text is
            # what the user reads; this role is for code that needs the "name
            # without decoration" later (step 3/4), and survives any relabel.
            if column == COL_MODEL:
                item.setData(Qt.ItemDataRole.UserRole, value)
            # Only the wide text column is left-aligned; short values (status,
            # confidence, age) look tidier centred under their header.
            if column in (COL_STATUS, COL_LABEL, COL_CONFIDENCE, COL_AGE):
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setItem(row, column, item)
        # Colour depends on the status text, so it must run after the cells
        # hold their values -- never before.
        self._paint_status(row)

    def _paint_status(self, row: int) -> None:
        """Colour the Status cell according to the status word it contains.

        Cosmetic only: the data is already correct before this runs, and
        deleting every call would leave a valid, merely colourless table.
        An unknown status (or an empty cell) is simply left unpainted.
        """
        item = self.item(row, COL_STATUS)
        if item is None:
            return
        color = STATUS_COLORS.get(item.text().lower())
        if color is not None:
            item.setBackground(color)