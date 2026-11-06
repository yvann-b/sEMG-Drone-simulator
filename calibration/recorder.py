"""Pc -- the recorder: ring readings -> ``data/calib_*.npz``.

The process walks the timeline built by :func:`calibration.timeline.build_timeline`
by the **monotonic clock** and, for every recording slot (rest or gesture),
collects *every* ring record whose arrival falls inside that slot into one
window. Nothing is trimmed, filtered, blended or merged: a rest window is only
rest samples, a gesture window is only that gesture's samples. Transition
handling belongs to the model, not here (calibration stays role-pure).

Reading the ring
----------------
The ring's single consumer cursor is ``"calib"`` (see ``ARCHITECTURE.md`` §4.1).
We use :meth:`read_next_fifo` in a non-blocking polling loop:

* ``gap > 0`` means samples were overwritten before we read them -- the window
  would be silently corrupt, so the run **aborts with an error** (Pc contract).
* `None` means "nothing new yet", which is a normal state between packets.

Synchronisation with the GUI
----------------------------
Pc is **headless**. It streams its phase to P0 over a one-way ``Pipe`` -- one
small dict per change -- so P0's window can show the image/prompt/progress. The
same one-way-pipe rule as P1 is followed: an error is sent with ``Pipe.send``
(synchronous) so it is on the wire before the process exits.

Dataset layout (``ARCHITECTURE.md`` §4.5)
-----------------------------------------
Two groups of keys, kept separate on purpose:

* **Offline-compatible** (matches ``hand_gesture_recognition_v1.py``):
  ``X``  ``float32[n_windows, n_samples, 5]`` -- per-window raw readings
         (2 EMG + 3 accel, timestamps dropped *inside* a window);
  ``y``  label per window (a gesture name, or ``"rest"``);
  ``blocks``  int: the repetition number each window came from (cross-val id);
  ``sampling_rate``  Hz;
  ``labels_map``  the ordered vocabulary (``config.labels()``, ``rest`` last).
* **Metadata** (provenance): ``config_name``, ``config_path``, ``session_name``,
  ``date_iso``, ``recorded_at`` (epoch seconds).

Windows that summed to zero samples (e.g. the ring was not streaming) are
dropped, so ``X.shape[0] == len(y) == len(blocks)`` always holds.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from calibration.timeline import (
    PHASE_DONE,
    PHASE_GESTURE,
    PHASE_INTRO,
    Phase,
    build_timeline,
    timeline_duration_s,
)
from p0.calibration_config import PROJECT_ROOT, CalibrationConfig
from utils.emg_accel_ring import EmgAccelRing

#: Project data folder for datasets (git-ignored). Created on demand.
DATA_DIR = PROJECT_ROOT / "data"

#: How often the recorder polls the ring while inside a slot (seconds). ~5 ms
#: catches every 512 Hz sample; ``read_next_fifo`` drains in a tight loop so a
#: few-ms poll never falls behind.
POLL_INTERVAL_S = 0.005

#: The single consumer cursor id used for calibration (see ARCHITECTURE.md §4.1).
CURSOR_ID = "calib"


@dataclass
class _Window:
    """Accumulates the samples of one recording slot."""

    label: str
    repetition: int
    samples: list[np.ndarray] = field(default_factory=list)
    #: Per-sample ring timestamps (ns); kept only to infer the sample rate.
    timestamps: list[int] = field(default_factory=list)

    def add(self, record) -> None:
        # Stack EMG[2] + accel[3] into a flat 5-vector per sample.








        emg = np.asarray(record["emg"], dtype=np.float32)
        accel = np.asarray(record["accel"], dtype=np.float32)
        self.samples.append(np.concatenate((emg, accel)))
        self.timestamps.append(int(record["timestamp_ns"]))

    def as_array(self) -> np.ndarray | None:
        if not self.samples:
            return None
        return np.asarray(self.samples, dtype=np.float32)

    def sampling_rate_hz(self) -> float | None:
        """Estimate the ring sample rate from this window's timestamps."""
        if len(self.timestamps) < 2:
            return None
        span_s = (self.timestamps[-1] - self.timestamps[0]) / 1e9
        if span_s <= 0:
            return None
        return (len(self.timestamps) - 1) / span_s


