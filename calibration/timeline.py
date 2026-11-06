"""The calibration *timeline*: which slot comes next, and for how long.

This module is **pure data + pure functions**. It knows nothing about rings,
processes, Qt or the filesystem. Given a
:class:`~p0.calibration_config.CalibrationConfig` it builds the ordered list of
:class:`Phase` objects that the recorder (Pc) will walk:

    intro        "Calibration will begin..."           (short lead-in)
    rest          rest_time_s of rest  -- labelled "rest"
    gesture       movement_time_s of <g> -- labelled <g>
    rest          rest_time_s of rest  -- labelled "rest"
    gesture       ...
    ...
    done         "Calibration complete"                 (short tail)

Two design points
-----------------
* **Random gesture order.** Gestures are NOT presented ``g1, g2, g3 ...``.
  Each *round* (one pass through every gesture) is shuffled independently, so
  the user cannot anticipate the next move. The shuffle is seeded so a run is
  reproducible from its config; pass ``seed=None`` for true randomness.
* **Every activity slot is preceded by a rest slot.** A rest before each gesture
  gives the arm a neutral baseline and makes the blocks independent, which is
  exactly what the models need. The label vocabulary itself (gestures + optional
  ``noise`` + ``rest``) is still owned by the config (``config.labels()``); the
  timeline only decides the *schedule*.

The ``noise`` gesture, when enabled, participates in the shuffle like any other
gesture and gets its own labelled slots.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from p0.calibration_config import REST_LABEL, CalibrationConfig

#: Kind of a phase. ``intro`` / ``done`` carry no samples; ``rest`` and
#: ``gesture`` are the *recording* slots (each becomes one window in the .npz).
PHASE_INTRO = "intro"
PHASE_REST = "rest"
PHASE_GESTURE = "gesture"
PHASE_DONE = "done"

#: Short lead-in before the first rest slot, so the user can get ready and the
#: recorder can seek to the live end of the ring.
INTRO_SECONDS = 3.0

#: Short tail so the final gesture window is fully flushed before saving.
OUTRO_SECONDS = 1.0

#: Label used for a rest slot's window. Imported so the timeline, the recorder
#: and the model all agree on the single reserved word.
REST_WINDOW_LABEL = REST_LABEL


@dataclass(frozen=True)
class Phase:
    """One scheduled step of the recording.

    ``label`` is the *window label* written to the .npz for a recording slot
    (``"rest"`` or a gesture name); ``None`` for ``intro``/``done``. ``image``
    is the gesture's bare preview filename (``None`` for rest and the boundary
    phases). ``repetition`` is 1-based so the window can show "3 / 5".
    """

    kind: str
    duration_s: float
    label: str | None = None
    image: str | None = None
    repetition: int | None = None
    total_repetitions: int | None = None

    @property
    def is_recording(self) -> bool:
        """``True`` for the slots that actually produce a labelled window."""
        return self.kind in (PHASE_REST, PHASE_GESTURE)


def _round_gestures(config: CalibrationConfig, rng: random.Random) -> list:
    """One round = every effective gesture, shuffled.

    ``config.effective_gestures()`` already includes the derived ``noise`` row
    when enabled, so the shuffle naturally mixes ``noise`` in with the user
    gestures instead of always putting it last.
    """
    gestures = list(config.effective_gestures())
    rng.shuffle(gestures)
    return gestures


def build_timeline(
    config: CalibrationConfig,
    *,
    seed: int | None = None,
) -> list[Phase]:
    """Build the full ordered list of phases for one recording.

    Parameters
    ----------
    config:
        The recipe. Its ``effective_gestures()`` (user gestures + optional
        ``noise``) and its ``default_repetitions`` / ``movement_time_s`` /
        ``rest_time_s`` define the schedule.
    seed:
        Seed for the per-round shuffle. ``None`` -> the OS entropy source (a
        genuinely random order each run). A fixed ``seed`` makes a run
        reproducible (used by the tests).

    Returns
    -------
    list[Phase]
        ``[intro, (rest, gesture) * rounds * gestures, done]``. Rest always
        precedes its gesture.
    """
    rng = random.Random(seed)

    # A single global repetition count drives every gesture (the config's
    # design: one field bulk-applied to all rows). Guard against a bad value.
    repetitions = max(1, int(config.default_repetitions))
    movement_s = float(config.movement_time_s)
    rest_s = float(config.rest_time_s)

    phases: list[Phase] = [
        Phase(kind=PHASE_INTRO, duration_s=INTRO_SECONDS),
    ]

    for repetition in range(1, repetitions + 1):
        for gesture in _round_gestures(config, rng):
            # Rest slot first: neutral baseline before the activity, and its own
            # labelled window.
            phases.append(
                Phase(
                    kind=PHASE_REST,
                    duration_s=rest_s,
                    label=REST_WINDOW_LABEL,
                    image=None,
                    repetition=repetition,
                    total_repetitions=repetitions,
                )
            )
            # Then the gesture slot.
            phases.append(
                Phase(
                    kind=PHASE_GESTURE,
                    duration_s=movement_s,
                    label=gesture.name,
                    image=gesture.image,
                    repetition=repetition,
                    total_repetitions=repetitions,
                )
            )

    phases.append(Phase(kind=PHASE_DONE, duration_s=OUTRO_SECONDS))
    return phases


def timeline_duration_s(phases: list[Phase]) -> float:
    """Total planned duration (sum of every phase), in seconds.

    Drives the GUI progress bar: ``progress = elapsed_planned / total``.
    """
    return float(sum(phase.duration_s for phase in phases))


def count_recording_slots(phases: list[Phase]) -> int:
    """How many labelled windows the recording will produce."""
    return sum(1 for phase in phases if phase.is_recording)


__all__ = [
    "PHASE_INTRO",
    "PHASE_REST",
    "PHASE_GESTURE",
    "PHASE_DONE",
    "INTRO_SECONDS",
    "OUTRO_SECONDS",
    "REST_WINDOW_LABEL",
    "Phase",
    "build_timeline",
    "timeline_duration_s",
    "count_recording_slots",
]
