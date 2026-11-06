"""The calibration *run* window: the guided recording the user follows.

When a recording is running, this window tells the user exactly what to do:
which gesture to perform (with its image), when to rest, which repetition they
are on, and how far through the session they are. It is **dumb** like the rest
of the GUI: it never talks to Pc. The controller pushes a
:class:`~p0.state.CalibrationRun` into :meth:`apply_run` every tick, and the
window is a pure function of that state.

Lifecycle
---------
* P0's *Start calibration* button opens this window and starts the run.
* While ``run.active`` the window shows the cue + progress.
* When ``run.done`` it shows a success summary and the ``.npz`` path.
* When ``run.error`` it shows the error.
* Closing the window at any time aborts the run: ``closeEvent`` emits
  :attr:`cancelled` so P0 stops Pc. There is no separate Cancel button.

Design
------
This window deliberately does **not** impose its own palette, background or
font: it inherits the same theme as the main window and the Configure-
calibration window, so the whole application looks like one thing. The only
accent colours used are the ones the configuration window already uses (muted
``#666`` grey for hints, ``#b42318`` red for errors).

The window has no image of its own: gesture previews are resolved from the
recipe's ``image`` (a bare filename) against ``images/`` via
:func:`p0.calibration_config.image_path`. A missing image is shown as a plain
text prompt rather than an empty box.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from p0.calibration_config import image_path
from p0.state import CalibrationRun

#: Pixel size of the gesture preview area (the image is scaled to fit).
IMAGE_BOX = 320


class CalibrationRunWindow(QWidget):
    """Guided recording window (one per run). Fed by ``apply_run``."""

    #: The user asked to cancel the current run (by closing the window).
    cancelled = Signal()
    #: The window was closed (safe to drop the reference).
    closed = Signal()

    def __init__(self, parent=None) -> None:
        # ``Qt.Window`` makes this a real top-level window even though we pass a
        # parent (we do so it inherits the app icon and stays associated with the
        # main window). Without it, a QWidget with a parent is a *child* widget
        # and never opens as its own window.
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Calibration \u2014 follow the cue")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.resize(520, 640)

        self._finished = False

        self.title = QLabel("Get ready\u2026")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setStyleSheet("font-size: 22px; font-weight: 600;")

        self.prompt = QLabel("")
        self.prompt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.prompt.setWordWrap(True)

        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumSize(IMAGE_BOX, IMAGE_BOX)
        self.image.setStyleSheet("border: 1px solid #666; border-radius: 6px;")

        self.repetition = QLabel("")
        self.repetition.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.repetition.setStyleSheet("color: #666;")

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(14)

        self.status = QLabel("")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #666;")

        # Shown only once the run has ended. There is no Cancel button: closing
        # the window at any time aborts the run (see ``closeEvent``).
        self.close_button = QPushButton("Close")
        self.close_button.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addWidget(self.title)
        layout.addWidget(self.prompt)
        layout.addWidget(self.image, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.repetition)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addStretch(1)
        layout.addWidget(self.close_button)

        self.close_button.clicked.connect(self.close)

    # ------------------------------------------------------------------- feed
    def apply_run(self, run: CalibrationRun) -> None:
        """Render one tick of the run state. The only way the window changes."""
        if run.error is not None:
            self._show_error(run.error)
            return
        if run.done:
            self._show_done(run)
            return

        self._finished = False
        self.title.setStyleSheet("font-size: 22px; font-weight: 600;")
        self.close_button.setVisible(False)
        self.status.setText("")
        self.progress.setValue(int(max(0.0, min(1.0, run.progress)) * 1000))

        if run.phase == "intro":
            self.title.setText("Get ready\u2026")
            self.prompt.setText("Calibration will begin in a moment.")
            self._set_image(None)
            self.repetition.setText("")
        elif run.phase == "rest":
            self.title.setText("Rest")
            self.prompt.setText("Relax your hand.")
            self._set_image(None)
            self._set_repetition(run)
        elif run.phase == "gesture":
            self.title.setText(f"Do: {run.label}" if run.label else "Do the gesture")
            self.prompt.setText("Hold the gesture until the window moves on.")
            self._set_image(run.image)
            self._set_repetition(run)
        else:
            self.title.setText("Calibration")
            self.prompt.setText("")

    def _set_repetition(self, run: CalibrationRun) -> None:
        if run.repetition and run.total_repetitions:
            self.repetition.setText(
                f"Repetition {run.repetition} of {run.total_repetitions}"
            )
        else:
            self.repetition.setText("")

    def _set_image(self, filename: str | None) -> None:
        """Load a gesture preview, scaled to the box; clear when ``None``."""
        path = image_path(filename)
        if path is None or not path.is_file():
            self.image.setPixmap(QPixmap())
            self.image.setText("(no image)" if filename else "")
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.image.setText("(image could not be loaded)")
            return
        self.image.setText("")
        self.image.setPixmap(
            pixmap.scaled(
                IMAGE_BOX,
                IMAGE_BOX,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _show_done(self, run: CalibrationRun) -> None:
        self._finished = True
        self.title.setText("Calibration complete")
        self.prompt.setText("Thank you \u2014 the recording was saved.")
        self.repetition.setText("")
        self.progress.setValue(1000)
        if run.output_path:
            self.status.setText(f"Saved to:\n{run.output_path}")
        else:
            self.status.setText("")
        self.close_button.setVisible(True)

    def _show_error(self, message: str) -> None:
        self._finished = True
        self.title.setText("Calibration failed")
        # Same error colour the Configure-calibration window uses, for harmony.
        self.title.setStyleSheet("font-size: 22px; font-weight: 600; color: #b42318;")
        # The common failure is "no sensor data": calibration reads the ring, so
        # if acquisition is not streaming, nothing can be recorded. Say that.
        if "No samples" in message or "is acquisition running" in message:
            self.prompt.setText(
                "No sensor data was received.\n"
                "Start acquisition first (with the Shimmer connected), then "
                "run the calibration again."
            )
        else:
            self.prompt.setText("The recording could not be completed.")
        self.repetition.setText("")
        self.progress.setValue(0)
        self.status.setText(message)
        self.close_button.setVisible(True)

    # ---------------------------------------------------------------- actions
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt's naming
        """Closing an unfinished run cancels it; always signal ``closed``."""
        if not self._finished:
            self.cancelled.emit()
        self.closed.emit()
        super().closeEvent(event)


__all__ = ["CalibrationRunWindow"]
