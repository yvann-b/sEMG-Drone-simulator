"""The gesture table, as a Qt *data model* (an editable table).

One row per **active gesture**. Columns:

    Gesture | Image | Preview | Repetitions

* **Gesture** — the label name (editable text).
* **Image** — the bare image filename under ``images/`` (editable text); the
  cell shows a red tint when the file is missing so the user notices.
* **Preview** — a thumbnail of that image (read-only; ``DecorationRole``). A
  placeholder pixmap when the filename is empty or the file is missing.
* **Repetitions** — how many times this gesture is performed (editable int).

The optional ``noise`` gesture is **not** a hand-typed row: the window adds it
with :meth:`set_noise_enabled` when the checkbox is ticked, and it is drawn
greyed and non-editable so it reads as a built-in. ``rest`` is never a row —
it is the implicit separator in :class:`~p0.calibration_config.CalibrationConfig`.

This mirrors ``model_table.py``: the controller/window talk to the model, the
view only paints. The model is the single place the rows live.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor, QPixmap

from p0.calibration_config import (
    NOISE_LABEL,
    GestureSpec,
    image_exists,
    image_path,
)

#: Column index -> header label. Order matters: the view builds columns from it.
COLUMNS: tuple[str, ...] = ("Gesture", "Image", "Preview", "Repetitions")

COL_NAME, COL_IMAGE, COL_PREVIEW, COL_REPETITIONS = range(len(COLUMNS))

#: Size of the in-row thumbnail, in pixels (square).
THUMBNAIL_SIZE = 48

#: Background tint for an Image cell whose file does not exist.
MISSING_IMAGE_COLOR = QColor("#fbd8d6")

#: Background tint for the built-in noise row's name cell.
BUILTIN_COLOR = QColor("#eef0f2")


def _placeholder_pixmap() -> QPixmap:
    """A neutral "no image" swatch for empty or missing previews."""
    pixmap = QPixmap(THUMBNAIL_SIZE, THUMBNAIL_SIZE)
    pixmap.fill(QColor("#e2e2e2"))
    return pixmap


class GestureTableModel(QAbstractTableModel):
    """Editable table of the user's gestures.

    The window keeps a :class:`~p0.calibration_config.CalibrationConfig` in sync
    with this model: read :meth:`gestures` to snapshot the rows, call
    :meth:`set_gestures` to replace them (on Load), and connect to
    :attr:`changed` to be told the recipe was edited.
    """

    #: Emitted after any edit, add, delete, or noise toggle.
    changed = Signal()

    def __init__(self, parent=None, gestures: Sequence[GestureSpec] | None = None) -> None:
        super().__init__(parent)
        #: The *user* rows only; the noise row is derived from ``_noise_enabled``
        #: so it can never be duplicated or hand-edited.
        self._gestures: list[GestureSpec] = list(gestures or [])
        self._noise_enabled = False

    # -------------------------------------------------------------- geometry
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows())

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def _rows(self) -> list[GestureSpec]:
        """User rows plus the derived noise row, exactly as displayed."""
        rows = list(self._gestures)
        if self._noise_enabled:
            rows.append(GestureSpec(name=NOISE_LABEL, image=None, repetitions=1))
        return rows

    def _is_noise_index(self, row: int) -> bool:
        return self._noise_enabled and row == len(self._gestures)

    # ------------------------------------------------------------------ data
    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        gesture = self._rows()[index.row()]
        column = index.column()
        is_noise = self._is_noise_index(index.row())

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return self._text(gesture, column)

        if role == Qt.ItemDataRole.DecorationRole and column == COL_PREVIEW:
            return self._thumbnail(gesture.image)

        if role == Qt.ItemDataRole.BackgroundRole:
            if is_noise and column == COL_NAME:
                return BUILTIN_COLOR
            if column == COL_IMAGE and gesture.image and not image_exists(gesture.image):
                return MISSING_IMAGE_COLOR
            return None

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if column in (COL_NAME, COL_IMAGE):
                return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignCenter)

        if role == Qt.ItemDataRole.ToolTipRole and column == COL_IMAGE and gesture.image:
            return str(image_path(gesture.image))

        return None

    def _text(self, gesture: GestureSpec, column: int) -> str:
        if column == COL_NAME:
            return gesture.name
        if column == COL_IMAGE:
            return gesture.image or ""
        if column == COL_PREVIEW:
            return ""
        if column == COL_REPETITIONS:
            return str(gesture.repetitions)
        return ""

    def _thumbnail(self, filename: str | None) -> QPixmap:
        resolved = image_path(filename)
        if resolved is not None and resolved.is_file():
            pixmap = QPixmap(str(resolved))
            if not pixmap.isNull():
                return pixmap.scaled(
                    THUMBNAIL_SIZE,
                    THUMBNAIL_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
        return _placeholder_pixmap()

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

    # -------------------------------------------------------------- editing
    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        column = index.column()
        # Preview is display-only; the built-in noise row is not hand-edited.
        if column == COL_PREVIEW or self._is_noise_index(index.row()):
            return base
        return base | Qt.ItemFlag.ItemIsEditable

    def setData(self, index: QModelIndex, value, role: int = Qt.ItemDataRole.EditRole) -> bool:  # noqa: N802
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False
        if self._is_noise_index(index.row()) or index.column() == COL_PREVIEW:
            return False

        gesture = self._gestures[index.row()]
        column = index.column()
        if column == COL_NAME:
            gesture.name = str(value).strip()
        elif column == COL_IMAGE:
            text = str(value).strip()
            gesture.image = text or None
        elif column == COL_REPETITIONS:
            try:
                gesture.repetitions = max(1, int(value))
            except (TypeError, ValueError):
                return False
        else:
            return False

        self.dataChanged.emit(index, index, [])
        self.changed.emit()
        return True

    # ----------------------------------------------------- row operations API
    def add_gesture(self, gesture: GestureSpec | None = None) -> int:
        """Append a gesture row and return its index."""
        row = len(self._gestures)
        self.beginInsertRows(QModelIndex(), row, row)
        self._gestures.append(gesture or GestureSpec(name="", image=None, repetitions=5))
        self.endInsertRows()
        self.changed.emit()
        return row

    def remove_gesture(self, row: int) -> bool:
        """Delete one full user row. The built-in noise row cannot be deleted."""
        if not (0 <= row < len(self._gestures)):
            return False
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._gestures[row]
        self.endRemoveRows()
        self.changed.emit()
        return True

    def set_noise_enabled(self, enabled: bool) -> None:
        """Show or hide the built-in ``noise`` row (driven by the checkbox)."""
        if enabled == self._noise_enabled:
            return
        self._noise_enabled = enabled
        self.layoutChanged.emit()
        self.changed.emit()

    def set_gestures(self, gestures: Sequence[GestureSpec], noise_enabled: bool) -> None:
        """Replace every row at once (used when loading a configuration)."""
        self.beginResetModel()
        self._gestures = list(gestures)
        self._noise_enabled = noise_enabled
        self.endResetModel()

    def set_repetitions_all(self, value: int) -> None:
        """Set every user row's repetitions (the window's global field).

        The built-in noise row is not a user row and is left untouched. Emits a
        single ``dataChanged`` for the whole Repetitions column so the view
        repaints once.
        """
        value = max(1, int(value))
        if not self._gestures:
            return
        for gesture in self._gestures:
            gesture.repetitions = value
        top = self.index(0, COL_REPETITIONS)
        bottom = self.index(len(self._gestures) - 1, COL_REPETITIONS)
        self.dataChanged.emit(top, bottom, [Qt.ItemDataRole.DisplayRole])
        self.changed.emit()

    # --------------------------------------------------------------- readers
    def gestures(self) -> list[GestureSpec]:
        """The **user** gestures only (noise/rest excluded) — for saving."""
        return [
            GestureSpec(name=g.name, image=g.image, repetitions=g.repetitions)
            for g in self._gestures
        ]

    def is_noise_row(self, row: int) -> bool:
        """``True`` if ``row`` is the built-in noise row."""
        return self._is_noise_index(row)
