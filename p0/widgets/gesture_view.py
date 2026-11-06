"""The gesture table *view*: how :class:`GestureTableModel` rows are drawn.

Display configuration only — editable cells, taller rows so the in-row
thumbnail fits, column widths. It holds no data; ask the model for that. The
window talks to the model, never to this class.
"""

from __future__ import annotations

from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView

from p0.widgets.gesture_table import (
    COL_IMAGE,
    COL_NAME,
    COL_PREVIEW,
    COL_REPETITIONS,
    THUMBNAIL_SIZE,
)


class GestureView(QTableView):
    """Editable table of gestures, bound to a :class:`GestureTableModel`."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        # This one IS editable: the user types gesture names, images, reps.
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        # A click selects the whole row (a gesture), not a single cell.
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(True)  # row numbers help delete the right line

        # Tall enough for the thumbnail plus breathing room.
        self.verticalHeader().setDefaultSectionSize(THUMBNAIL_SIZE + 8)

        header = self.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_IMAGE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_PREVIEW, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(COL_PREVIEW, THUMBNAIL_SIZE + 16)
        header.setSectionResizeMode(COL_REPETITIONS, QHeaderView.ResizeMode.ResizeToContents)
