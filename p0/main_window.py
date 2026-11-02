"""The P0 main window.

Step 2 of the GUI: the layout. The window is split into three rectangles:

    ┌────────────────────────────┬───────────────────────────────┐
    │                            │  Calibration                  │
    │  Models                    │  (settings · run · status)    │
    │  (selector + table)        ├───────────────────────────────┤
    │                            │  Simulation                   │
    │                            │  (train · run · open view)    │
    └────────────────────────────┴───────────────────────────────┘
      Ring: —   P1 rate: —   Ps: —

The left column is 60% of the width and uses the full height. The right column
takes the remaining 40% and is split 40/60 between calibration (top) and
simulation (bottom). Both divisions are real ``QSplitter``s, so the ratios are
the initial geometry and the user can still drag them.

The widgets are still filled with dummy data; steps 3 and 4 wire them to the
real state. Every control is present and emits a signal, so the layout can be
judged — and clicked — before any process exists.

The simulation itself is *not* drawn here: ``Ps`` is its own process with its
own window, opened from the simulation pane.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from p0.widgets import ModelTable, StatusBar

#: Width split of the window: models on the left, everything else on the right.
MODELS_SIDE = 60
CONTROLS_SIDE = 40

#: Height split of the right-hand column: calibration on top, simulation below.
CALIBRATION_SIDE = 40
SIMULATION_SIDE = 60

#: Placeholder text for the calibration pane until a run is described.
CALIBRATION_SUMMARY_PLACEHOLDER = (
    "No recording planned yet.\n\n"
    "Open Configure calibration to choose the gestures, the number of "
    "repetitions per gesture and the image shown for each one."
)


class MainWindow(QMainWindow):
    """Top-level window of the orchestrator.

    ``QMainWindow`` (rather than plain ``QWidget``) is used because it already
    provides the pieces a real application needs: a menu bar, a status bar and
    a central widget area. We fill those in as we go.
    """

    def __init__(self) -> None:
        super().__init__()

        self.setWindowTitle("sEMG Drone — P0 Orchestrator")
        self.resize(1100, 700)

        self.status_bar = StatusBar(self)
        self.setStatusBar(self.status_bar)

        # --- widgets owned by the panes ---------------------------------
        self.model_table = ModelTable()
        self.model_selector = QComboBox()
        for name in self._model_names():
            self.model_selector.addItem(name)

        self.reload_button = QPushButton("Reload")
        self.calibrate_button = QPushButton("Start calibration")
        self.calibration_active = False
        self.calibrate_settings_button = QPushButton("Configure calibration…")
        self.train_button = QPushButton("Train")
        self.acquisition_button = QPushButton("Start acquisition")
        self.acquisition_active = False
        self.simulator_button = QPushButton("Start simulator")
        self.simulator_active = False
        self.open_simulation_button = QPushButton("Open simulation view")

        # --- window skeleton --------------------------------------------
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(0)
        outer.addWidget(self._build_layout())
        self.setCentralWidget(central)

        self.calibration_summary = QLabel(CALIBRATION_SUMMARY_PLACEHOLDER)
        self.calibration_summary.setWordWrap(True)

        self._connect_signals()

    # ------------------------------------------------------------------ build

    def _build_layout(self) -> QWidget:
        """The three rectangles that make up the window.

        Left: the models, full height. Right: split horizontally into the
        calibration area (top) and the simulation area (bottom). The splitter
        handles carry the 60/40 and 40/60 ratios; the user can drag them.
        """
        columns = QSplitter(Qt.Orientation.Horizontal)
        columns.addWidget(self._build_models_pane())

        rows = QSplitter(Qt.Orientation.Vertical)
        rows.addWidget(self._build_calibration_pane())
        rows.addWidget(self._build_simulation_pane())
        columns.addWidget(rows)

        # The ratio is enforced by the splitter itself: with a non-zero stretch
        # factor Qt shares leftover space by that weight, and the children's
        # size hints no longer decide the initial geometry.
        columns.setStretchFactor(0, MODELS_SIDE)
        columns.setStretchFactor(1, CONTROLS_SIDE)
        rows.setStretchFactor(0, CALIBRATION_SIDE)
        rows.setStretchFactor(1, SIMULATION_SIDE)

        columns.setChildrenCollapsible(False)
        rows.setChildrenCollapsible(False)

        self._columns = columns
        self._rows = rows
        return columns
        columns.setStretchFactor(1, CONTROLS_SIDE)
        rows.setStretchFactor(0, CALIBRATION_SIDE)
        rows.setStretchFactor(1, SIMULATION_SIDE)

        # Initial geometry, so the ratios hold on the first paint.
        columns.setSizes([MODELS_SIDE, CONTROLS_SIDE])
        rows.setSizes([CALIBRATION_SIDE, SIMULATION_SIDE])

        return columns

    def _make_pane(self, title: str) -> tuple[QGroupBox, QVBoxLayout]:
        """A titled box standing for one rectangle of the layout."""
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)
        return box, layout

    def _build_models_pane(self) -> QWidget:
        """Left pane (full height): which model feeds Ps, then the table."""
        pane, layout = self._make_pane("Models")
        pane.setMinimumWidth(240)

        header = QHBoxLayout()

        header = QHBoxLayout()
        header.addWidget(QLabel("Feeds the simulator:"))
        header.addWidget(self.model_selector, stretch=1)
        layout.addLayout(header)

        layout.addWidget(self.model_table, stretch=1)
        return pane

    def _build_calibration_pane(self) -> QWidget:
        """Top-right pane: run a recording, or open its settings window."""
        pane, layout = self._make_pane("Calibration")

        buttons = QHBoxLayout()
        buttons.addWidget(self.calibrate_button)
        buttons.addWidget(self.calibrate_settings_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.calibration_summary = QLabel(CALIBRATION_SUMMARY_PLACEHOLDER)
        self.calibration_summary.setWordWrap(True)
        layout.addWidget(self.calibration_summary)
        layout.addStretch(1)
        return pane

    def _build_simulation_pane(self) -> QWidget:
        """Bottom-right pane: train, then run / reload / stop the simulator."""
        pane, layout = self._make_pane("Simulation")

        sim_row = QHBoxLayout()
        sim_row.addWidget(self.simulator_button)
        sim_row.addWidget(self.reload_button)
        sim_row.addWidget(self.open_simulation_button)
        sim_row.addStretch(1)
        layout.addLayout(sim_row)

        acq_row = QHBoxLayout()
        acq_row.addWidget(self.acquisition_button)
        acq_row.addWidget(self.train_button)
        acq_row.addStretch(1)
        layout.addLayout(acq_row)

        layout.addStretch(1)
        return pane

    def _model_names(self) -> list[str]:
        """Model names as they appear in the table (dummy data until step 3)."""
        names = []
        for row in range(self.model_table.rowCount()):
            item = self.model_table.item(row, 0)
            if item is not None:
                names.append(item.text())
        return names

    # ----------------------------------------------------------------- wiring

    def _connect_signals(self) -> None:
        # Buttons only flip their own label for now — the controller (step 4)
        # will connect to these signals and do the real work.
        self.calibrate_button.clicked.connect(self._toggle_calibration)
        self.acquisition_button.clicked.connect(self._toggle_acquisition)
        self.simulator_button.clicked.connect(self._toggle_simulator)

    def _toggle_calibration(self) -> None:
        self.calibration_active = not self.calibration_active
        self.calibrate_button.setText(
            "Stop calibration" if self.calibration_active else "Start calibration"
        )

    def _toggle_acquisition(self) -> None:
        self.acquisition_active = not self.acquisition_active
        self.acquisition_button.setText(
            "Stop acquisition" if self.acquisition_active else "Start acquisition"
        )

    def _toggle_simulator(self) -> None:
        self.simulator_active = not self.simulator_active
        self.simulator_button.setText(
            "Stop simulator" if self.simulator_active else "Start simulator"
        )

    # --------------------------------------------------------------- helpers

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's naming
        """Re-apply the 60/40 and 40/60 ratios whenever the window resizes.

        ``setSizes`` is only a hint: Qt recomputes the split from the children's
        size hints on every re-layout, which quickly drifts away from the
        intended proportions. Re-applying the ratios on resize keeps the
        geometry stable; the user can still drag the handles in between.
        """
        super().resizeEvent(event)
        if not hasattr(self, "_columns"):
            return
        self._apply_ratios()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt's naming
        super().showEvent(event)
        self._apply_ratios()

    def _apply_ratios(self) -> None:
        self._columns.setSizes(
            [
                self._columns.width() * MODELS_SIDE // (MODELS_SIDE + CONTROLS_SIDE),
                self._columns.width() * CONTROLS_SIDE // (MODELS_SIDE + CONTROLS_SIDE),
            ]
        )
        self._rows.setSizes(
            [
                self._rows.height()
                * CALIBRATION_SIDE
                // (CALIBRATION_SIDE + SIMULATION_SIDE),
                self._rows.height()
                * SIMULATION_SIDE
                // (CALIBRATION_SIDE + SIMULATION_SIDE),
            ]
        )

    def selected_model(self) -> str | None:
        """Name of the model currently chosen to feed the simulator."""
        return self.model_selector.currentText() or None

    def select_model(self, name: str) -> None:
        """Select ``name`` in the combo box (no-op if it is not listed)."""
        index = self.model_selector.findText(name)
        if index >= 0:
            self.model_selector.setCurrentIndex(index)
