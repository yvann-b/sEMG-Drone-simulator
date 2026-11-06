"""The model table *view*: how the rows of :class:`ModelTableModel` are drawn.

Everything here is display configuration — column widths, selection, zebra
striping. It holds no data: ask the model for that. The controller must never
touch this class; if it needs something to change, it calls
``ModelTableModel.apply_snapshot``.
"""

from __future__ import annotations

from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView

from p0.widgets.model_table import (
    COL_AGE,
    COL_CONFIDENCE,
    COL_LABEL,
    COL_MODEL,
    COL_STATUS,
)


class ModelView(QTableView):
    """Read-only table of models, bound to a :class:`ModelTableModel`."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        # --- how the user may interact -------------------------------
        # Rows are fed by code, never typed by hand, so editing is disabled.
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # A click selects the whole row (a model), not the single cell, and
        # only one row at a time: the selection answers "which model is this".
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # The eye loses the row between two distant cells; alternation fixes it.
        self.setAlternatingRowColors(True)
        # Row numbers would be noise: a row is identified by its model name.
        self.verticalHeader().setVisible(False)

        # --- column widths -------------------------------------------
        # Exactly one column may Stretch (it absorbs the leftover width) --
        # here the free-text one, Last label. Give a second column Stretch
        # and Qt would share the slack between them.
        header = self.horizontalHeader()
        header.setSectionResizeMode(COL_MODEL, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_LABEL, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_CONFIDENCE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_AGE, QHeaderView.ResizeMode.ResizeToContents)
