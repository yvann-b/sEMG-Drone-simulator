<div align="center">

# Project layout

**One folder per process**

</div>

> Companion to [`ARCHITECTURE.md`](ARCHITECTURE.md). That document says *what*
> each process does; this one says *where its code lives*.

---

## 1. Rule

**One top-level folder per process.** A process folder contains everything that
runs *inside* that process and nothing else. Code shared by two or more
processes goes in `common/` — and `common/` must never import from a process
folder (dependencies point **inward**, toward `common/`).

```
process folder  ──imports──▶  common/
process folder  ──imports──▶  common/
common/         ──imports──▶  (nothing of ours)
```

This keeps the process map in `ARCHITECTURE.md` and the folder tree here
**isomorphic**: one box in the diagram = one folder on disk.

---

## 2. Target tree

Folders marked `[P0]` exist today. The rest are created when their process is
written — **do not create empty folders in advance**.

```text
PythonProject/
│
├── main.py                     # entry point: launches P0
├── pyproject.toml              # deps + tool config
│
├── p0/                         # [P0] Orchestrator + GUI
│   ├── __init__.py
│   ├── __main__.py             # `python -m p0`
│   ├── app.py                  # QApplication bootstrap
│   ├── main_window.py          # the window (widgets, layout)
│   ├── controller.py           # control loop: QTimer tick, routing
│   ├── state.py                # snapshot dataclasses shared with the GUI
│   └── widgets/                # custom widgets (model table, status bar)
│       └── __init__.py
│
├── acquisition/                # [P1] Bluetooth sEMG driver → ring
│   ├── __init__.py
│   ├── __main__.py
│   └── driver.py
│
├── calibration/                # [Pc] ring → data/calib_*.npz
│   ├── __init__.py
│   ├── __main__.py
│   └── recorder.py
│
├── models/                     # [Pm_i] one file per model + its weights
│   ├── __init__.py
│   ├── runner.py               # generic Pm_i wrapper (ring → predict → Queue)
│   ├── svm.py                  # a model plugin: fit / load / predict
│   ├── svm.pkl                 # its weights (generated)
│   └── ...
│
├── simulator/                  # [Ps] Ursina 3D drone
│   ├── __init__.py
│   ├── __main__.py
│   └── drone.py
│
├── common/                     # shared by ≥2 processes
│   ├── __init__.py
│   ├── ring/                   # EmgRing (CircularBuffer subclass)
│   │   ├── __init__.py
│   │   └── emg_ring.py
│   ├── ipc/                    # message schemas + queue helpers
│   │   ├── __init__.py
│   │   └── messages.py
│   └── config.py               # YAML loading
│
├── config/
│   └── app.yaml                # label vocabulary, model list, timings
│
├── data/                       # calibration datasets (git-ignored)
├── docs/
│   ├── ARCHITECTURE.md
│   ├── PROJECT_LAYOUT.md       # ← this file
│   └── P0_GUIDE.md             # step-by-step GUI tutorial
└── tests/
```

---

## 3. Why each folder

| Folder | Process | Owns | Never does |
|:--|:--|:--|:--|
| `p0/` | **P0** | Window, control loop, ring handle, spawning | Signal processing, model maths |
| `acquisition/` | **P1** | Bluetooth link, sample timestamps | Know who reads the ring |
| `calibration/` | **Pc** | Recording a labelled dataset | Train anything |
| `models/` | **Pm_i** | `fit` / `load` / `predict` per model | Touch the ring, queues or disk |
| `simulator/` | **Ps** | 3D scene, label → motion | Read the ring |
| `common/` | — | Ring, message schemas, config | Import from a process folder |

---

## 4. Import rules

| From | May import |
|:--|:--|
| `p0/` | `common/`, `models/` (only to read the plugin list) |
| `acquisition/`, `calibration/`, `simulator/` | `common/` |
| `models/` | `common/` (types only) |
| `common/` | stdlib + numpy |

> **No process imports another process.** If two processes need the same code,
> it moves to `common/`. This is what keeps the star topology honest: the only
> runtime links between processes are the **ring** and the **queues**.

---

## 5. Entry points

Every process is runnable on its own, which makes debugging trivial:

```bash
python main.py            # P0 (normal use)
python -m p0              # P0 (same thing)
python -m acquisition     # P1 alone
python -m calibration     # Pc alone
python -m models.runner --model svm --weights models/svm.pkl
python -m simulator       # Ps alone
```

P0 spawns the others with `multiprocessing` using these same module paths, so
"run it by hand" and "run it under P0" are the **same code path**.