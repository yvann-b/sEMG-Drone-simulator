"""The P0 main window.

The window is split into three rectangles:

    EMG sensor: [Start acquisition]
    ┌────────────────────────────┬───────────────────────────────┐
    │                            │  Calibration                  │
    │  Models                    │  (run · settings · summary)   │
    │  (selector + table)        ├───────────────────────────────┤
    │                            │  Simulation                   │
    │                            │  (run · reload · open view)   │
    └────────────────────────────┴───────────────────────────────┘
      Sensor 1: online   Ps: —
The left column is ~60% of the width and uses the full height. The right column
takes the remainder and is split between calibration (top) and simulation
(bottom). The ratios are a *convenience*, not a constraint: both divisions are
real ``QSplitter``s, so the user can drag them.

**The window is dumb.** It holds no truth about the processes. Everything
visible comes from a snapshot pushed by the controller:

    controller ──snapshot_ready──▶ MainWindow.apply_snapshot()
                                          │
                                          ├─▶ ModelTableModel.apply_snapshot
                                          └─▶ StatusBar.set_sensors / set_ps

Going the other way, the window only *posts commands*: a button click emits a
signal, the controller picks it up on the next tick. The window never waits for
a backend and the backend never waits for the window.

The simulation itself is *not* drawn here: ``Ps`` is its own process with its
own window, opened from the simulation pane.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
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

from p0.state import ModelRow, SystemSnapshot
from p0.widgets import ModelTableModel, ModelView, StatusBar

#: Width split of the window: models on the left, everything else on the right. (%)
MODELS_SIDE = 60
CONTROLS_SIDE = 40

#: Height split of the right-hand column: calibration on top, simulation below. (%)
CALIBRATION_SIDE = 40
SIMULATION_SIDE = 60


def _split_by_ratio(total: int, first: int, second: int) -> list[int]:
    """Split ``total`` into two integers following ``first:second``.

    The last pane absorbs the rounding remainder so the two values always add
    up to ``total`` exactly; otherwise a one-pixel gap shows up as a jump when
    the window is resized.
    """
    share = first + second
    first_size = total * first // share
    return [first_size, total - first_size]


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
    a central widget area.
    """

    #: A toggle / action was requested. ``action`` is the command vocabulary
    #: the controller understands; ``active`` is the desired new state.
    command_requested = Signal(str, bool)
    #: The user picked a different model to feed the simulator.
    model_selected = Signal(str)

    def __init__(self) -> None:
        super().__init__()

        self.setWindowTitle("sEMG Drone — P0 Orchestrator")
        self.resize(1100, 700)

        self.status_bar = StatusBar(self)
        self.setStatusBar(self.status_bar)

        # --- data model + view ------------------------------------------
        # The model is the single place the rows live. Placeholder rows come
        # from the model itself, so the window looks the same until the
        # controller's first snapshot arrives.
        self.models = ModelTableModel(self)
        self.model_view = ModelView()
        self.model_view.setModel(self.models)

        self.model_selector = QComboBox()
        self._sync_selector(self.models.row_names())

        self.reload_button = QPushButton("Reload")
        self.calibrate_button = QPushButton("Start calibration")
        self.calibration_active = False
        self.calibrate_settings_button = QPushButton("Configure calibration…")
        self.acquisition_button = QPushButton("Start acquisition")
        self.acquisition_active = False
        self.acquisition_button.setToolTip(
            "Start / stop reading the sensor. Independent of everything else: "
            "nothing needs the data, and Ps is simply switched off when it stops."
        )
        self.simulator_button = QPushButton("Start simulator")
        self.simulator_active = False
        self.open_simulation_button = QPushButton("Open simulation view")

        # --- window skeleton --------------------------------------------
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(8)
        outer.addWidget(self._build_acquisition_bar())
        outer.addWidget(self._build_layout(), stretch=1)
        self.setCentralWidget(central)

        self._connect_signals()

    # ------------------------------------------------------------------ build

    def _build_acquisition_bar(self) -> QWidget:
        """The one control that belongs to no rectangle: read the sensor or not.

        Acquisition is the root of the whole data plane — the rings exist as
        soon as it runs, and every model reads from them. It is therefore shown
        above the three panes rather than inside one of them, because it is not
        part of any single workflow.
        """
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(8)
        layout.addWidget(QLabel("EMG sensor:"))
        layout.addWidget(self.acquisition_button)
        layout.addStretch(1)
        return bar

    def _build_layout(self) -> QWidget:
        """The three rectangles that make up the window.

        Left: the models, full height. Right: split horizontally into the
        calibration area (top) and the simulation area (bottom). The splitter
        handles carry the initial ratios; the user can drag them.
        """
        columns = QSplitter(Qt.Orientation.Horizontal)
        columns.addWidget(self._build_models_pane())

        rows = QSplitter(Qt.Orientation.Vertical)
        rows.addWidget(self._build_calibration_pane())
        rows.addWidget(self._build_simulation_pane())
        columns.addWidget(rows)

        # The ratio is seeded by the splitter: with a non-zero stretch factor
        # Qt shares leftover space by that weight, and the children's size
        # hints no longer decide the initial geometry.
        columns.setStretchFactor(0, MODELS_SIDE)
        columns.setStretchFactor(1, CONTROLS_SIDE)
        rows.setStretchFactor(0, CALIBRATION_SIDE)
        rows.setStretchFactor(1, SIMULATION_SIDE)

        columns.setChildrenCollapsible(False)
        rows.setChildrenCollapsible(False)

        self._columns = columns
        self._rows = rows
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
        header.addWidget(QLabel("Feeds the simulator:"))
        header.addWidget(self.model_selector, stretch=1)
        layout.addLayout(header)

        layout.addWidget(self.model_view, stretch=1)
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
        """Bottom-right pane: run / reload / stop the simulator, and open its view.

        There is no *Train* button: a model trains itself on spawn when it was
        given a dataset, so training is part of starting the model, not a
        separate action.
        """
        pane, layout = self._make_pane("Simulation")

        sim_row = QHBoxLayout()
        sim_row.addWidget(self.simulator_button)
        sim_row.addWidget(self.reload_button)
        sim_row.addWidget(self.open_simulation_button)
        sim_row.addStretch(1)
        layout.addLayout(sim_row)

        layout.addStretch(1)
        return pane

    # ----------------------------------------------------------------- wiring

    def _connect_signals(self) -> None:
        # Buttons flip their own label for instant feedback and *post a
        # command*; the controller does the real work on its next tick.
        self.calibrate_button.clicked.connect(self._toggle_calibration)
        self.acquisition_button.clicked.connect(self._toggle_acquisition)
        self.simulator_button.clicked.connect(self._toggle_simulator)
        self.reload_button.clicked.connect(self._request_reload)
        self.calibrate_settings_button.clicked.connect(self._request_calibration_settings)
        self.open_simulation_button.clicked.connect(self._request_open_simulation)
        self.model_selector.currentTextChanged.connect(self._on_model_selected)

    def _toggle_calibration(self) -> None:
        self.calibration_active = not self.calibration_active
        self.calibrate_button.setText(
            "Stop calibration" if self.calibration_active else "Start calibration"
        )
        self.command_requested.emit("calibrate", self.calibration_active)

    def _toggle_acquisition(self) -> None:
        self.acquisition_active = not self.acquisition_active
        self.acquisition_button.setText(
            "Stop acquisition" if self.acquisition_active else "Start acquisition"
        )
        self.command_requested.emit("acquire", self.acquisition_active)

    def _toggle_simulator(self) -> None:
        self.simulator_active = not self.simulator_active
        self.simulator_button.setText(
            "Stop simulator" if self.simulator_active else "Start simulator"
        )
        self.command_requested.emit("simulate", self.simulator_active)

    def _request_reload(self) -> None:
        self.command_requested.emit("reload", True)

    def _request_calibration_settings(self) -> None:
        # A settings dialog is a later step; the signal already exists so the
        # controller can be wired to it without touching the window again.
        self.command_requested.emit("calibration_settings", True)

    def _request_open_simulation(self) -> None:
        self.command_requested.emit("open_view", True)

    def _on_model_selected(self, name: str) -> None:
        if name:
            self.model_selected.emit(name)

    # ------------------------------------------------------- controller in

    def apply_snapshot(self, snapshot: SystemSnapshot) -> None:
        """Render one tick of truth. The *only* way the table changes."""
        self.models.apply_snapshot(snapshot.models, now=snapshot.now)
        self._sync_selector(self.models.row_names())
        self.status_bar.set_sensors(snapshot.p1_states)
        self.status_bar.set_ps(snapshot.ps_state)

    def _sync_selector(self, names: list[str]) -> None:
        """Keep the combo box in step with the rows, preserving the selection.

        The signal is blocked while rebuilding so that repopulating the list
        does not look like the user picking a different model — that would
        bounce a spurious ``model_selected`` back at the controller every time
        a row is added.
        """
        current = self.model_selector.currentText()
        if names == [self.model_selector.itemText(i) for i in range(self.model_selector.count())]:
            return
        blocked = self.model_selector.blockSignals(True)
        try:
            self.model_selector.clear()
            self.model_selector.addItems(names)
            if current:
                index = self.model_selector.findText(current)
                if index >= 0:
                    self.model_selector.setCurrentIndex(index)
        finally:
            self.model_selector.blockSignals(blocked)

    # --------------------------------------------------------------- helpers

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's naming
        """Re-apply the window's division ratios when it resizes.

        ``setSizes`` is only a hint: Qt recomputes the split from the children's
        size hints on every re-layout. Re-applying the ratios keeps the initial
        look stable; the user can still drag the handles in between.
        """
        super().resizeEvent(event)
        if not hasattr(self, "_columns"):
            return
        self._apply_ratios()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt's naming
        super().showEvent(event)
        self._apply_ratios()

    def _apply_ratios(self) -> None:
        """Re-impose the target ratios after Qt recomputed the sizes.

        The ratios are computed from the *sum of the current sizes* rather
        than from ``splitter.width()``. ``width()`` includes the splitter
        handles, so sizing from it makes Qt shrink the panes to fit the
        handles and the ratios drift. The sizes sum to the space actually
        shared between the panes, handles excluded.
        """
        self._columns.setSizes(
            _split_by_ratio(sum(self._columns.sizes()), MODELS_SIDE, CONTROLS_SIDE)
        )
        self._rows.setSizes(
            _split_by_ratio(sum(self._rows.sizes()), CALIBRATION_SIDE, SIMULATION_SIDE)
        )

    def selected_model(self) -> str | None:
        """Name of the model currently chosen to feed the simulator."""
        return self.model_selector.currentText() or None

    def select_model(self, name: str) -> None:
        """Select ``name`` in the combo box (no-op if it is not listed)."""
        index = self.model_selector.findText(name)
        if index >= 0:
            self.model_selector.setCurrentIndex(index)


__all__ = ["MainWindow", "ModelRow"]
