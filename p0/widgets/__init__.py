"""Custom widgets for the P0 main window.

Each widget owns one area of the layout described in ARCHITECTURE.md (GUI).
They are deliberately dumb: they render state and emit signals. They never
read the processes, the ring or the queues - that is the controller's job.

ModelTableModel holds the rows and ModelView paints them; the split is
deliberate (step 3). StatusBar is a single widget with three indicators.

The gesture editor (GestureTableModel + GestureView) backs the Configure
calibration window; it lives here so every table in P0 is a model/view pair.
"""

from p0.widgets.model_table import ModelTableModel
from p0.widgets.model_view import ModelView
from p0.widgets.status_bar import StatusBar
from p0.widgets.gesture_table import GestureTableModel
from p0.widgets.gesture_view import GestureView

__all__ = [
    "ModelTableModel",
    "ModelView",
    "StatusBar",
    "GestureTableModel",
    "GestureView",
]
