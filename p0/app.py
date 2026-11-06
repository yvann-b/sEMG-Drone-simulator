"""Application bootstrap for P0.

Its only job is to create the Qt application, create the window, create the
control loop, and hand control to Qt's event loop. Everything else lives in
``main_window.py`` (widgets), ``controller.py`` (the loop) and ``state.py``
(the contract between them).

Why a separate ``app.py``?
    * ``main.py`` at the repo root stays a one-liner entry point.
    * Tests can import ``create_app`` / ``build`` without starting an event loop.
    * The window class never has to know how the application was created.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from p0.controller import Command, Controller, FakeBackend
from p0.main_window import MainWindow

#: How many Shimmer sensors / rings the system is built for. A constant, not a
#: config file (see ``docs/ARCHITECTURE.md`` §8, decision 17). The rings
#: themselves are created in a later step; here it only names the rings.
NUMBER_SHIMMER = 1


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


def build(window: MainWindow | None = None) -> tuple[MainWindow, Controller]:
    """Create the window + controller and wire them.

    The wiring has exactly two directions:

        window.command_requested ─▶ controller.submit(...)
        controller.snapshot_ready ─▶ window.apply_snapshot(...)

    Neither object knows about the other's internals. The controller runs on a
    ``FakeBackend`` for now, so the window is fully usable before any worker
    process exists.
    """
    window = window or MainWindow()
    backend = FakeBackend(window.models.row_names())
    controller = Controller(backend, parent=window)

    window.command_requested.connect(
        lambda action, active: controller.submit(Command(action=action))
    )
    window.model_selected.connect(controller.select_model)
    controller.snapshot_ready.connect(window.apply_snapshot)
    controller.error.connect(lambda message: window.status_bar.showMessage(message, 5000))

    # The selector's initial value, so the loop starts on the same model the
    # user sees selected.
    controller.select_model(window.selected_model())
    return window, controller


def main(argv: list[str] | None = None) -> int:
    """Entry point: build the app, show the window, run the event loop."""
    app = create_app(argv)

    window, controller = build()
    window.show()

    # Start the loop once the event loop is running: a QTimer needs a running
    # event loop to fire.
    QTimer.singleShot(0, controller.start)
    app.aboutToQuit.connect(controller.stop)

    # ``exec()`` blocks here until the last window is closed (or quit() is
    # called). Its return value is the process exit code.
    return app.exec()
