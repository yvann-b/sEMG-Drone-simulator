"""The **Configure calibration** window.

A dedicated ``QDialog`` opened from P0's main window. It edits one
:class:`~p0.calibration_config.CalibrationConfig` — a recording *recipe* — and
saves/loads it as JSON under ``calibrations/``. It touches no process, ring or
queue: it is pure GUI producing a plain dataclass.

Layout (kept deliberately simple):

    Name: [________________]   Movement time (s): [ ]   Rest time (s): [ ]
    Image folder: <resolved path>            [x] Noise gesture

    [Add gesture] [Delete row]                 [Load…] [Save] [Save as…]
    ┌──────────┬───────────┬────────┬──────────────┐
    │ Gesture  │ Image     │ Preview│ Repetitions  │
    │ fist     │ fist.png  │ [img]  │ 5            │
    └──────────┴───────────┴────────┴──────────────┘
    This recording will contain: fist, point, noise, rest
    <validation messages>                              [Cancel] [OK]

The window owns a :class:`GestureTableModel`; the fields, the noise checkbox
and the table all feed a single :meth:`current_config`, which is validated on
OK. An **unsaved-changes guard** asks before discarding edits (close, load, or
open-another).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from p0.calibration_config import (
    CALIBRATIONS_DIR,
    IMAGES_DIR,
    REST_LABEL,
    CalibrationConfig,
)
from p0.widgets.gesture_table import GestureTableModel
from p0.widgets.gesture_view import GestureView


class CalibrationWindow(QDialog):
    """Modal editor for one calibration configuration file."""

    def __init__(self, parent=None, config: CalibrationConfig | None = None,
                 path: Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Configure calibration")
        self.resize(720, 520)

        #: The file this window is bound to (``None`` until first Save as…).
        self._path: Path | None = Path(path) if path else None
        #: Set by every edit; drives the unsaved-changes guard.
        self._dirty = False

        self._build_ui()
        self._connect_signals()

        if config is not None:
            self._load_into_fields(config)
        self._refresh_summary()
        self._mark_clean()

    # ------------------------------------------------------------------ build
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # --- top: name, times, noise --------------------------------
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. patient_a_session1")
        form.addRow("Name:", self.name_edit)

        self.movement_spin = QDoubleSpinBox()
        self.movement_spin.setRange(0.1, 120.0)
        self.movement_spin.setDecimals(1)
        self.movement_spin.setSuffix(" s")
        self.movement_spin.setValue(3.0)
        form.addRow("Movement time:", self.movement_spin)

        self.rest_spin = QDoubleSpinBox()
        self.rest_spin.setRange(0.1, 120.0)
        self.rest_spin.setDecimals(1)
        self.rest_spin.setSuffix(" s")
        self.rest_spin.setValue(2.0)
        form.addRow("Rest time:", self.rest_spin)

        self.repetitions_spin = QSpinBox()
        self.repetitions_spin.setRange(1, 999)
        self.repetitions_spin.setValue(5)
        self.repetitions_spin.setToolTip(
            "Global number of repetitions: how many times each gesture is performed.\n"
            "Changing it sets every gesture row; a single row can still be edited."
        )
        form.addRow("Repetitions:", self.repetitions_spin)

        self.noise_check = QCheckBox("Noise gesture (arm moves, no real gesture)")
        self.noise_check.setToolTip(
            "Adds a built-in 'noise' gesture so a rest-noise-rest slot is "
            "possible. Counted as a gesture in the label set."
        )
        form.addRow("", self.noise_check)
        root.addLayout(form)

        self.images_hint = QLabel()
        self.images_hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.images_hint.setStyleSheet("color: #666;")
        root.addWidget(self.images_hint)

        # --- toolbar ------------------------------------------------
        toolbar = QHBoxLayout()
        self.add_button = QPushButton("Add gesture")
        self.delete_button = QPushButton("Delete row")
        self.load_button = QPushButton("Load…")
        self.save_button = QPushButton("Save")
        self.save_as_button = QPushButton("Save as…")
        toolbar.addWidget(self.add_button)
        toolbar.addWidget(self.delete_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.load_button)
        toolbar.addWidget(self.save_button)
        toolbar.addWidget(self.save_as_button)
        root.addLayout(toolbar)

        # --- table --------------------------------------------------
        self.model = GestureTableModel(self)
        self.view = GestureView()
        self.view.setModel(self.model)
        root.addWidget(self.view, stretch=1)

        # --- summary + validation ----------------------------------
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label)

        self.validation_label = QLabel()
        self.validation_label.setWordWrap(True)
        self.validation_label.setStyleSheet("color: #b42318;")
        root.addWidget(self.validation_label)

        # --- OK / Cancel -------------------------------------------
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        root.addWidget(self.buttons)

    def _connect_signals(self) -> None:
        self.add_button.clicked.connect(self._on_add)
        self.delete_button.clicked.connect(self._on_delete)
        self.load_button.clicked.connect(self._on_load)
        self.save_button.clicked.connect(self._on_save)
        self.save_as_button.clicked.connect(self._on_save_as)
        self.buttons.accepted.connect(self._on_ok)
        self.buttons.rejected.connect(self.reject)

        # Any edit marks the recipe dirty and refreshes the summary.
        self.name_edit.textChanged.connect(self._on_edited)
        self.movement_spin.valueChanged.connect(self._on_edited)
        self.rest_spin.valueChanged.connect(self._on_edited)
        self.repetitions_spin.valueChanged.connect(self._on_global_repetitions_changed)
        self.noise_check.toggled.connect(self._on_noise_toggled)
        self.model.changed.connect(self._on_edited)

    # -------------------------------------------------------------- helpers
    def _on_edited(self) -> None:
        self._dirty = True
        self._refresh_summary()

    def _on_noise_toggled(self, checked: bool) -> None:
        self.model.set_noise_enabled(checked)
        self._on_edited()

    def _on_global_repetitions_changed(self, value: int) -> None:
        """Bulk-apply the global repetition count to every gesture row."""
        self.model.set_repetitions_all(int(value))
        self._on_edited()

    def _refresh_summary(self) -> None:
        self.images_hint.setText(f"Image folder: {IMAGES_DIR}")
        labels = self.current_config().labels()
        if labels:
            self.summary_label.setText(
                "This recording will contain: " + ", ".join(labels)
            )
        else:
            self.summary_label.setText("No gestures yet — add at least one.")
        self._refresh_validation()

    def _refresh_validation(self) -> None:
        problems = self.current_config().validate()
        # Only show blocking problems in red; warnings are grey-ish but still shown.
        if problems:
            self.validation_label.setText("\n".join(problems))
            blocking = any(not p.startswith("Warning:") for p in problems)
            self.validation_label.setStyleSheet(
                "color: #b42318;" if blocking else "color: #9a6700;"
            )
        else:
            self.validation_label.setText("")

    def _mark_clean(self) -> None:
        self._dirty = False

    def _confirm_discard(self) -> bool:
        """Return ``True`` if it is OK to lose the current edits."""
        if not self._dirty:
            return True
        answer = QMessageBox.question(
            self,
            "Discard changes?",
            "This configuration has unsaved changes. Discard them?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Discard

    # ---------------------------------------------------------- field <-> cfg
    def _load_into_fields(self, config: CalibrationConfig) -> None:
        """Fill the widgets from ``config`` without emitting spurious edits."""
        self.name_edit.blockSignals(True)
        self.movement_spin.blockSignals(True)
        self.rest_spin.blockSignals(True)
        self.repetitions_spin.blockSignals(True)
        self.noise_check.blockSignals(True)
        try:
            self.name_edit.setText(config.name)
            self.movement_spin.setValue(config.movement_time_s)
            self.rest_spin.setValue(config.rest_time_s)
            self.repetitions_spin.setValue(max(1, int(config.default_repetitions)))
            self.noise_check.setChecked(config.noise_enabled)
            self.model.set_gestures(config.gestures, config.noise_enabled)
        finally:
            self.name_edit.blockSignals(False)
            self.movement_spin.blockSignals(False)
            self.rest_spin.blockSignals(False)
            self.repetitions_spin.blockSignals(False)
            self.noise_check.blockSignals(False)
        self._refresh_summary()

    def current_config(self) -> CalibrationConfig:
        """Resolve the widgets into a :class:`CalibrationConfig` right now."""
        return CalibrationConfig(
            name=self.name_edit.text().strip(),
            movement_time_s=self.movement_spin.value(),
            rest_time_s=self.rest_spin.value(),
            default_repetitions=self.repetitions_spin.value(),
            noise_enabled=self.noise_check.isChecked(),
            gestures=self.model.gestures(),
        )

    @property
    def path(self) -> Path | None:
        """The file this window is bound to, if any."""
        return self._path

    # ------------------------------------------------------------- actions
    def _on_add(self) -> None:
        row = self.model.add_gesture()
        self.view.selectRow(row)
        self.view.edit(self.model.index(row, 0))

    def _on_delete(self) -> None:
        index = self.view.currentIndex()
        if not index.isValid():
            return
        row = index.row()
        if self.model.is_noise_row(row):
            QMessageBox.information(
                self, "Built-in row",
                "The 'noise' row is controlled by its checkbox and cannot be deleted here.",
            )
            return
        self.model.remove_gesture(row)

    def _on_load(self) -> None:
        if not self._confirm_discard():
            return
        start = str(CALIBRATIONS_DIR if CALIBRATIONS_DIR.is_dir() else Path.home())
        filename, _ = QFileDialog.getOpenFileName(
            self, "Load calibration configuration", start, "Calibration (*.json)"
        )
        if not filename:
            return
        try:
            config = CalibrationConfig.load(filename)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Cannot load", f"{filename}\n\n{exc}")
            return
        self._path = Path(filename)
        self._load_into_fields(config)
        self._mark_clean()

    def _on_save(self) -> None:
        if self._path is None:
            self._on_save_as()
            return
        self._write_to(self._path)

    def _on_save_as(self) -> None:
        CALIBRATIONS_DIR.mkdir(parents=True, exist_ok=True)
        default = str(CALIBRATIONS_DIR / f"{(self.name_edit.text().strip() or 'calibration')}.json")
        filename, _ = QFileDialog.getSaveFileName(
            self, "Save calibration configuration", default, "Calibration (*.json)"
        )
        if not filename:
            return
        if not filename.lower().endswith(".json"):
            filename += ".json"
        self._write_to(Path(filename))

    def _write_to(self, path: Path) -> None:
        config = self.current_config()
        blocking = [p for p in config.validate() if not p.startswith("Warning:")]
        if blocking:
            QMessageBox.warning(
                self, "Cannot save",
                "Fix these problems first:\n\n" + "\n".join(blocking),
            )
            return
        try:
            config.save(path)
        except OSError as exc:
            QMessageBox.critical(self, "Cannot save", f"{path}\n\n{exc}")
            return
        self._path = Path(path)
        self._mark_clean()

    def _on_ok(self) -> None:
        config = self.current_config()
        blocking = [p for p in config.validate() if not p.startswith("Warning:")]
        if blocking:
            QMessageBox.warning(
                self, "Cannot accept",
                "Fix these problems first:\n\n" + "\n".join(blocking),
            )
            return
        # Persist automatically when a path is known, so OK never loses work.
        if self._path is not None:
            self._write_to(self._path)
        self.accept()

    # ---------------------------------------------------------------- close
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()


__all__ = ["CalibrationWindow"]
