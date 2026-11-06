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
  Ring: —        P1 rate: —        Ps: —
    ~60 % width                   ~40 % width
```

| Rectangle | Content |
|:--|:--|
| **Top bar — no rectangle** | *Start acquisition*: the one global switch. Acquisition is the root of the data plane (the rings exist only while it runs, every model reads from them), so it belongs to no single workflow |
| **Left, full height (~60 %)** | `QComboBox` choosing which model feeds Ps, above the model table |
| **Top-right (~40 % of the height)** | *Start calibration* · *Configure calibration…* + a summary of the planned run |
| **Bottom-right (~60 % of the height)** | *Start simulator* · *Reload* · *Open simulation view* |
| **Status bar** | Message area + three *permanent* indicators (ring fill, P1 rate, Ps state), so `showMessage` cannot overwrite them |

**No *Train* button.** A model trains itself on spawn when it was given a
dataset, so training is part of *starting* the model, not a separate action
(`ARCHITECTURE.md` §3, Pm_i). Spawning it is already the train action.

**Why splitters.** The two divisions are real `QSplitter`s, so 60/40 and 40/60
are only the *initial* geometry — the user can drag the handles. These ratios
are a **convenience, not a hard constraint**: nothing depends on them, so a bit
of drift (e.g. 60/40 settling at 45/55) is acceptable. `setSizes()` is a hint
that Qt discards on re-layout, so `_apply_ratios()` re-applies the ratios from
`resizeEvent()`/`showEvent()` to keep the initial look stable.

**The handle trap** (cosmetic only). Even inside `_apply_ratios()`, size the
panes from the *sum of the current sizes* (`sum(splitter.sizes())`), never from
`splitter.width()`. `width()` includes the 2 px drag handles, so sizing from it
over-allocates and Qt trims the oversized pane. `_split_by_ratio()` gives the
clean two-value split and lets the second pane absorb the rounding remainder, so
the pair always adds up exactly and nothing jumps on resize.

**Not in this window.** The simulation image is *not* drawn in P0 — `Ps` is its
own process with its own Ursina window, opened by *Open simulation view*.
Likewise the calibration setup (gestures, repetitions, images) lives behind
*Configure calibration…* so the main window stays sober.

**Design rule:** the widgets are dumb. They hold state, render it, and emit
signals — they never touch processes or queues. The toggle buttons flip their own
label and a `*_active` flag; the controller connects to them in step 4.
Dummy data lives in `ModelTable.PLACEHOLDER_ROWS`.

**Done when:** the three rectangles are visible side by side, resize sanely, and
every control is clickable.

---

## Step 3 — Model table ✅

**Goal:** split *"what is true about the models"* (a data model) from *"what is
painted"* (the view). This is **not** about making the table editable: editing
stays disabled, and rows are only ever changed by the controller.

**Files:** `p0/state.py`, `p0/widgets/model_table.py`,
`p0/widgets/model_view.py`, `p0/main_window.py`

`QTableWidget` made the widget *be* the database: the controller would have had
to find a row and write into cells. Now:

- **`p0/state.py`** holds the contract — `ModelRow` (frozen dataclass: `name`,
  `status`, `last_label`, `last_confidence`, `last_seen`) and `SystemSnapshot`
  (models + ring fill + P1 rate + Ps state + `now`). Both the GUI and the
  controller import it; it imports neither.
- **`ModelTableModel(QAbstractTableModel)`** holds the rows.
  `apply_snapshot()` replaces them; `row_names()` returns real data for the
  combo box.
- **`ModelView(QTableView)`** is display configuration only — no editing, row
  selection, zebra striping, one `Stretch` column.

**`data()` is asked on demand.** That is why *Age* is computed from
`snapshot.now` at paint time (`format_age`) instead of being stored: a stalled
model's age keeps growing with no extra work from the controller.

**Minimal repaints.** If the set of names is unchanged, `apply_snapshot` emits
one `dataChanged` over the affected range instead of resetting the model; a
status-only change recolours without rebuilding the table.

**Done when:** `window.models.apply_snapshot([...])` updates the table from code
without touching the view, and `QTableWidget` / `QTableWidgetItem` are gone from
`p0/`. ✅ Covered by `tests/test_p0_gui.py`.

---

## Step 4 — Control loop ✅

**Goal:** a non-blocking loop that drives the processes.

**Files:** `p0/controller.py`, `p0/app.py`, `p0/main_window.py`

`Controller(QObject)` owns a `QTimer` (`TICK_INTERVAL_MS = 50`). Each tick:

1. drain the GUI → controller command queue (`get_nowait`),
2. drain every `Pm_i` queue (`get_nowait`),
3. forward the selected model's label to Ps with a deadline
   (`COMMAND_TIMEOUT_S`),
4. build **one immutable `SystemSnapshot`** and emit `snapshot_ready`.

**The seam.** `app.build()` wires exactly two directions:

```text
window.command_requested → controller.submit(Command(...))
controller.snapshot_ready → window.apply_snapshot(...)
```

Neither object knows the other's internals; the window never calls the backend
and the backend never touches a widget.

**No blocking calls in the tick.** Every read is `get_nowait`; an empty queue is
a normal state. A backend exception is caught and surfaced on `error`, so the
loop survives and the next tick still runs.

**`FakeBackend`** lets step 4 run with **no processes at all**, so the GUI is
fully usable before P1/Pc/Pm/Ps exist. It is the single class the real spawner
will replace.

**Button feedback.** A click flips the button label immediately (so it feels
responsive) *and* posts a command; the controller acts on the next tick.

**Done when:** the GUI stays responsive while the timer runs, and commands
posted from buttons reach the controller on the next tick. ✅

---

## Later steps

- Ring ownership: P0 creates `NUMBER_SHIMMER` `EmgAccelRing`s and keeps the
  handles alive for the session.
- Process lifecycle (spawn / stop `P1`, `Pc`, `Pm_i`, `Ps`).
- Model selection and reload.
- Calibration and training actions.
- Ps connection state in the status bar.
