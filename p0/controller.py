"""The P0 control loop.

One ``QTimer`` tick owns all the work that must happen regularly:

    1. drain the GUI -> controller command queue   (get_nowait, never blocks)
    2. drain every Pm_i queue                      (get_nowait, never blocks)
    3. forward the selected model's label to Ps, with a deadline
    4. check deadlines (an expired command means Ps falls back to "rest")
    5. build one immutable SystemSnapshot and hand it to the GUI

Two rules make this safe:

* **No blocking calls inside the tick.** Every queue read uses ``get_nowait``;
  a missing message is a normal state, not an error.
* **The GUI never calls back into the backend.** It posts a command and
  renders whatever snapshot arrives. The tick and the UI never wait on each
  other.

This step ships a ``FakeBackend``: a controller that runs with **no processes
at all**, so the loop can be exercised (and tested) before P1/Pc/Pm/Ps exist.
``Controller.backend`` is the only seam the real spawner will replace.
"""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Signal

from p0.state import ModelRow, SystemSnapshot

#: How often the loop runs. 50 ms = 20 Hz: faster than any model's emit rate,
#: slow enough to be invisible on the CPU. Hardcoded on purpose -- no config.
TICK_INTERVAL_MS = 50

#: How long a forwarded command stays valid before Ps falls back to "rest".
#: P0 stamps the deadline; Ps only compares it to ``now``.
COMMAND_TIMEOUT_S = 0.5


@dataclass(frozen=True)
class Command:
    """A request posted by the GUI and handled on the next tick.

    ``action`` is a small string vocabulary (``"acquire"``, ``"calibrate"``,
    ``"simulate"``, ``"reload"``, ``"select"``); ``model`` is an optional
    payload, e.g. the name chosen in the selector.
    """

    action: str
    model: str | None = None


class _ModelState:
    """Mutable per-model bookkeeping inside the controller only."""

    __slots__ = ("row", "seq")

    def __init__(self, name: str) -> None:
        self.row = ModelRow(name=name, status="idle")
        self.seq: int | None = None


class FakeBackend:
    """Stand-in for the real processes, so step 4 runs with nothing spawned.

    It knows the models by name, fakes a "selected" model emitting a label,
    and lets the tests advance the clock deterministically.
    """

    def __init__(self, model_names: list[str]) -> None:
        self._models = {name: _ModelState(name) for name in model_names}
        self._selected: str | None = none_or_first(model_names)
        self._pending_ps: list[dict] = []

    def names(self) -> list[str]:
        return list(self._models)

    def drain_model_queues(self, now: float) -> list[ModelRow]:
        """Return current rows (no queues exist yet -> nothing changes)."""
        return [state.row for state in self._models.values()]

    def set_status(self, name: str, status: str) -> None:
        state = self._models.get(name)
        if state is not None:
            state.row = ModelRow(
                name=state.row.name,
                status=status,  # type: ignore[arg-type]
                last_label=state.row.last_label,
                last_confidence=state.row.last_confidence,
                last_seen=state.row.last_seen,
            )

    def select(self, name: str | None) -> None:
        self._selected = name

    def forward(self, label: str, deadline: float) -> None:
        self._pending_ps.append({"label": label, "deadline": deadline, "model": self._selected})

    def drain_ps(self) -> list[dict]:
        out, self._pending_ps = self._pending_ps, []
        return out

    @property
    def selected(self) -> str | None:
        return self._selected


def none_or_first(names: list[str]) -> str | None:
    return names[0] if names else None


