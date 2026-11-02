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

**Goal:** replace the placeholder label with the real widget tree.

**Files:** `p0/main_window.py`, `p0/widgets/model_table.py`,
`p0/widgets/status_bar.py`

**Layout** (see `ARCHITECTURE.md` §7), top to bottom:

1. **Selector** — a `QGroupBox` of radio buttons, one per model, deciding which
   model feeds Ps. The first model is preselected; `selected_model()` /
   `select_model(name)` are the accessors.
2. **Model table** — a read-only `QTableWidget` with the columns *Model ·
   Status · Last label · Confidence · Age*. The status cell is colour-coded;
   the *Last label* column stretches on resize.
3. **Buttons** — *Reload · Start calibration · Train* on the left, *Start
   acquisition · Start simulator* on the right.
4. **Status bar** — a message area plus three *permanent* indicators (ring fill,
   P1 rate, Ps state) so they are not overwritten by `showMessage`.

**Design rule:** the widgets are dumb. They hold state, render it, and emit
signals — they never touch processes or queues. All three toggle buttons flip
their own label and a `*_active` flag; the controller connects to them in step 4.
Dummy data lives in `ModelTable.PLACEHOLDER_ROWS`.

**Done when:** the window shows all four areas with dummy content, resizes
sensibly, and every control is clickable. ✅ *Verified offscreen: 3 rows, 3 radio
buttons, buttons toggle their labels, status indicators update.*

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

## Step 4 — Control loop

**Goal:** a non-blocking loop that drives the processes.

- `p0/controller.py` owns a `QTimer` tick.
- Each tick: read a snapshot of state → push it to the GUI; drain the command
  queue posted by the GUI → dispatch to the processes.
- The loop never blocks the UI and the UI never blocks the loop.

**Done when:** the GUI stays responsive while the timer runs, and commands
posted from buttons reach the controller on the next tick.

---

## Later steps

- Process lifecycle (spawn / stop `P1`, `Pc`, `Pm_i`, `Ps`).
- Model selection and reload.
- Calibration and training actions.
- Ps connection state in the status bar.