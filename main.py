"""Entry point for the sEMG → drone project.

Running this file launches **P0**, the orchestrator + GUI. Every other
process (acquisition, calibration, models, simulator) is spawned by P0.

    python main.py

See ``docs/ARCHITECTURE.md`` for the process map and
``docs/PROJECT_LAYOUT.md`` for the folder structure.
"""

from p0.app import main

if __name__ == "__main__":
    raise SystemExit(main())
