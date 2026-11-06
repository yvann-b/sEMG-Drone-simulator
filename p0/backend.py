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
from p0.state import CalibrationRun

#: Sampling rate requested from every Shimmer. Hardcoded like the other P0
#: constants (tick interval, ring capacity): keeps ``python -m p0`` runnable.
SAMPLING_RATE_HZ = 512.0

#: Ring capacity in slots. At 512 Hz this is ~16 s of history per ring, enough
#: for any model window while staying small in shared memory.
RING_CAPACITY = 8192

#: Default Bluetooth outgoing COM port for the first Shimmer.
DEFAULT_PORT = "COM5"


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


def _send(conn, payload: dict) -> None:
    """Best-effort synchronous send on a one-way pipe."""
    try:
        conn.send(payload)
    except Exception:  # noqa: BLE001 -- reporting must never re-raise
        pass


def _run_calibration(
    ring: EmgAccelRing,
    config_path: str,
    session_name: str,
    stop_event,
    event_conn,
    rate: float,
    seed: int | None,
) -> None:
    """Child-process entry point for Pc (the calibration recorder).

    Runs in its own interpreter. It loads the recipe, records against the ring
    and streams phase/progress/done/error events back to P0 over a one-way
    pipe. Importing ``calibration`` here keeps P0's import graph free of the
    recorder until a run is actually started.
    """
    from calibration.recorder import Pc
    from p0.calibration_config import CalibrationConfig

    try:
        config = CalibrationConfig.load(config_path)
    except Exception as exc:  # noqa: BLE001 -- report, never crash silently
        _send(event_conn, {"kind": "error", "detail": f"Cannot load config: {exc}"})
        ring.close()
        return

    pc = Pc(
        ring,
        config,
        config_path=config_path,
        session_name=session_name,
        event_conn=event_conn,
        stop_event=stop_event,
        seed=seed,
        sampling_rate_hz=rate,
    )
    try:
        pc.run()
    except Exception as exc:  # noqa: BLE001 -- child must not hang the parent
        _send(event_conn, {"kind": "error", "detail": f"{type(exc).__name__}: {exc}"})
    finally:
        # The child only detaches; the parent owns unlink().
        ring.close()


@dataclass
class _CalibrationProcess:
    """Bookkeeping for the spawned Pc child (kept in the parent only)."""

    process: mp.Process
    stop_event: object
    event_conn: object
    session_name: str
    config_path: str
    #: Latest merged run state (updated as events arrive).
    run: CalibrationRun


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

        #: The running calibration child, or ``None`` when idle.
        self._calibration: _CalibrationProcess | None = None
        #: Latest calibration run state, kept between ticks for the snapshot.
        self.calibration_run: CalibrationRun = CalibrationRun()

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

    # ------------------------------------------------- calibration (Pc)
    # === IMPLEMENTED ===
    @property
    def calibrating(self) -> bool:
        """``True`` while a Pc child is alive."""
        return self._calibration is not None and self._calibration.process.is_alive()

    def start_calibration(
        self,
        config_path,
        session_name: str | None = None,
        *,
        seed: int | None = None,
    ) -> bool:
        """Spawn Pc to record against ``emg_ring_0``. Returns success.

        No-op (returns ``False``) if a run is already in progress or if no ring
        exists. The current run state becomes ``active`` immediately so the GUI
        can open its window before the first event arrives.
        """
        if self.calibrating:
            return False
        if self._calibration is not None:
            self._reap_calibration()

        config_path = str(config_path)
        session = (session_name or "").strip()
        stop_event = self.ctx.Event()
        parent_conn, child_conn = self.ctx.Pipe(duplex=False)
        process = self.ctx.Process(
            target=_run_calibration,
            args=(
                self.emg_rings[0],
                config_path,
                session,
                stop_event,
                child_conn,
                self.rate,
                seed,
            ),
            name="Pc:calibration",
            daemon=True,
        )
        process.start()
        child_conn.close()

        self._calibration = _CalibrationProcess(
            process=process,
            stop_event=stop_event,
            event_conn=parent_conn,
            session_name=session,
            config_path=config_path,
            run=CalibrationRun(active=True, phase="intro", session_name=session),
        )
        self.calibration_run = self._calibration.run
        return True

    def stop_calibration(self) -> None:
        """Ask Pc to stop (cancel) and reap it."""
        if self._calibration is None:
            return
        self._calibration.stop_event.set()
        self._reap_calibration()

    def _reap_calibration(self) -> None:
        """Join the Pc child, drain its last events, then drop the handle."""
        if self._calibration is None:
            return
        self.drain_calibration()
        self._calibration.process.join(timeout=5.0)
        self._calibration = None

    def drain_calibration(self) -> CalibrationRun:
        """Drain Pc events (non-blocking) and return the merged run state.

        Called every tick by the controller. Never blocks and never raises; a
        finished child is reaped lazily on the next call. ``done``/``error``
        latch into :attr:`calibration_run` so the GUI keeps the final state
        until it acknowledges (a new run resets it).
        """
        proc = self._calibration
        if proc is None:
            return self.calibration_run

        run = proc.run
        conn = proc.event_conn
        while conn is not None:
            try:
                if not conn.poll():
                    break
                event = conn.recv()
            except (EOFError, OSError):
                conn = None
                break
            if not isinstance(event, dict):
                continue
            kind = event.get("kind")
            if kind == "phase":
                run = CalibrationRun(
                    active=True,
                    phase=event.get("phase", run.phase),
                    label=event.get("label"),
                    image=event.get("image"),
                    repetition=event.get("repetition"),
                    total_repetitions=event.get("total_repetitions"),
                    progress=float(event.get("progress", run.progress)),
                    session_name=proc.session_name,
                )
            elif kind == "done":
                run = CalibrationRun(
                    active=False,
                    phase="done",
                    progress=1.0,
                    session_name=proc.session_name,
                    done=True,
                    output_path=event.get("path"),
                )
            elif kind == "error":
                run = CalibrationRun(
                    active=False,
                    phase=run.phase,
                    progress=run.progress,
                    session_name=proc.session_name,
                    error=str(event.get("detail", "unknown error")),
                )

        proc.run = run
        self.calibration_run = run

        # Reap the child as soon as it has finished and reported.
        if (run.done or run.error is not None) and not proc.process.is_alive():
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            proc.process.join(timeout=0.5)
            self._calibration = None
        return run

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
        self.stop_calibration()
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