class Controller(QObject):
    """Owns the tick. Builds snapshots. Never blocks."""

    #: A whole tick's worth of truth, ready to render.
    snapshot_ready = Signal(object)
    #: Something went wrong (bad command, backend error). Free text for now.
    error = Signal(str)

    def __init__(self, backend, model_names: list[str] | None = None, parent=None) -> None:
        super().__init__(parent)
        self._backend = backend
        self._commands: queue.Queue[Command] = queue.Queue()
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_INTERVAL_MS)
        self._timer.timeout.connect(self._on_tick)
        self._selected: str | None = None
        self._started = False

    @property
    def timer(self) -> QTimer:
        return self._timer

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._timer.start()

    def stop(self) -> None:
        self._started = False
        self._timer.stop()

    def close_backend(self) -> None:
        """Release the backend's resources (stop P1, unlink the rings).

        Called on application shutdown *after* :meth:`stop`, so no tick races
        the teardown. A backend without ``close`` (e.g. ``FakeBackend``) is a
        no-op.
        """
        close = getattr(self._backend, "close", None)
        if callable(close):
            close()

    # ------------------------------------------------------------------ GUI
    def submit(self, command: Command) -> None:
        """Queue a GUI command. Handled on the next tick, never right now."""
        self._commands.put(command)

    def submit_command(self, action: str, active: bool) -> None:
        """Slot for ``MainWindow.command_requested(str, bool)``.

        The window speaks ``(action, active_bool)``; the acquisition handler
        speaks ``Command(action, model="start"|"stop")``. This adapter bridges
        the two so the window never needs the command vocabulary details.
        Other actions ignore ``active`` for now.
        """
        if action == "acquire":
            self.submit(Command(action="acquire", model="start" if active else "stop"))
        else:
            self.submit(Command(action=action))

    def select_model(self, name: str | None) -> None:
        """Choose which model's output is forwarded to Ps.

        The selection is pushed to the backend too, so the loop and the backend
        never disagree about which model is being watched.
        """
        self._selected = name
        select = getattr(self._backend, "select", None)
        if callable(select):
            select(name)

    # ----------------------------------------------------------------- tick
    def _on_tick(self) -> None:
        try:
            self._drain_commands()
            self._tick()
        except Exception as exc:  # noqa: BLE001 -- the loop must survive
            self.error.emit(str(exc))

    def _drain_commands(self) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            self._handle(command)

    def _handle(self, command: Command) -> None:
        if command.action in ("select", "model"):
            # Go through select_model so the backend is told as well; setting
            # _selected directly would leave the two out of step.
            self.select_model(command.model)
            return
        if command.action == "acquire":
            self._handle_acquisition(command)
            return
        # The rest are lifecycle actions; they are wired up in the process
        # steps. For now they must not crash the tick.
        if command.action in ("calibrate", "simulate", "reload"):
            return
        self.error.emit(f"Unknown command: {command.action!r}")

    def _handle_acquisition(self, command: Command) -> None:
        """Start / stop P1 through the backend.

        A ``Command`` carries a ``model`` string, not a bool, so the desired
        state is encoded as ``"start"`` / ``"stop"``. A backend without the
        acquisition surface (e.g. ``FakeBackend`` in the GUI tests) is a no-op.
        """
        start = getattr(self._backend, "start_acquisition", None)
        stop = getattr(self._backend, "stop_acquisition", None)
        if start is None or stop is None:
            return
        if command.model == "stop":
            stop()
        else:
            start()

    def _tick(self) -> None:
        now = time.monotonic()
        rows = self._backend.drain_model_queues(now)

        # Forward the selected model's most recent label with a deadline.
        selected_row = next((r for r in rows if r.name == self._selected), None)
        if selected_row is not None and selected_row.last_label:
            self._backend.forward(selected_row.last_label, now + COMMAND_TIMEOUT_S)

        # Sensor health + child errors. A reported error both flips the
        # indicator to "error" and is emitted on the ``error`` signal, so a
        # Bluetooth failure reaches the GUI, not just the console.
        p1_states = self._poll_sensor_health()

        snapshot = SystemSnapshot(
            models=tuple(rows),
            p1_rate_hz=None,
            p1_states=p1_states,
            ps_state=self._backend.selected,
            now=now,
        )
        self.snapshot_ready.emit(snapshot)

    def _poll_sensor_health(self) -> tuple[str, ...]:
        """Per-Shimmer states for this tick, and forward any new child errors.

        Returns a tuple (one entry per sensor) for ``SystemSnapshot.p1_states``.
        ``FakeBackend`` has neither ``p1_states`` nor ``drain_events``, so this
        returns ``()`` there and the status bar shows no sensor chip.

        Order matters: drain *first* to capture the new error strings (the pipe
        is one-shot), emit them, then read ``p1_states`` (whose own drain finds
        nothing left but still consults the per-port error map).
        """
        drain = getattr(self._backend, "drain_events", None)
        new_errors = drain() if callable(drain) else []
        for message in new_errors:
            self.error.emit(f"Sensor error -- {message}")

        states_fn = getattr(self._backend, "p1_states", None)
        if callable(states_fn):
            return tuple(states_fn())
        return ()
