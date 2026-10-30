"""The P0 main window.

Step 1 of the GUI: a window that opens, shows a title, and closes cleanly.
Layout, the model table and the control loop are added in later steps.
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QMainWindow


class MainWindow(QMainWindow):
    """Top-level window of the orchestrator.

    ``QMainWindow`` (rather than plain ``QWidget``) is used because it already
    provides the pieces a real application needs: a menu bar, a status bar and
    a central widget area. We fill those in as we go.
    """

    def __init__(self) -> None:
        super().__init__()

        self.setWindowTitle("sEMG Drone — P0 Orchestrator")
        self.resize(900, 600)

        # The central widget is the main content area of a QMainWindow.
        # For now it is a single placeholder label.
        placeholder = QLabel("P0 is running.\n\nGUI layout comes next.")
        placeholder.setStyleSheet("font-size: 16px; padding: 24px;")
        self.setCentralWidget(placeholder)

        self.statusBar().showMessage("Ready")