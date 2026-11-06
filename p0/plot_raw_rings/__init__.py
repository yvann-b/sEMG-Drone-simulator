"""Live plotting of the raw ring contents, for eyeballing the data plane.

This module is a **debugging aid**: it renders the newest samples sitting in
the shared-memory ``EmgAccelRing`` / ``ImuRing`` pairs, so you can see whether
a Shimmer is actually streaming before any model exists.

Why it lives in the GUI process (and not in a spawned child)
------------------------------------------------------------
The rings are plain shared-memory segments: *any* process can attach to them.
But the GUI already owns a Qt event loop, so the cheapest correct design is a
``QTimer`` in that loop that polls the rings FIFO-style every few tens of
milliseconds and redraws. Spawning a second process just to plot would mean a
second top-level window with its own event loop, fragile to close and awkward
to keep in sync with shutdown -- none of which buys us anything here.

Ownership rule preserved
------------------------
The plotter only ever *reads* and calls ``close()`` (detach) on close. It never
calls ``unlink()``: the ``DeviceBackend`` remains the sole owner that creates
and unlinks the segments (see :mod:`p0.backend`).
"""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget

try:  # matplotlib is optional; the rest of P0 must import without it
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
    from matplotlib.figure import Figure

    _MATPLOTLIB_AVAILABLE = True
except Exception:  # noqa: BLE001 -- any import failure just disables plotting
    FigureCanvasQTAgg = None  # type: ignore[assignment]
    Figure = None  # type: ignore[assignment]
    _MATPLOTLIB_AVAILABLE = False


#: How often the plotter polls the rings and redraws (ms). 40 ms ~= 25 Hz:
#: smooth enough for a debug view, light enough to never starve the data plane.
PLOT_TICK_MS = 40

#: How many samples each rolling line keeps. At 512 Hz this is ~4 s of history.
PLOT_WINDOW = 2048


class RingPlotWindow(QMainWindow):
    """A window with two stacked panels: EMG and IMU accel.

    It is fed by the *live* ring objects P0 already owns; because this window
    runs in P0's own process, it reads them directly -- no pickling, no second
    shared-memory attach. Each tick it drains only the samples written since
    the previous tick (``read_next_fifo`` with a private cursor) and appends
    them to bounded deques, so the cost per tick is proportional to the number
    of *new* samples, not the whole buffer.
    """

    def __init__(self, emg_rings, imu_rings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Raw rings - live")
        self.resize(760, 560)

        # P0 owns these live ring handles (same process); the plotter only reads.
        self._emg_rings = list(emg_rings or [])
        self._imu_rings = list(imu_rings or [])

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        if not _MATPLOTLIB_AVAILABLE:
            from PySide6.QtWidgets import QLabel

            label = QLabel(
                "matplotlib is not installed, so the raw-ring plot is disabled.\n"
                "Install it (e.g. `uv add matplotlib`) to enable this view."
            )
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(label)
            self._timer = None
            return

        self._figure = Figure(figsize=(7, 5), tight_layout=True)
        self._canvas = FigureCanvasQTAgg(self._figure)
        layout.addWidget(self._canvas)

        self._emg_ax = self._figure.add_subplot(2, 1, 1)
        self._emg_ax.set_ylabel("EMG")
        self._emg_ax.set_title("EMG (raw)")
        self._acc_ax = self._figure.add_subplot(2, 1, 2)
        self._acc_ax.set_ylabel("Accel (g)")
        self._acc_ax.set_xlabel("sample")

        self._emg_lines = [
            self._emg_ax.plot([], [], label=f"emg{i}")[0] for i in range(2)
        ]
        self._emg_ax.legend(loc="upper right", fontsize="small")
        self._acc_lines = {
            axis: self._acc_ax.plot([], [], label=f"acc_{axis}")[0]
            for axis in ("x", "y", "z")
        }
        self._acc_ax.legend(loc="upper right", fontsize="small")

        # Rolling sample buffers (one EMG channel pair + 3 accel axes).
        self._emg0: deque = deque(maxlen=PLOT_WINDOW)
        self._emg1: deque = deque(maxlen=PLOT_WINDOW)
        self._acc: dict[str, deque] = {
            axis: deque(maxlen=PLOT_WINDOW) for axis in ("x", "y", "z")
        }

        # Private FIFO cursors so we only pull what is new since the last tick.
        self._emg_cursor = "plot-emg"
        self._imu_cursor = "plot-imu"

        self._timer = QTimer(self)
        self._timer.setInterval(PLOT_TICK_MS)
        self._timer.timeout.connect(self._on_tick)

    # ------------------------------------------------------------ lifecycle
    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        if self._timer is not None and not self._timer.isActive():
            self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().hideEvent(event)
        # Stop polling while hidden: no point burning CPU on an unseen window.
        if self._timer is not None and self._timer.isActive():
            self._timer.stop()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._timer is not None:
            self._timer.stop()
        super().closeEvent(event)

    # ----------------------------------------------------------------- tick
    def _on_tick(self) -> None:
        if not _MATPLOTLIB_AVAILABLE or not self._emg_rings:
            return

        # Drain every new EMG sample since the previous tick.
        emg_ring = self._emg_rings[0]
        while True:
            res = emg_ring.read_next_fifo(self._emg_cursor)
            if res is None:
                break
            rec, _last_seen, _gap = res  # gap>0: fell behind; fine for a debug view
            self._emg0.append(float(rec["emg"][0]))
            self._emg1.append(float(rec["emg"][1]))

        # Drain every new IMU sample (accel only here; gyro/mag not shown).
        if self._imu_rings:
            imu_ring = self._imu_rings[0]
            while True:
                res = imu_ring.read_next_fifo(self._imu_cursor)
                if res is None:
                    break
                rec, _last_seen, _gap = res
                for i, axis in enumerate(("x", "y", "z")):
                    self._acc[axis].append(float(rec["accel"][i]))

        if self._emg0:
            n_emg = len(self._emg0)
            self._emg_lines[0].set_data(range(n_emg), self._emg0)
            self._emg_lines[1].set_data(range(len(self._emg1)), self._emg1)
            self._emg_ax.relim()
            self._emg_ax.autoscale_view()

        n_acc = len(self._acc["x"])
        if n_acc:
            for axis, line in self._acc_lines.items():
                line.set_data(range(n_acc), self._acc[axis])
            self._acc_ax.relim()
            self._acc_ax.autoscale_view()

        self._canvas.draw_idle()


__all__ = ["RingPlotWindow", "PLOT_TICK_MS", "PLOT_WINDOW"]
