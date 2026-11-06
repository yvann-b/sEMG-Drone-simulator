"""The real P0 backend: ring ownership + P1 (acquisition) lifecycle.

This is the "real spawner" that replaces :class:`~p0.controller.FakeBackend` at
integration time. It does two things **for real** today:

1. **Owns the data plane.** On construction it creates ``NUMBER_SHIMMER``
   ``EmgAccelRing`` + ``ImuRing`` pairs (``is_creating=True``) and keeps them
   alive for the whole session. This is the *only* place the shared-memory
   segments are created and the only place they are unlinked.
2. **Spawns / stops P1.** ``start_acquisition()`` spawns one
   ``multiprocessing`` child per Shimmer running
   :class:`~p1_device_acquisition.shimmer_acquisition.ShimmerAcquisition`;
   ``stop_acquisition()`` asks every child to stop and reaps it.

The model and simulator sides are **not implemented yet**. They exist here as
clearly-marked *stubs* so that the real backend is a drop-in replacement for
:class:`~p0.controller.FakeBackend` -- the controller already calls those methods
every tick, and they must not crash the loop. Each stub returns "nothing" and is
tagged ``NOT IMPLEMENTED`` so it is obvious what is still missing.

Process model note
------------------
The rings are handed to the children **by pickling**. ``CircularBuffer`` defines
``__getstate__`` / ``__setstate__`` so the shared-memory handle and the shared
``write_count`` value survive the trip; the child re-attaches to the same
segment. This is the mechanism exercised by ``tests/test_shimmer_ring.py``
(``test_multiprocess_pickle``), so it is already proven.

Ports
-----
P1 needs one Bluetooth COM port per Shimmer. ``ports`` is a list of length
``NUMBER_SHIMMER``; if it is shorter than the ring count, the missing entries
fall back to ``default_port`` (``COM3``). This is intentionally a small helper
rather than a config file (see ``docs/PROJECT_LAYOUT.md``: no central config).
"""

from __future__ import annotations

import multiprocessing as mp
from dataclasses import dataclass
from multiprocessing import shared_memory

from utils.emg_accel_ring import EmgAccelRing
from utils.imu_ring import ImuRing

#: Sampling rate requested from every Shimmer. Hardcoded like the other P0
#: constants (tick interval, ring capacity): keeps ``python -m p0`` runnable.
SAMPLING_RATE_HZ = 512.0

#: Ring capacity in slots. At 512 Hz this is ~16 s of history per ring, enough
#: for any model window while staying small in shared memory.
RING_CAPACITY = 8192

#: Default Bluetooth outgoing COM port for the first Shimmer.
DEFAULT_PORT = "COM3"


def _unlink_if_stale(name: str) -> None:
    """Remove a leftover shared-memory segment, if any.

    P0 is the *sole owner* of its rings. If a segment with our name already
    exists when we start, it is a leftover from a previous P0 that crashed
    before ``close()`` ran -- never a live owner (a live owner would still hold
    the app's lock). Unlinking it here means a crash no longer makes the next
    launch fail with ``FileExistsError``.
    """
    try:
        stale = shared_memory.SharedMemory(name=name)
    except FileNotFoundError:
        return
    except Exception:  # noqa: BLE001 -- permission oddities: leave it alone
        return
    try:
        stale.close()
        stale.unlink()
    except Exception:  # noqa: BLE001
        pass


@dataclass
class _ShimmerProcess:
    """Bookkeeping for one spawned P1 child (kept in the parent only)."""

    index: int
    port: str
    emg_ring: EmgAccelRing
    imu_ring: ImuRing
    process: mp.Process
    #: A stop flag shared with the child so it can exit its run loop.
    stop_event: object
    #: Parent's end of a one-way pipe carrying child error reports.
    event_conn: object
    #: EMG ring ``write_count`` seen on the previous poll, used to tell
    #: "connecting" (no samples yet) from "online" (samples advancing).
    last_write_count: int = 0


def _run_p1(
    port: str,
    emg_ring: EmgAccelRing,
    imu_ring: ImuRing,
    stop_event,
    rate: float,
    calibrate: bool,
    event_conn,
) -> None:
    """Child-process entry point for one Shimmer.

    Runs in its own interpreter. It builds a
    :class:`~p1_device_acquisition.shimmer_acquisition.ShimmerAcquisition`,
    then blocks until ``stop_event`` is set. Importing P1 *here* (not at module
    import time) keeps the parent's P0 import graph free of the Bluetooth stack
    and avoids loading ``pyshimmer`` before a child is actually spawned.
    """
    from p1_device_acquisition.shimmer_acquisition import ShimmerAcquisition

    acquisition = ShimmerAcquisition(
        emg_ring,
        imu_ring,
        port=port,
        rate=rate,
        calibrate=calibrate,
    )
    # Let P1 see the shared stop flag so ``stop_event.set()`` ends the run.
    acquisition.stop_event = stop_event  # type: ignore[attr-defined]
    try:
        acquisition.run()
    except Exception as exc:  # noqa: BLE001 -- child must not hang the parent
        # Report to the GUI *and* the console. The parent relay drains
        # ``event_conn`` each tick and surfaces this in the status bar.
        message = f"{type(exc).__name__}: {exc}"
        print(f"[P0 child P1:{port}] acquisition failed: {message}")
        try:
            # ``Pipe.send`` writes synchronously to the OS pipe -- no feeder
            # thread -- so the message is on the wire before we exit. This is
            # why a pipe is used here instead of ``mp.Queue`` (whose put only
            # queues to a background thread that can lose the item on exit).
            event_conn.send({"kind": "error", "port": port, "detail": message})
        except Exception:  # noqa: BLE001 -- reporting must never re-raise
            pass
        raise
    finally:
        # The child only detaches; the parent owns unlink().
        emg_ring.close()
        imu_ring.close()


