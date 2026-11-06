"""The status bar: health of the data plane, shown as permanent widgets.

A plain ``QStatusBar.showMessage`` gets overwritten by the next message, so the
live values are added as *permanent* widgets on the right-hand side, next to a
transient message on the left.

What is shown, and why
----------------------
* One **``Sensor <i>:``** chip per Shimmer, coloured by health
  (offline / connecting / online / error). The number of chips and their states
  both come from ``SystemSnapshot.p1_states`` -- the window never needs to know
  ``NUMBER_SHIMMER``. One dead Shimmer is therefore visible next to a healthy
  one.
* **``Ps:``** the simulator state.

Ring fill and P1 sample rate were dropped on purpose: they are internal buffer
plumbing, not something an operator acts on.
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QStatusBar

#: Colour per sensor health state. ``None`` falls back to the default label
#: colour (used for "offline" so it reads as neutral, not as an alarm).
SENSOR_COLORS = {
    "online": "#1a7f37",      # green
    "connecting": "#9a6700",  # amber
    "error": "#b42318",       # red
}


class StatusBar(QStatusBar):
    """Status bar with a transient message area and live indicators.

    The sensor chips are rebuilt by :meth:`set_sensors` whenever the number of
    Shimmers in the snapshot changes, so the same widget serves 1, 2 or N
    sensors. ``ps`` is a single permanent chip.
    """

    PLACEHOLDER = "\u2014"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self.showMessage("Ready")

        #: One coloured "Sensor <i>:" label per Shimmer, rebuilt as needed.
        self.sensor_labels: list[QLabel] = []
        #: The state currently painted on each chip (used to skip repaints).
        self._sensor_states: list[str] = []

        self.ps = self._make_indicator("Ps", self.PLACEHOLDER)
        self.addPermanentWidget(self.ps)

    def _make_indicator(self, prefix: str, value: str) -> QLabel:
        label = QLabel(f"{prefix}: {value}")
        label.setContentsMargins(8, 0, 8, 0)
        return label

    # -------------------------------------------------------------- sensors

    def set_sensors(self, states: tuple[str, ...]) -> None:
        """Show one ``Sensor <i>:`` chip per state, coloured by health.

        Called every tick. Rebuilds the chip list only when the *count* of
        sensors changes; otherwise it just updates text and colour, so it is
        cheap to call at 20 Hz.
        """
        if len(states) != len(self.sensor_labels):
            self._rebuild_sensor_labels(len(states))

        for index, (label, state) in enumerate(zip(self.sensor_labels, states)):
            state = state or "offline"
            label.setText(f"Sensor {index + 1}: {state}")
            colour = SENSOR_COLORS.get(state)
            label.setStyleSheet(f"color: {colour};" if colour else "")

        self._sensor_states = [s or "offline" for s in states]

    def _rebuild_sensor_labels(self, count: int) -> None:
        """Recreate the sensor chips so exactly ``count`` of them exist.

        New chips are inserted *before* ``ps`` so the order reads left-to-right
        as ``Sensor 1, Sensor 2, ..., Ps``.
        """
        for label in self.sensor_labels:
            self.removeWidget(label)
            label.deleteLater()
        self.sensor_labels = []

        for index in range(count):
            label = self._make_indicator(f"Sensor {index + 1}", "offline")
            # ``addPermanentWidget`` appends on the right; since we rebuild all
            # chips before re-adding ``ps`` we must re-order. Simplest: remove
            # ps, add sensors, re-add ps.
            self.sensor_labels.append(label)

        # Re-add ps last so it always sits on the far right, after the chips.
        self.removeWidget(self.ps)
        for label in self.sensor_labels:
            self.addPermanentWidget(label)
        self.addPermanentWidget(self.ps)

    # ------------------------------------------------------------ simulator

    def set_ps(self, state: str | None) -> None:
        """Show whether the simulator process is connected."""
        self.ps.setText(f"Ps: {self.PLACEHOLDER if not state else state}")
