"""Pc -- the calibration (recording) process.

Turns one calibration *configuration* (``calibrations/<name>.json``) into one
immutable *dataset* (``data/calib_*.npz``). See ``docs/ARCHITECTURE.md`` §3 (Pc)
and §4.5 (dataset layout).

Two small, independent pieces:

* :mod:`calibration.timeline` -- **pure** logic: which slot (rest / gesture)
  comes next and how long it lasts. No rings, no processes, easy to unit-test.
* :mod:`calibration.recorder` -- the :class:`Pc` process: walks the timeline by
  the monotonic clock, reads the EMG ring with ``read_next_fifo("calib")`` and
  stores every slot's readings **as-is** (no trimming, no filtering). It streams
  the current phase to P0 over a ``Pipe`` so P0 can show the guided window.

Raw-only contract
-----------------
Calibration records *raw ring readings*, nothing more: one window per slot, each
window labelled with its slot category (a gesture name or ``"rest"``). Any
transition handling (cutting edges, windowing) is the *model's* job, not Pc's.
This keeps the two roles cleanly separated.
"""

from calibration.recorder import Pc
from calibration.timeline import Phase, build_timeline, timeline_duration_s

__all__ = ["Pc", "Phase", "build_timeline", "timeline_duration_s"]
