"""State shared between the control loop and the GUI.

This module is the *contract* between :mod:`p0.controller` and
:mod:`p0.main_window`. Both import it; it imports nothing of ours and touches
no process, no ring and no queue. That is what lets the GUI be a pure
function of a snapshot.

A snapshot is **immutable**. The controller builds a fresh one each tick and
hands it to the model; the model never mutates it, the GUI never writes back.
That keeps "who owns the truth" unambiguous: the controller does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: The only statuses a model row may hold. Unknown text is drawn neutral;
#: ``idle`` is the starting state before a model process has ever been spawned.
ModelStatus = Literal["idle", "running", "training", "error", "stopped"]

ALL_STATUSES: tuple[ModelStatus, ...] = (
    "idle",
    "running",
    "training",
    "error",
    "stopped",
)

#: Placeholder label shown when a model has not emitted a classification yet.
EM_DASH = "\u2014"


@dataclass(frozen=True)
class ModelRow:
    """Everything the table needs to know about one ``Pm_i`` process.

    ``last_seen`` is a **monotonic** timestamp in seconds, or ``None`` if the
    model has never emitted. The *Age* column is derived from it at paint time
    (:func:`format_age`) so a stalled model's age keeps growing without the
    controller having to rewrite the value every tick.
    """

    name: str
    status: ModelStatus = "idle"
    last_label: str | None = None
    last_confidence: float | None = None
    last_seen: float | None = None

    def display_label(self) -> str:
        return self.last_label if self.last_label else EM_DASH

    def display_confidence(self) -> str:
        if self.last_confidence is None:
            return EM_DASH
        return f"{self.last_confidence:.2f}"


#: The phases a calibration run can be in, as reported by Pc. ``idle`` means
#: "no run"; the rest mirror :mod:`calibration.timeline`.
CalibrationPhase = Literal["idle", "intro", "rest", "gesture", "done"]


@dataclass(frozen=True)
class CalibrationRun:
    """The live state of one calibration recording (Pc), rendered by the GUI.

    Built each tick by the controller from the events Pc streams over its pipe.
    ``active`` is ``True`` while Pc is running; ``done``/``error`` mark the end.
    ``progress`` is ``0.0..1.0`` planned-time completion (drives the bar).
    ``label`` is the *next move* for a gesture slot (a gesture name), or
    ``"rest"``; ``image`` is the gesture's bare preview filename.
    """

    active: bool = False
    phase: CalibrationPhase = "idle"
    label: str | None = None
    image: str | None = None
    repetition: int | None = None
    total_repetitions: int | None = None
    progress: float = 0.0
    session_name: str = ""
    done: bool = False
    error: str | None = None
    output_path: str | None = None

    def is_finished(self) -> bool:
        """``True`` once the run ended (success or error)."""
        return self.done or self.error is not None


@dataclass(frozen=True)
class SystemSnapshot:
    """One tick's worth of truth, handed to the GUI.

    Defaults describe "nothing running yet", which is exactly what the window
    shows before the controller's first tick.
    """

    models: tuple[ModelRow, ...] = ()
    p1_rate_hz: float | None = None
    #: Per-Shimmer health, one entry per sensor in spawn order:
    #: "offline" | "connecting" | "online" | "error". The status bar shows one
    #: "Sensor <i>:" chip per entry, so the count and the states both come from
    #: the snapshot -- the window never needs to know NUMBER_SHIMMER.
    p1_states: tuple[str, ...] = ()
    ps_state: str | None = None
    now: float | None = None            # monotonic clock used for Age
    #: Live state of the calibration recording (default: no run).
    calibration: CalibrationRun = field(default_factory=CalibrationRun)

    def as_dict(self) -> dict:
        return {
            "models": self.models,
            "p1_rate_hz": self.p1_rate_hz,
            "p1_states": self.p1_states,
            "ps_state": self.ps_state,
            "now": self.now,
            "calibration": self.calibration,
        }


def format_age(last_seen: float | None, now: float | None) -> str:
    """Render the age of the last message as a short human string."""
    if last_seen is None or now is None:
        return EM_DASH
    age = now - last_seen
    if age < 0:          # clock skew between producer and consumer
        return "0.0s"
    if age < 10:
        return f"{age:.1f}s"
    return f"{age:.0f}s"


def empty_snapshot() -> SystemSnapshot:
    """The snapshot a controller returns before anything has happened."""
    return SystemSnapshot(
        models=tuple(),
        p1_rate_hz=None,
        p1_states=(),
        ps_state=None,
    )

