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

## Step 2 — Layout

**Goal:** replace the placeholder label with the real widget tree.

**Target layout** (see `ARCHITECTURE.md` §7):

| Area | Content |
|:--|:--|
| **Model table** | One row per `Pm_i` — name, status, last label, last confidence, age of last message |
| **Selector** | Radio button or dropdown choosing which model feeds Ps |
| **Buttons** | Reload · Start/Stop calibration · Train · Start/Stop acquisition |
| **Status bar** | Ring fill level · P1 sample rate · Ps connection state |

**Done when:** the window shows all four areas with dummy/empty content and
resizes sensibly.

---

## Step 3 — Model table

**Goal:** a real table backed by a Qt model, not hand-filled widgets.

- A `QAbstractTableModel` exposing one row per model.
- A `QTableView` bound to it, with the status column colour-coded
  (running / training / error / stopped).
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