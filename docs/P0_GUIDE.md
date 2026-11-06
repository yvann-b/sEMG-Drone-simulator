<div align="center">

# P0 GUI — Step-by-Step Guide

*How the orchestrator window is built, one step at a time.*

</div>

---

## Overview

P0 is the orchestrator process: it owns the GUI, the process lifecycle and the
command routing. It is built incrementally so that every step leaves the
application in a runnable state.

Run it at any point with:

```bash
python -m p0
```

---

## Step 1 — Window skeleton ✅

**Goal:** a window that opens, shows a title, and closes cleanly.

**Files:** `p0/app.py`, `p0/main_window.py`

- `create_app()` creates (or reuses) the single `QApplication` and sets the
  application / organization names.
- `main()` builds the window, shows it, and hands control to `app.exec()`.
- `MainWindow` subclasses `QMainWindow` (menu bar, status bar and central widget
  area come for free) and sets a title, a size and a placeholder label.

**Done when:** the window appears, the title reads *sEMG Drone — P0 Orchestrator*,
the status bar says *Ready*, and closing the window exits the process with code 0.

---

## Step 2 — Layout ✅

**Goal:** the window's structure. No visual polish (colours, icons, themes) —
that waits until the backend produces real values to style.

**Files:** `p0/main_window.py`, `p0/widgets/model_table.py`,
`p0/widgets/status_bar.py`

**Layout:**

```text
  EMG sensor: [Start acquisition]
┌──────────────────────────────┬─────────────────────────────────┐
│                              │  Calibration                    │
│  Models                      │  [Start calibration] [Configure…]│
│  Feeds the simulator: [▾]    │  <plan summary>                 │
│  ┌────────────────────────┐  ├─────────────────────────────────┤
│  │ Model │ Status │ …     │  │  Simulation                     │
│  │ svm   │stopped │ …     │  │  [Start simulator] [Reload]     │
│  │ lda   │stopped │ …     │  │  [Open simulation view]         │
│  │ cnn   │stopped │ …     │  │                                 │
│  └────────────────────────┘  │                                 │
└──────────────────────────────┴─────────────────────────────────┘
  Sensor 1: online        Ps: —
    60 % width                     40 % width
```

| Rectangle | Content |
|:--|:--|
| **Top bar — no rectangle** | *Start acquisition*: the one global switch. Acquisition is the root of the data plane (the ring exists only while it runs, every model reads from it), so it belongs to no single workflow |
| **Left, full height (60 %)** | `QComboBox` choosing which model feeds Ps, above the model table |
| **Top-right (40 % of the height)** | *Start calibration* · *Configure calibration…* + a summary of the planned run |
| **Bottom-right (60 % of the height)** | *Start simulator* · *Reload* · *Open simulation view* |
| **Status bar** | Message area + *permanent* indicator(s): one **`Sensor <i>:`** chip per Shimmer (colour-coded health) and **Ps** state, so `showMessage` cannot overwrite them. *(Ring fill and P1 rate were later dropped — see step 5.)* |

**No *Train* button.** A model trains itself on spawn when it was given a
dataset, so training is part of *starting* the model, not a separate action
(`ARCHITECTURE.md` §3, Pm_i). Spawning it is already the train action.

**Why splitters.** The two divisions are real `QSplitter`s, so 60/40 and 40/60
are only the *initial* geometry — the user can drag the handles. `setSizes()` is
a hint that Qt discards on re-layout, so `_apply_ratios()` re-applies the ratios
from `resizeEvent()`/`showEvent()`; that is what makes them actually hold.

**The handle trap.** Even inside `_apply_ratios()`, size the panes from the *sum
of the current sizes* (`sum(splitter.sizes())`), never from `splitter.width()`.
`width()` includes the 2 px drag handles, so sizing from it over-allocates and Qt
trims the oversized pane: 60/40 silently became 45/55. `_split_by_ratio()` gives
the clean two-value split and lets the second pane absorb the rounding remainder,
so the pair always adds up exactly and nothing jumps on resize.

**Not in this window.** The simulation image is *not* drawn in P0 — `Ps` is its
own process with its own Ursina window, opened by *Open simulation view*.
Likewise the calibration setup (gestures, repetitions, images) lives behind
*Configure calibration…* so the main window stays sober.

