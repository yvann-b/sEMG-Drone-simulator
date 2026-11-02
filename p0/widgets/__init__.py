"""Custom widgets for the P0 main window.

Each widget owns one area of the layout described in ``docs/ARCHITECTURE.md``
§7. They are deliberately dumb: they render state and emit signals. They never
read the processes, the ring or the queues — that is the controller's job.
"""

from p0.widgets.model_table import ModelTable
from p0.widgets.status_bar import StatusBar

__all__ = ["ModelTable", "StatusBar"]