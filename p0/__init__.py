"""P0 — orchestrator + GUI.

This package is the control plane of the system: it owns the window, the
control loop, the shared-memory ring handle, and the lifecycle of every
worker process. See ``docs/ARCHITECTURE.md`` for the process map.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"