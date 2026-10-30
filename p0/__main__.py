"""Allow ``python -m p0`` to launch the orchestrator."""

from p0.app import main

if __name__ == "__main__":
    raise SystemExit(main())