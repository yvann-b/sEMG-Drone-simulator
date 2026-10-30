"""Application bootstrap for P0.

This module is deliberately tiny. Its only job is to create the Qt
application object, create the main window, and hand control to Qt's event
loop. Everything else lives in ``main_window.py`` (widgets) and, later,
``controller.py`` (the control loop).

Why a separate ``app.py``?
    * ``main.py`` at the repo root stays a one-liner entry point.
    * Tests can import ``create_app`` without starting an event loop.
    * The window class never has to know how the application was created.
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from p0.main_window import MainWindow


def create_app(argv: list[str] | None = None) -> QApplication:
    """Create (or return) the single ``QApplication`` instance.

    Qt allows exactly one ``QApplication`` per process. Creating a second one
    raises. We therefore reuse an existing instance if there is one — this
    makes the function safe to call from tests and from ``main()``.
    """
    app = QApplication.instance()
    if app is None:
        app = QApplication(argv if argv is not None else sys.argv)

    # Used by QSettings and by the window manager to group our windows.
    app.setApplicationName("sEMG Drone")
    app.setOrganizationName("ISAE")
    return app


def main(argv: list[str] | None = None) -> int:
    """Entry point: build the app, show the window, run the event loop."""
    app = create_app(argv)

    window = MainWindow()
    window.show()

    # ``exec()`` blocks here until the last window is closed (or quit() is
    # called). Its return value is the process exit code.
    return app.exec()