class Pc:
    """The calibration recorder process.

    Parameters
    ----------
    ring:
        The (pickled) :class:`~utils.emg_accel_ring.EmgAccelRing` to read.
    config:
        The loaded recipe. The timeline is built from it.
    session_name:
        Optional human name for this run (stored in the ``.npz``). Falls back
        to ``config.name`` + timestamp.
    output_dir:
        Where to write the ``.npz``. Defaults to :data:`DATA_DIR`.
    event_conn:
        Child end of a one-way ``Pipe`` to P0. When ``None`` (standalone run)
        no events are sent and progress is printed instead.
    stop_event:
        Shared ``mp.Event`` set by P0 to cancel the run early.
    seed:
        Shuffle seed for the timeline (``None`` = random order each run).
    """

    def __init__(
        self,
        ring: EmgAccelRing,
        config: CalibrationConfig,
        *,
        config_path: str | Path | None = None,
        session_name: str | None = None,
        output_dir: str | Path | None = None,
        event_conn=None,
        stop_event=None,
        seed: int | None = None,
        sampling_rate_hz: float | None = None,
    ) -> None:
        self.ring = ring
        self.config = config
        self.config_path = str(config_path) if config_path is not None else None
        self.session_name = (session_name or "").strip() or self._default_name(config)
        self.output_dir = Path(output_dir) if output_dir is not None else DATA_DIR
        self.event_conn = event_conn
        self.stop_event = stop_event
        self.seed = seed
        self.sampling_rate_hz = float(sampling_rate_hz) if sampling_rate_hz else None

        self.phases: list[Phase] = build_timeline(config, seed=seed)
        self.total_duration_s = timeline_duration_s(self.phases)

    # ------------------------------------------------------------- naming
    @staticmethod
    def _default_name(config: CalibrationConfig) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{(config.name or 'calibration')}_{stamp}"

    def _output_path(self) -> Path:
        safe = "".join(
            c if (c.isalnum() or c in "-_.") else "_" for c in self.session_name
        ).strip("_") or "calibration"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.output_dir / f"calib_{safe}_{stamp}.npz"

    # ------------------------------------------------------------- events
    def _emit(self, payload: dict) -> None:
        """Send one event to P0, or print it when running standalone."""
        if self.event_conn is not None:
            try:
                self.event_conn.send(payload)
                return
            except Exception:  # noqa: BLE001 -- reporting must never crash Pc
                pass
        if payload.get("kind") in ("phase", "done", "error"):
            print(f"[Pc] {payload}")

    def _should_stop(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    # --------------------------------------------------------------- run
    def run(self) -> Path | None:
        """Execute the timeline, read the ring, and save the dataset.

        Returns the written ``.npz`` path, or ``None`` if the run was cancelled
        or aborted. Errors/cancellation are reported over the pipe.
        """
        # Start reading "now": skip stale history up to the current write head.
        self.ring.seek_to_latest(CURSOR_ID)

        windows: list[_Window] = []
        planned_elapsed = 0.0
        current: _Window | None = None

        try:
            for index, phase in enumerate(self.phases):
                if self._should_stop():
                    self._emit({"kind": "error", "detail": "cancelled"})
                    return None

                self._emit(
                    {
                        "kind": "phase",
                        "index": index,
                        "total": len(self.phases),
                        "phase": phase.kind,
                        "label": phase.label,
                        "image": phase.image,
                        "repetition": phase.repetition,
                        "total_repetitions": phase.total_repetitions,
                        "duration_s": phase.duration_s,
                        "progress": (
                            planned_elapsed / self.total_duration_s
                            if self.total_duration_s > 0
                            else 0.0
                        ),
                    }
                )

                if phase.kind == PHASE_INTRO:
                    self._wait(phase.duration_s, planned_elapsed)
                    planned_elapsed += phase.duration_s
                    continue

                if phase.kind == PHASE_DONE:
                    # A tiny tail so the last gesture window is fully flushed.
                    self._wait(phase.duration_s, planned_elapsed)
                    planned_elapsed += phase.duration_s
                    break

                if phase.is_recording:
                    current = _Window(
                        label=phase.label or "",
                        repetition=int(phase.repetition or 0),
                    )
                    gap = self._record_slot(
                        current, phase.duration_s, planned_elapsed, index, phase
                    )
                    if gap > 0:
                        self._emit(
                            {
                                "kind": "error",
                                "detail": (
                                    f"Ring gap of {gap} samples during "
                                    f"'{phase.label}' -- the recording would be "
                                    "corrupt. Aborted."
                                ),
                            }
                        )
                        return None
                    windows.append(current)
                    current = None
                    planned_elapsed += phase.duration_s

            if self._should_stop():
                self._emit({"kind": "error", "detail": "cancelled"})
                return None

            path = self._save(windows)
            self._emit({"kind": "done", "path": str(path), "progress": 1.0})
            return path

        except Exception as exc:  # noqa: BLE001 -- report, never hang the parent
            self._emit({"kind": "error", "detail": f"{type(exc).__name__}: {exc}"})
            return None
        finally:
            if self.event_conn is not None:
                try:
                    self.event_conn.close()
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------ helpers
    def _wait(self, duration_s: float, planned_elapsed: float) -> None:
        """Idle for a boundary phase (intro/outro) while staying responsive.

        Sleeps in small chunks so a cancel is honoured near-instantly.
        """
        end = time.monotonic() + duration_s
        while time.monotonic() < end:
            if self._should_stop():
                return
            time.sleep(POLL_INTERVAL_S)

    def _record_slot(
        self,
        window: _Window,
        duration_s: float,
        planned_elapsed: float,
        index: int,
        phase: Phase,
    ) -> int:
        """Fill ``window`` with this slot's readings; return the ring gap seen.

        Every record arriving between now and ``now + duration_s`` is appended
        as-is (no trimming). ``progress`` is emitted as the slot elapses.
        """
        start = time.monotonic()
        end = start + duration_s
        last_emit = 0.0
        gap_total = 0

        while True:
            if self._should_stop():
                return gap_total

            now = time.monotonic()
            result = self.ring.read_next_fifo(CURSOR_ID)
            if result is not None:
                record, _last_seen, gap = result
                if gap > 0:
                    gap_total += int(gap)
                window.add(record)

            # Emit progress a few times per slot (not every 5 ms).
            if now - last_emit >= 0.1:
                within = min(1.0, (now - start) / duration_s) if duration_s > 0 else 1.0
                overall = (
                    (planned_elapsed + within * duration_s) / self.total_duration_s
                    if self.total_duration_s > 0
                    else 0.0
                )
                self._emit(
                    {
                        "kind": "phase",
                        "index": index,
                        "total": len(self.phases),
                        "phase": phase.kind,
                        "label": phase.label,
                        "image": phase.image,
                        "repetition": phase.repetition,
                        "total_repetitions": phase.total_repetitions,
                        "duration_s": duration_s,
                        "progress": overall,
                    }
                )
                last_emit = now

            if now >= end:
                break
            time.sleep(POLL_INTERVAL_S)

        return gap_total

    # --------------------------------------------------------------- save
    def _save(self, windows: list[_Window]) -> Path:
        """Assemble and write the ``.npz`` dataset. Returns its path."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self._output_path()

        arrays = [w.as_array() for w in windows]
        kept = [(w, arr) for w, arr in zip(windows, arrays) if arr is not None]

        if not kept:
            # No samples at all (ring not streaming): refuse to write an empty
            # dataset rather than produce a file the models cannot use.
            raise RuntimeError(
                "No samples were recorded -- is acquisition running?"
            )

        n_samples = min(arr.shape[0] for _, arr in kept)
        # Truncate every window to the shortest, so X is a rectangular array.
        X = np.stack([arr[:n_samples] for _, arr in kept], axis=0).astype(np.float32)
        y = np.array([w.label for w, _ in kept])
        blocks = np.array([w.repetition for w, _ in kept], dtype=np.int64)

        # Sampling rate: caller-provided, else inferred from the windows.
        sampling_rate = self.sampling_rate_hz
        if not sampling_rate:
            rates = [w.sampling_rate_hz() for w, _ in kept]
            rates = [r for r in rates if r]
            sampling_rate = float(np.median(rates)) if rates else 0.0

        labels = self.config.labels()

        now = datetime.now()
        np.savez_compressed(
            path,
            # --- offline-compatible keys (hand_gesture_recognition_v1.py) ---
            X=X,
            y=y,
            blocks=blocks,
            sampling_rate=float(sampling_rate),
            labels_map=np.array(labels),
            labels=np.array(labels),
            # --- provenance / metadata ---
            config_name=np.array(self.config.name),
            config_path=np.array(self.config_path or ""),
            session_name=np.array(self.session_name),
            date_iso=np.array(now.isoformat(timespec="seconds")),
            recorded_at=np.array(float(now.timestamp())),
        )
        return path


__all__ = ["Pc", "DATA_DIR", "CURSOR_ID", "POLL_INTERVAL_S"]
