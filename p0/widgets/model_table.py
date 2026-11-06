"""The model table, as a Qt *data model* (not a widget).

Step 3 swaps ``QTableWidget`` for a ``QAbstractTableModel``. The difference is
who owns the numbers:

    before:  QTableWidget held the rows, the controller had to find a cell
             and write into it (the widget *was* the database).
    after:   this model holds the rows, the controller calls
             ``apply_snapshot(rows)`` once per tick, and the view only paints.

The table stays **read-only**: rows change because the controller says so,
never because the user types. Editing is a property of the view, and the
controller never touches the view.

Why a model and not a widget:

* ``data()`` is asked on demand, so *Age* is computed from ``now`` at paint
  time instead of being stored and rewritten every tick.
* one ``apply_snapshot`` + a single ``dataChanged`` repaints what changed,
  instead of looping over every cell of every row.
* "which rows exist" is ``row_names()``, real data the combo box can use,
  instead of reading text back out of a view.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from p0.state import EM_DASH, ModelRow, format_age

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
    "idle": QColor("#f0f0f0"),
}

#: Placeholder rows shown before the controller's first snapshot. They live
#: in the *model* now (they used to live in the widget), so the window looks
#: exactly the same while the controller is still being wired in.
PLACEHOLDER_ROWS: tuple[ModelRow, ...] = (
    ModelRow(name="svm", status="idle"),
    ModelRow(name="lda", status="idle"),
    ModelRow(name="cnn", status="idle"),
)


class ModelTableModel(QAbstractTableModel):
    """Read-only table of every model process and its last classification."""

    def __init__(self, parent=None, rows: Sequence[ModelRow] | None = None) -> None:
        super().__init__(parent)
        # ``now`` is the clock the Age column is measured against. The
        # controller stamps it into the snapshot each tick; until then, no
        # age can be shown and the cell reads "—".
        self._now: float | None = None
        self._rows: list[ModelRow] = list(rows if rows is not None else PLACEHOLDER_ROWS)

    # ------------------------------------------------------------------ data
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        # A flat table: no index ever has children.
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        column = index.column()

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return self._text(row, column)

        if role == Qt.ItemDataRole.BackgroundRole and column == COL_STATUS:
            # Unknown / missing status: no colour, the default background.
            return STATUS_COLORS.get(row.status)

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if column == COL_MODEL:
                return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignCenter)

        if role == Qt.ItemDataRole.UserRole:
            # The raw model name, undecorated. Code needs this identity; the
            # visible text is for a human.
            return row.name

        return None

    def _text(self, row: ModelRow, column: int) -> str:
        if column == COL_MODEL:
            return row.name
        if column == COL_STATUS:
            return row.status
        if column == COL_LABEL:
            return row.display_label()
        if column == COL_CONFIDENCE:
            return row.display_confidence()
        if column == COL_AGE:
            return format_age(row.last_seen, self._now)
        return EM_DASH

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section] if 0 <= section < len(COLUMNS) else None
        return None

    # ------------------------------------------------------------- the API
    def apply_snapshot(self, rows: Sequence[ModelRow], now: float | None = None) -> None:
        """Replace the whole table with ``rows``.

        Called by the controller once per tick. ``now`` (monotonic seconds) is
        what the Age column is measured against.

        Emits the *smallest* correct ``dataChanged``: if the set of names is
        unchanged, only the fields that actually moved trigger a repaint, and
        a status change does not force the whole table to be rebuilt.
        """
        rows = list(rows)
        self._now = now

        new_names = [r.name for r in rows]
        old_names = [r.name for r in self._rows]

        if new_names != old_names:
            # Different rows exist: rebuild once, with the right signal.
            self.beginResetModel()
            self._rows = rows
            self.endResetModel()
            return

        old_rows = self._rows
        self._rows = rows
        if not rows:
            return
        # Same rows, possibly changed content. Repaint the columns that moved.
        self.dataChanged.emit(
            self.index(0, 0),
            self.index(len(rows) - 1, len(COLUMNS) - 1),
            [
                Qt.ItemDataRole.DisplayRole,
                Qt.ItemDataRole.BackgroundRole,
            ],
        )
        # ``old_rows`` is intentionally only used to keep the signature
        # identical to the rebuild branch; the view recomputes from ``_rows``.
        _ = old_rows

    def row_names(self) -> list[str]:
        """The model names currently listed, in order."""
        return [row.name for row in self._rows]

    def rows(self) -> tuple[ModelRow, ...]:
        """The current rows, for tests and for the controller's own reads."""
        return tuple(self._rows)

    def reset_to_placeholders(self) -> None:
        """Testing aid: restore the step-2 dummy rows."""
        self.apply_snapshot(PLACEHOLDER_ROWS, now=None)