**Design rule:** the widgets are dumb. They hold state, render it, and emit
signals — they never touch processes or queues. The toggle buttons flip their own
label and a `*_active` flag; the controller connects to them in step 4.
Dummy data lives in `ModelTable.PLACEHOLDER_ROWS`.

**Done when:** the three rectangles are where they belong at the right ratios,
resize correctly, and every control is clickable.
✅ *Verified offscreen at 1100×700 and 1400×800: columns 59.9/40.1, rows 40/60;
selector and all buttons behave; status indicators update.*

---

## Step 3 — Model table

**Goal:** a real table backed by a Qt model, not hand-filled widgets.

- A `QAbstractTableModel` exposing one row per model, replacing
  `QTableWidget` + `PLACEHOLDER_ROWS`.
- A `QTableView` bound to it, with the status column colour-coded
  (running / training / error / stopped) via `QAbstractTableModel.data()` and
  `ForegroundRole`/`BackgroundRole`.
- The model is fed from a state snapshot; it never talks to the processes
  directly.

**Done when:** rows can be added/updated from code and the view refreshes
without rebuilding the widget tree.

---

## Step 4 — Control loop ✅

**Goal:** a non-blocking loop that drives the processes.

- `p0/controller.py` owns a `QTimer` tick (50 ms).
- Each tick: drain the GUI command queue (`get_nowait`) → handle it; drain the
  backends → build one immutable `SystemSnapshot` → push it to the GUI;
  `_poll_sensor_health()` asks the backend for per-Shimmer states and forwards
  any new child errors on the `error` signal.
- The loop never blocks the UI and the UI never blocks the loop; every queue
  read is `get_nowait`, so a missing message is a normal state, not an error.

**Done when:** the GUI stays responsive while the timer runs, and commands
posted from buttons reach the controller on the next tick.
✅ *Verified: 20 GUI tests against `FakeBackend`; commands are handled on the
next tick, snapshots reach the window.*

---

## Step 5 — P0 ↔ P1 wiring ✅

**Goal:** the GUI actually launches acquisition, owns the rings, and shows
sensor health — not just mock rows.

**Files:** `p0/backend.py` (`DeviceBackend`), `p0/app.py`, `p0/controller.py`,
`p0/widgets/status_bar.py`, `p0/state.py`,
`p1_device_acquisition/shimmer_acquisition.py`.

- `DeviceBackend` creates `NUMBER_SHIMMER` ring pairs (`EmgAccelRing` +
  `ImuRing`, `is_creating=True`), keeps every handle alive, and is the only
  place that unlinks them.
- `start_acquisition()` spawns one `ShimmerAcquisition` child per Shimmer;
  `stop_acquisition()` sets a shared `stop_event` and **`join()`s every child
  before** `close()` unlinks the rings (the precedent project's pattern:
  `p.join()` then `buffer.close(); buffer.unlink()`).
- **Sensor health** is per Shimmer: `p1_states()` returns one of
  `offline / connecting / online / error` per child, driven by whether the EMG
  ring's `write_count` advanced between ticks. The status bar paints one
  coloured `Sensor <i>:` chip per entry; the count comes from the snapshot.
- **A child failure reaches the GUI**: the child sends `{kind, port, detail}`
  over a one-way `mp.Pipe` before exiting; the tick drains it, emits
  `error("Sensor error — …")`, and the chip turns red. A `Pipe` (not a `Queue`)
  is used because `Pipe.send` is synchronous — a `Queue.put` can be lost when
  the child re-raises and exits.
- **Crash resilience**: `_unlink_if_stale()` clears a segment left by a crashed
  previous P0, so relaunch does not fail with `FileExistsError`.
- **Dropped** the ring-fill and P1-rate indicators: internal buffer plumbing,
  not operator-facing.

**Done when:** with a real Shimmer the chip goes `connecting → online`; a dead
port shows `error` (red) instead of a console-only print; and the window adapts
to 1 or 2 Shimmers with no code change.
✅ *Verified: 12 backend tests + offscreen render for 0/1/2 sensors; no SHM
leak after the suite.*

> Full reference: [`P1_WIRING.md`](P1_WIRING.md).

---

## Later steps

- `Pc` calibration and `Pm_i` model processes (`drain_model_queues` /
  `forward` are still stubs).
- Model selection and reload.
- Ps connection state driven by a real simulator process.
