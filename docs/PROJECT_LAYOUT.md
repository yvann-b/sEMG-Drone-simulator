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
processes goes in `utils/` — and `utils/` must never import from a process
folder (dependencies point **inward**, toward `utils/`).

```
process folder  ──imports──▶  utils/
process folder  ──imports──▶  utils/
utils/          ──imports──▶  (nothing of ours)
```

This keeps the process map in `ARCHITECTURE.md` and the folder tree here
**isomorphic**: one box in the diagram = one folder on disk.

> **Why `utils/` and not `common/`.** The ring layer
> (`circular_buffer.py`, `shimmer_ring.py`, `emg_accel_ring.py`, `imu_ring.py`)
> and its tests already live in `utils/` and are green. The role is the same —
> shared, process-agnostic code — only the folder name differs. Renaming the
> folder and rewriting every import would buy nothing and would churn tested
> code.

---

## 2. Target tree

Folders marked `[EXISTS]` exist today. The rest are created when their process
is written — **do not create empty folders in advance**.

```text
PythonProject/
│
├── main.py                     # entry point: launches P0
├── pyproject.toml              # deps + tool config
│
├── p0/                         # [EXISTS] Orchestrator + GUI
│   ├── __init__.py
│   ├── __main__.py             # `python -m p0`
│   ├── app.py                  # QApplication bootstrap
│   ├── main_window.py          # the window (widgets, layout)
│   ├── controller.py           # control loop: QTimer tick, routing
│   ├── state.py                # snapshot dataclasses shared with the GUI
│   └── widgets/                # custom widgets (model table, status bar)
│       └── __init__.py
│
├── acquisition/                # [P1] Bluetooth sEMG driver → ring(s)
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
├── utils/                      # [EXISTS] shared by ≥2 processes
│   ├── circular_buffer.py      # raw byte ring (unchanged)
│   ├── shimmer_ring.py         # typed layer: dtype → item_size
│   ├── emg_accel_ring.py       # ONE per Shimmer: emg[2] + accel[3]  (28 B)
│   ├── imu_ring.py             # accel / gyro / mag triad             (44 B)
│   └── ipc/                    # message schemas + queue helpers (to add)
│       ├── __init__.py
│       └── messages.py
│
├── data/                       # calibration datasets (git-ignored)
├── docs/
│   ├── ARCHITECTURE.md
│   ├── PROJECT_LAYOUT.md       # ← this file
│   └── P0_GUIDE.md             # step-by-step GUI tutorial
└── tests/
    └── test_shimmer_ring.py    # [EXISTS]
```

> **No `config/` folder.** There is no central config file: each model's label
> vocabulary travels inside its calibration `.npz`, so two models may
> legitimately use different gesture sets. The few remaining constants
> (`NUMBER_SHIMMER`, tick interval, ring capacity) are hardcoded at the top of
> the module that owns them, keeping `python -m p0` runnable with zero setup.

---

## 3. Why each folder

| Folder | Process | Owns | Never does |
|:--|:--|:--|:--|
| `p0/` | **P0** | Window, control loop, ring handles, spawning | Signal processing, model maths |
| `acquisition/` | **P1** | Bluetooth link, sample timestamps, one ring per Shimmer | Know who reads the rings |
| `calibration/` | **Pc** | Recording a labelled dataset | Train anything |
| `models/` | **Pm_i** | `fit` / `load` / `predict` per model | Touch the ring, queues or disk |
| `simulator/` | **Ps** | 3D scene, label → motion | Read the rings |
| `utils/` | — | Rings, message schemas, small constants | Import from a process folder |

---

## 4. Import rules

| From | May import |
|:--|:--|
| `p0/` | `utils/`, `models/` (only to read the plugin list) |
| `acquisition/`, `calibration/`, `simulator/` | `utils/` |
| `models/` | `utils/` (types only) |
| `utils/` | stdlib + numpy |

> **No process imports another process.** If two processes need the same code,
> it moves to `utils/`. This is what keeps the star topology honest: the only
> runtime links between processes are the **rings** and the **queues**.

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