class DeviceBackend:
    """Ring owner + P1 spawner. The real backend for :class:`Controller`."""

    def __init__(
        self,
        number_shimmer: int,
        *,
        ports: list[str] | None = None,
        rate: float = SAMPLING_RATE_HZ,
        ring_capacity: int = RING_CAPACITY,
        calibrate: bool = True,
        ctx=None,
    ) -> None:
        self.number_shimmer = max(1, int(number_shimmer))
        self.rate = rate
        self.ring_capacity = ring_capacity
        self.calibrate = calibrate
        self.ctx = ctx or mp.get_context("spawn")

        # Resolve one port per Shimmer, falling back to DEFAULT_PORT.
        resolved = list(ports or [])
        self.ports: list[str] = [
            resolved[i] if i < len(resolved) else DEFAULT_PORT
            for i in range(self.number_shimmer)
        ]

        # Create the rings ONCE, here, and keep the handles for the session.
        # Creation must happen in the parent so a single owner unlinks them.
        self.emg_rings: list[EmgAccelRing] = []
        self.imu_rings: list[ImuRing] = []
        for index in range(self.number_shimmer):
            emg_name = f"emg_ring_{index}"
            imu_name = f"imu_ring_{index}"
            # Clear a segment leaked by a crashed previous run, so relaunching
            # the app never fails with FileExistsError.
            _unlink_if_stale(emg_name)
            _unlink_if_stale(imu_name)
            self.emg_rings.append(
                EmgAccelRing(emg_name, ring_capacity, is_creating=True)
            )
            self.imu_rings.append(
                ImuRing(imu_name, ring_capacity, is_creating=True)
            )

        self._shimmer_procs: list[_ShimmerProcess] = []

        # --- control-plane placeholders (models / simulator: later steps) ---
        # The controller calls the control-plane methods below every tick; we
        # keep the tiny bits of state they need so those calls are cheap and
        # cannot crash the loop. None of this talks to a real process yet.
        self._selected: str | None = None
        self._pending_ps: list[dict] = []

        #: Most recent P1 failure reported by a child, or ``None``. The
        #: controller reads this through :meth:`drain_events` and shows it in
        #: the GUI, so a Bluetooth error is never console-only.
        self.last_error: str | None = None
        #: Latest failure per port, so a dead Shimmer stays "error" even after
        #: the one-shot pipe message has been drained.
        self._errors_by_port: dict[str, str] = {}

    # ---------------------------------------------------- acquisition (P1)
    # === IMPLEMENTED ===
    @property
    def acquiring(self) -> bool:
        """``True`` while any P1 child is alive."""
        return any(proc.process.is_alive() for proc in self._shimmer_procs)

    def start_acquisition(self) -> None:
        """Spawn one P1 child per Shimmer. No-op if already running."""
        if self.acquiring:
            return
        if self._shimmer_procs:
            # Stale (dead) handles from a previous run: reap before respawning.
            self._reap()

        # A fresh start clears any error from a previous (failed) attempt.
        self.last_error = None
        self._errors_by_port = {}

        for index in range(self.number_shimmer):
            stop_event = self.ctx.Event()
            # One-way pipe: child sends error reports, parent polls them.
            # ``duplex=False`` -> (recv_conn, send_conn).
            parent_conn, child_conn = self.ctx.Pipe(duplex=False)
            process = self.ctx.Process(
                target=_run_p1,
                args=(
                    self.ports[index],
                    self.emg_rings[index],
                    self.imu_rings[index],
                    stop_event,
                    self.rate,
                    self.calibrate,
                    child_conn,
                ),
                name=f"P1:{self.ports[index]}",
                daemon=True,
            )
            process.start()
            self._shimmer_procs.append(
                _ShimmerProcess(
                    index=index,
                    port=self.ports[index],
                    emg_ring=self.emg_rings[index],
                    imu_ring=self.imu_rings[index],
                    process=process,
                    stop_event=stop_event,
                    event_conn=parent_conn,
                )
            )
            # The parent never writes to the pipe; close its copy of the send
            # end so EOF is detectable and the handle is not leaked.
            child_conn.close()

    def stop_acquisition(self) -> None:
        """Signal every P1 child to stop, then join them all.

        Same shape as the precedent project's shutdown::

            stop_event.set()        # ask every child to stop
            for p in processes:     # wait until each has actually exited
                p.join()

        Joining *before* unlink is the whole point: a child that still maps the
        shared memory must be gone before :meth:`close` unlinks it.
        """
        for proc in self._shimmer_procs:
            proc.stop_event.set()
        self._reap()

    def _reap(self) -> None:
        """Join every child, then drop the handles."""
        for proc in self._shimmer_procs:
            proc.process.join()
            # Drain any final error the child reported on its way out.
            self.drain_events()
        self._shimmer_procs = []

    # ---------------------------------------------- P1 health (for the GUI)
    # === IMPLEMENTED ===
    def drain_events(self) -> list[str]:
        """Drain child error reports (non-blocking) and remember the last one.

        Returns the new error strings so the caller can log/show them. A
        one-way pipe carries a failure from the *child* across the process
        boundary; the console print in the child is kept for debugging,
        but this is what reaches the GUI.
        """
        messages: list[str] = []
        for proc in self._shimmer_procs:
            conn = proc.event_conn
            if conn is None:
                continue
            while True:
                try:
                    if not conn.poll():
                        break
                    event = conn.recv()
                except (EOFError, OSError):
                    break
                if isinstance(event, dict) and event.get("kind") == "error":
                    detail = event.get("detail", "unknown error")
                    port = event.get("port", proc.port)
                    self._errors_by_port[port] = detail
                    messages.append(f"{port}: {detail}")
        if messages:
            self.last_error = messages[-1]
        return messages

    def p1_states(self) -> list[str]:
        """Per-Shimmer health, one entry per child, in spawn order.

        Each entry is ``offline`` / ``connecting`` / ``online`` / ``error``:

        * ``offline``   -- that child is not alive (not started, exited, or it
                           died after reporting an error).
        * ``error``     -- that child is not alive *and* reported an error on
                           its way out (see :attr:`last_error` per port).
        * ``online``    -- its EMG ring ``write_count`` advanced since the last
                           poll: it is really streaming samples.
        * ``connecting``-- it is alive but no samples have arrived yet (still
                           opening the port / reading calibration).

        The GUI shows one ``Sensor <i>:`` chip per entry, so a single dead
        Shimmer is visible next to a healthy one.
        """
        self.drain_events()
        states: list[str] = []
        for proc in self._shimmer_procs:
            count = proc.emg_ring.write_count.value
            advancing = count > proc.last_write_count
            proc.last_write_count = count
            if proc.process.is_alive():
                states.append("online" if advancing else "connecting")
            elif proc.port in self._errors_by_port:
                states.append("error")
            else:
                states.append("offline")
        return states

    def p1_state(self) -> str:
        """Aggregate health across all Shimmers (for non-GUI callers).

        ``online`` if any Shimmer is online, else ``error`` if any reported an
        error, else ``connecting`` if any is still connecting, else ``offline``.
        The GUI uses :meth:`p1_states` for the per-Shimmer view.
        """
        states = self.p1_states()
        if not states:
            return "offline"
        for wanted in ("online", "error", "connecting"):
            if wanted in states:
                return wanted
        return "offline"

    # ------------------------------------------------ control plane (stubs)
    # === NOT IMPLEMENTED (models / simulator are later steps) ===
    # These mirror the slice of FakeBackend that Controller already uses, so
    # the real backend is a drop-in replacement. Each returns "nothing" today.

    def drain_model_queues(self, now: float):
        """NOT IMPLEMENTED -- no model processes are spawned yet."""
        return []

    def forward(self, label: str, deadline: float) -> None:
        """NOT IMPLEMENTED -- no simulator queue yet; drop the command."""
        self._pending_ps.append({"label": label, "deadline": deadline})

    def drain_ps(self) -> list[dict]:
        """NOT IMPLEMENTED -- nothing is queued for the simulator yet."""
        out, self._pending_ps = self._pending_ps, []
        return out

    def select(self, name: str | None) -> None:
        """IMPLEMENTED (trivial) -- remembers the selection for ``selected``."""
        self._selected = name

    @property
    def selected(self) -> str | None:
        return self._selected

    # -------------------------------------------------------------- cleanup
    # === IMPLEMENTED ===
    def close(self) -> None:
        """Stop P1, then unlink the rings (owner only).

        Order matters: children must have detached (``close``) before the
        parent unlinks, otherwise other processes' mappings are invalidated.
        """
        self.stop_acquisition()
        for ring in (*self.emg_rings, *self.imu_rings):
            try:
                ring.close()
                ring.unlink()
            except Exception:  # noqa: BLE001 -- cleanup must not raise
                pass

    def __enter__(self) -> "DeviceBackend":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


__all__ = [
    "DeviceBackend",
    "SAMPLING_RATE_HZ",
    "RING_CAPACITY",
    "DEFAULT_PORT",
]
