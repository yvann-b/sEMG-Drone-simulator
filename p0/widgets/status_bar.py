"""The status bar: health of the data plane, shown as permanent widgets.

A plain ``QStatusBar.showMessage`` gets overwritten by the next message, so the
live values (ring fill, P1 rate, Ps state) are added as *permanent* widgets on
the right-hand side, next to a transient message on the left.
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QStatusBar


class StatusBar(QStatusBar):
    """Status bar with a message area and three permanent indicators.

    ``ring``, ``rate`` and ``ps`` are plain ``QLabel``s; the controller updates
    them through ``set_ring`` / ``set_rate`` / ``set_ps``. Until it runs, they
    show an em dash so it is obvious the values are not real.
    """

    PLACEHOLDER = "\u2014"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self.showMessage("Ready")

        self.ring = self._make_indicator("Ring", self.PLACEHOLDER)
        self.rate = self._make_indicator("P1 rate", self.PLACEHOLDER)
        self.ps = self._make_indicator("Ps", self.PLACEHOLDER)

        for indicator in (self.ring, self.rate, self.ps):
            self.addPermanentWidget(indicator)

    def _make_indicator(self, prefix: str, value: str) -> QLabel:
        label = QLabel(f"{prefix}: {value}")
        label.setContentsMargins(8, 0, 8, 0)
        return label

    def set_ring(self, fill_percent: float | None) -> None:
        """Show ring fill as a percentage; ``None`` means “not available”."""
        self.ring.setText(
            f"Ring: {self.PLACEHOLDER if fill_percent is None else f'{fill_percent:.0f}%'}"
        )

    def set_rate(self, samples_per_second: float | None) -> None:
        """Show the measured P1 sample rate."""
        self.rate.setText(
            f"P1 rate: {self.PLACEHOLDER if samples_per_second is None else f'{samples_per_second:.0f} Hz'}"
        )

    def set_ps(self, state: str | None) -> None:
        """Show whether the simulator process is connected."""
        self.ps.setText(f"Ps: {self.PLACEHOLDER if not state else state}")