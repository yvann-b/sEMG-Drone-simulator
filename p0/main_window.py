"""The P0 main window.

Step 2 of the GUI: the real layout. The placeholder label is gone and the window
now shows the four areas from ``docs/ARCHITECTURE.md`` §7:

    ┌──────────────────────────────────────────────────────────┐
    │  Models feeding Ps: ( ) svm  ( ) lda  ( ) cnn            │
    ├──────────────────────────────────────────────────────────┤
    │  Model  │ Status  │ Last label │ Confidence │ Age        │
    │  svm    │ stopped │     —      │     —      │  —         │
    │  ...                                                      │
    ├──────────────────────────────────────────────────────────┤
    │  [Reload] [Calibrate] [Train] │ [Start acquisition] [Ps] │
    └──────────────────────────────────────────────────────────┘
      Ring: —   P1 rate: —   Ps: —

The widgets are still filled with dummy data; steps 3 and 4 wire them to the
real state. Every control is present and emits a signal, so the layout can be
judged — and clicked — before any process exists.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from p0.widgets import ModelTable, StatusBar


class MainWindow(QMainWindow):
    """Top-level window of the orchestrator.

    ``QMainWindow`` (rather than plain ``QWidget``) is used because it already
    provides the pieces a real application needs: a menu bar, a status bar and
    a central widget area. We fill those in as we go.
    """

    def __init__(self) -> None:
        super().__init__()

        self.setWindowTitle("sEMG Drone — P0 Orchestrator")
        self.resize(900, 600)

        self.status_bar = StatusBar(self)
        self.setStatusBar(self.status_bar)

        self.model_table = ModelTable()
        self._model_buttons = QButtonGroup(self)
        self._model_buttons.setExclusive(True)

        self.reload_button = QPushButton("Reload")
        self.calibrate_button = QPushButton("Start calibration")
        self.calibration_active = False
        self.train_button = QPushButton("Train")
        self.acquisition_button = QPushButton("Start acquisition")
        self.acquisition_active = False
        self.simulator_button = QPushButton("Start simulator")
        self.simulator_active = False

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self._build_selector_area())
        layout.addWidget(self.model_table, stretch=1)
        layout.addWidget(self._build_control_area())
        self.setCentralWidget(central)

        self._connect_signals()

    # ------------------------------------------------------------------ build

    def _build_selector_area(self) -> QWidget:
        """Row of radio buttons choosing which model feeds the simulator."""
        box = QGroupBox("Models")
        box_layout = QHBoxLayout(box)
        box_layout.setContentsMargins(12, 8, 12, 8)
        box_layout.setSpacing(16)
        box_layout.addWidget(QLabel("Feeds the simulator:"))

        for name in self._model_names():
            radio = QRadioButton(name)
            radio.setProperty("model", name)
            self._model_buttons.addButton(radio)
            box_layout.addWidget(radio)
        box_layout.addStretch(1)

        # Something must always feed the simulator: preselect the first model.
        buttons = self._model_buttons.buttons()
        if buttons:
            buttons[0].setChecked(True)

        return box

    def _build_control_area(self) -> QWidget:
        """Row of buttons: model actions on the left, process actions on the right."""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.reload_button)
        layout.addWidget(self.calibrate_button)
        layout.addWidget(self.train_button)
        layout.addStretch(1)
        layout.addWidget(self.acquisition_button)
        layout.addWidget(self.simulator_button)
        return container

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

    def selected_model(self) -> str | None:
        """Name of the model currently chosen to feed the simulator."""
        button = self._model_buttons.checkedButton()
        return None if button is None else button.text()

    def select_model(self, name: str) -> None:
        """Check the radio button for ``name`` (no-op if it does not exist)."""
        for button in self._model_buttons.buttons():
            if button.text() == name:
                button.setChecked(True)
                return