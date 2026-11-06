<div align="center">

# P0 ↔ P1 wiring

**How the orchestrator owns the rings, spawns acquisition, and reports sensor health**

</div>

> Companion to [`ARCHITECTURE.md`](ARCHITECTURE.md) §3 (P1) and §4.1 (rings).
> This document fixes the **concrete wiring implemented today** between P0 and
> P1 (`DeviceBackend`), which is the step that turns the GUI from a mockup into
> a real launcher. It covers three things:
>
> 1. **Ring ownership** — who creates, who detaches, who unlinks.
> 2. **P1 lifecycle** — spawn / stop, and why a child must be `join()`ed before
>    the rings are unlinked.
> 3. **Sensor health** — the per-Shimmer `offline / connecting / online / error`
>    state shown in the status bar, and how a child's failure crosses the
>    process boundary to reach the GUI.

---

## 1. The seam: `DeviceBackend`

The controller (P0's tick) never touches `multiprocessing` directly. It talks to
**one object** behind a tiny contract, and two implementations satisfy it:

| Implementation | Where | Spawns? | Used by |
|:--|:--|:--|:--|
| `FakeBackend` | `p0/controller.py` | No | GUI tests, and `build()`'s default, so the window runs with **no rings, no processes, no Bluetooth** |
| `DeviceBackend` | `p0/backend.py` | Yes (rings + P1) | The real launch path, `main()` |

The controller reaches optional backend capabilities through `getattr`, so a
bare `FakeBackend` still works and the tick never has to branch on type:

```python
states_fn = getattr(self._backend, "p1_states", None)
if callable(states_fn):
    return tuple(states_fn())
return ()
```

The contract the controller relies on:

| Method | Direction | Purpose |
|:--|:--|:--|
| `drain_model_queues(now)` | P0 ⇒ backend | Rows for the model table |
| `forward(label, deadline)` | P0 ⇒ backend | Ps command (stub today) |
| `selected` | backend ⇒ P0 | Which model feeds Ps |
| `p1_states()` | backend ⇒ P0 | Per-Shimmer health (new) |
| `drain_events()` | backend ⇒ P0 | One-shot child error strings (new) |
| `start_acquisition()` / `stop_acquisition()` | P0 ⇒ backend | P1 lifecycle |
| `close()` | P0 ⇒ backend | Stop P1, then unlink the rings |

---

## 2. Ring ownership

> **Rule:** **P0 creates the rings once, keeps every handle alive for the whole
> session, and is the only place that unlinks them.** This mirrors the precedent
> project's pattern: buffers are created in the launcher and torn down at the
> very end, after every child has exited.

`DeviceBackend.__init__` builds `NUMBER_SHIMMER` ring pairs
(`EmgAccelRing` + `ImuRing`, each `is_creating=True`) and keeps them:

```python
for index in range(self.number_shimmer):
    emg_name = f"emg_ring_{index}"
    imu_name = f"imu_ring_{index}"
    _unlink_if_stale(emg_name)      # see §5
    _unlink_if_stale(imu_name)
    self.emg_rings.append(EmgAccelRing(emg_name, ring_capacity, is_creating=True))
    self.imu_rings.append(ImuRing(imu_name, ring_capacity, is_creating=True))
```

The children receive the ring objects **pickled at spawn**. `CircularBuffer`
defines `__getstate__` / `__setstate__` so the shared-memory handle and the
shared `write_count` travel intact; the child re-attaches to the same segment.
That mechanism is already exercised by `tests/test_shimmer_ring.py`
(`test_multiprocess_pickle`).

### Lifecycle order (the part that must not be reordered)

```
P0:  create rings (is_creating=True, kept alive)
 └─ spawn P1 children  ── pickled rings ──▶  children attach, write samples
 └─ … session runs … ──
P0:  stop_acquisition():  stop_event.set() for every child
 └─ join() every child        ← children detach (ring.close()), still mapped by parent
 └─ close():  ring.close(); ring.unlink()   ← SAFE: no child maps it any more
```

**Why join before unlink.** A child still holding the segment would have its
mapping invalidated the moment the parent unlinks, and on a crash the segment
would be left behind. Joining first guarantees the child is gone before the
memory is reclaimed — exactly the precedent project's:

```python
for p in liste_process:  p.join()
for buffer in liste_buffer:  buffer.close();  buffer.unlink()
```

---

## 3. P1 lifecycle

### Spawn

`start_acquisition()` is a no-op if already acquiring; otherwise it spawns one
child per Shimmer running `p1_device_acquisition.shimmer_acquisition.ShimmerAcquisition`:

```python
stop_event  = self.ctx.Event()
parent_conn, child_conn = self.ctx.Pipe(duplex=False)   # error channel (§4)
process = self.ctx.Process(
    target=_run_p1,
    args=(port, emg_ring, imu_ring, stop_event, rate, calibrate, child_conn),
    name=f"P1:{port}", daemon=True,
)
process.start()
child_conn.close()   # parent never writes
```

P1 imports its Bluetooth stack **inside** `_run_p1`, not at module import time,
so P0's import graph stays free of `pyshimmer` and no device library is loaded
before a child actually starts.

### Stop

```python
def stop_acquisition(self):
    for proc in self._shimmer_procs:
        proc.stop_event.set()     # ask every child to stop
    self._reap()                  # join() every child, then drop handles

def _reap(self):
    for proc in self._shimmer_procs:
        proc.process.join()       # block until it has really exited
        self.drain_events()       # catch any final error it reported
    self._shimmer_procs = []
```

P1 polls `self._should_stop()` (which reads the shared `stop_event`) every
50 ms once streaming, so a healthy child exits promptly after `set()`.

> **Known edge — a wedged connect.** `stop_event` is only checked *after*
> `Serial(port)` opens and `_prepare()` succeeds. A child stuck opening the port
> or reading calibration over the air never reaches that check, so a plain
> `join()` could block. This is acceptable **today** because
> `stop_acquisition()` is called at application shutdown (like the precedent's
> end-of-main teardown), not from a per-frame GUI action. If a future *Stop
> acquisition* button calls it mid-session, move the call off the GUI thread or
> add a bounded `join` + `terminate` fallback.

---

## 4. Sensor health → GUI

### Four states per Shimmer

`DeviceBackend.p1_states()` returns **one entry per child**, in spawn order:

| State | Meaning | Detected by |
|:--|:--|:--|
| `offline` | not started, or the child exited without reporting an error | no child alive |
| `error` | the child exited **and** reported a failure (e.g. `SerialException`) | port present in `_errors_by_port` |
| `connecting` | alive, but no samples have arrived yet (opening port / calibration) | alive, `write_count` unchanged |
| `online` | really streaming samples | alive, `write_count` advanced since last poll |

The `online` test is **"`write_count` advanced between ticks"**, which is
transport-agnostic: it does not care whether the data came over Bluetooth, a
file, or a simulator. `p1_state()` remains as an **aggregate** for non-GUI
callers (`online` > `error` > `connecting` > `offline`).

### How a child error reaches the GUI

A child failure happens in **another interpreter**, so it cannot call the GUI
directly. The path is:

```
child:  ShimmerAcquisition.run() raises
        ├─ print("[P0 child P1:COMx] acquisition failed: …")   (console)
        └─ event_conn.send({"kind": "error", "port": …, "detail": …})   (pipe)
P0 tick: drain_events()  ── recv() ──▶  self._errors_by_port[port] = detail
         controller.error emit("Sensor error -- …")      ──▶ status-bar message
         p1_states() sees port in _errors_by_port         ──▶ "error" (red chip)
```

> **Why a `Pipe` and not an `mp.Queue`.** `Queue.put` only hands the object to a
> background feeder thread; if the child then exits (it re-raises), the process
> can tear down before the feeder flushes and the message is **silently lost** —
> a well-known multiprocessing pitfall. `Pipe.send` writes synchronously to the
> OS pipe, so the message is on the wire before the child dies.

Messages are **one-shot**: once drained, the pipe is empty. To keep a dead
Shimmer showing `error` on every subsequent tick (not just the tick it died on),
`drain_events()` records the detail in `_errors_by_port`, which `p1_states()`
consults. A fresh `start_acquisition()` clears it.

---

## 5. Crash resilience — stale shared memory

If P0 dies without running `close()` (killed terminal, uncaught error), the SHM
segments survive and the next launch would fail with:

```
FileExistsError: [WinError 183] File exists: 'emg_ring_0'
```

`DeviceBackend.__init__` calls `_unlink_if_stale(name)` before creating each
ring. This is safe **because P0 is the sole owner** of the rings: if a segment
with our name already exists at startup, it is a leftover from a previous
crashed P0 — never a live one (a live owner would still hold the segment and a
second `python -m p0` would not be the intended state).

> **If you still see `FileExistsError`:** a *live* process is holding the
> segment — most often an old `python -m p0` left running in another terminal.
> Stop it; the stale-unlink only clears dead segments.

---

## 6. `spawn` and the `__main__` guard

P1 children use the Windows `spawn` start method, which **re-imports the main
module in every child**. Any backend construction at module top level is
therefore executed *again* in the child and will try to recreate the rings:

```python
# __main__.py — correct
if __name__ == "__main__":
    raise SystemExit(main())
```

`python -m p0` is safe because `p0/__main__.py` is guarded. Any **new**
launcher script (a benchmark, a one-off test harness) must guard its top level
the same way, or each child will crash with `FileExistsError` before reaching
its own logic.

---

## 7. What is still a stub

`DeviceBackend` implements ring ownership, P1 lifecycle, and sensor health for
real. The **control-plane** methods the controller calls every tick are
clearly-marked stubs, inert until their processes exist:

| Method | Status | Returns |
|:--|:--|:--|
| `drain_model_queues(now)` | NOT IMPLEMENTED | `[]` (no `Pm_i` yet) |
| `forward(label, deadline)` | NOT IMPLEMENTED | buffers to `_pending_ps` |
| `drain_ps()` | NOT IMPLEMENTED | drains `_pending_ps` |
| `select(name)` / `selected` | implemented (trivial) | remembers the selection |

---

## 8. Tests

| File | Covers |
|:--|:--|
| `tests/test_backend.py` | ring creation per Shimmer, port padding, `p1_states` three-state logic (no spawn), one entry per Shimmer, stale-unlink on relaunch, `close()` unlinks so rings recreate, context-manager teardown, status-bar `set_sensors` adapting to count |
| `tests/test_p0_gui.py` | The whole window against `FakeBackend` — snapshot → table/status bar, command → queue, selector sync |

All are hardware-free: they either use `FakeBackend`, drive `p1_states` with a
fake process + hand-bumped `write_count`, or assert on ring creation only.
