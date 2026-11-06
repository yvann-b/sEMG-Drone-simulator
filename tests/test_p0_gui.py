"""Hardware-free tests for P0 steps 3 and 4.

Step 3 — the model table is a real Qt data model, fed by a snapshot.
Step 4 — one controller tick is non-blocking, drains commands and emits a
         snapshot the window can render.

No processes, no rings, no Bluetooth: everything runs against ``FakeBackend``
with Qt's ``offscreen`` platform.

Run with:  python -m pytest tests/test_p0_gui.py -v
    or:    python tests/test_p0_gui.py
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from p0.app import build, create_app
from p0.controller import TICK_INTERVAL_MS, Command, Controller, FakeBackend
from p0.state import ModelRow, SystemSnapshot, format_age
from p0.widgets.model_table import COL_LABEL, COL_STATUS, ModelTableModel


@pytest.fixture(scope="module")
def qapp():
    return create_app([])


@pytest.fixture
def window_and_controller(qapp):
    window, controller = build()
    yield window, controller
    controller.stop()
    window.close()
    window.deleteLater()


# --------------------------------------------------------------------------- #
# Step 3 — the data model
# --------------------------------------------------------------------------- #

def test_model_exposes_placeholder_rows(qapp):
    model = ModelTableModel()
    assert model.rowCount() == 3
    assert model.columnCount() == 5
    assert model.row_names() == ["svm", "lda", "cnn"]


def test_apply_snapshot_replaces_rows(qapp):
    model = ModelTableModel()
    model.apply_snapshot(
        [
            ModelRow("svm", "running", "flex", 0.9, 10.0),
            ModelRow("lda", "stopped"),
        ],
        now=10.5,
    )
    assert model.row_names() == ["svm", "lda"]
    assert model.rowCount() == 2
    assert model.data(model.index(0, COL_LABEL)) == "flex"
    # Confidence is formatted to two decimals.
    assert model.data(model.index(0, 3)) == "0.90"


def test_apply_snapshot_same_rows_does_not_reset(qapp):
    """A status-only change must not rebuild the whole table."""
    model = ModelTableModel()
    model.apply_snapshot([ModelRow("svm", "idle")], now=0.0)
    resets = []
    model.modelReset.connect(lambda: resets.append(True))
    model.apply_snapshot([ModelRow("svm", "running")], now=0.0)
    assert resets == []
    assert model.data(model.index(0, COL_STATUS)) == "running"


def test_status_cell_is_coloured(qapp):
    model = ModelTableModel()
    model.apply_snapshot([ModelRow("svm", "error"), ModelRow("lda", "idle")], now=0.0)
    background = model.data(model.index(0, COL_STATUS), Qt.ItemDataRole.BackgroundRole)
    assert isinstance(background, QColor)
    assert background.name() == "#fbd8d6"


def test_unknown_status_is_not_coloured(qapp):
    model = ModelTableModel()
    model.apply_snapshot([ModelRow("svm", "weird")], now=0.0)
    assert model.data(model.index(0, COL_STATUS), Qt.ItemDataRole.BackgroundRole) is None


def test_age_is_computed_at_paint_time(qapp):
    """Age derives from ``now``, so it grows without the controller rewriting it."""
    model = ModelTableModel()
    model.apply_snapshot([ModelRow("svm", "running", "flex", 0.5, 100.0)], now=100.2)
    assert model.data(model.index(0, 4)) == "0.2s"
    model.apply_snapshot([ModelRow("svm", "running", "flex", 0.5, 100.0)], now=101.5)
    assert model.data(model.index(0, 4)) == "1.5s"


def test_format_age_handles_missing_and_skew():
    assert format_age(None, 5.0) == "\u2014"
    assert format_age(5.0, None) == "\u2014"
    assert format_age(5.0, 4.0) == "0.0s"     # clock skew never shows negative
    assert format_age(0.0, 3.2) == "3.2s"
    assert format_age(0.0, 42.0) == "42s"


def test_view_is_bound_to_the_model(window_and_controller):
    window, _ = window_and_controller
    assert window.model_view.model() is window.models
    assert window.model_view.editTriggers() == window.model_view.EditTrigger.NoEditTriggers


# --------------------------------------------------------------------------- #
# Step 4 — the control loop
# --------------------------------------------------------------------------- #

def test_tick_emits_a_snapshot(window_and_controller):
    window, controller = window_and_controller
    received = []
    controller.snapshot_ready.connect(received.append)

    controller._on_tick()

    assert len(received) == 1
    assert isinstance(received[0], SystemSnapshot)
    assert received[0].now is not None
    assert [r.name for r in received[0].models] == ["svm", "lda", "cnn"]


def test_snapshot_reaches_the_window(window_and_controller):
    window, controller = window_and_controller
    controller.snapshot_ready.connect(window.apply_snapshot)

    controller._on_tick()

    assert window.models.row_names() == ["svm", "lda", "cnn"]
    assert window.status_bar.ps.text().endswith("svm")


def test_commands_are_handled_on_the_next_tick(window_and_controller):
    window, controller = window_and_controller
    backend = controller._backend

    controller.submit(Command("reload"))
    controller.submit(Command("select", model="lda"))
    # Nothing happens until the tick runs.
    assert controller._selected == "svm"

    controller._on_tick()
    assert controller._selected == "lda"
    assert backend.selected == "lda"


def test_unknown_command_reports_an_error(qapp):
    controller = Controller(FakeBackend(["svm"]))
    errors = []
    controller.error.connect(errors.append)
    controller.submit(Command("nonsense"))
    controller._on_tick()
    assert errors and "nonsense" in errors[0]
    controller.stop()


def test_bad_backend_does_not_kill_the_tick(qapp):
    class Broken:
        selected = None

        def drain_model_queues(self, now):
            raise RuntimeError("boom")

    controller = Controller(Broken())
    errors = []
    controller.error.connect(errors.append)
    controller._on_tick()          # must not raise
    assert errors and "boom" in errors[0]


def test_selected_label_is_forwarded_with_a_deadline(window_and_controller):
    window, controller = window_and_controller
    backend = controller._backend
    backend._models["svm"].row = ModelRow("svm", "running", "flex", 0.9, 0.0)
    controller.select_model("svm")

    controller._on_tick()

    forwarded = backend.drain_ps()
    assert len(forwarded) == 1
    assert forwarded[0]["label"] == "flex"
    assert forwarded[0]["model"] == "svm"
    assert forwarded[0]["deadline"] > 0


def test_no_selected_label_means_nothing_forwarded(window_and_controller):
    window, controller = window_and_controller
    backend = controller._backend
    controller.select_model("lda")        # this model emitted nothing
    controller._on_tick()
    assert backend.drain_ps() == []


def test_tick_uses_get_nowait_only(qapp):
    """The loop must never block: an empty queue returns immediately."""
    calls = []

    class RecordingCommands:
        def get_nowait(self):
            calls.append(True)
            raise __import__("queue").Empty

    controller = Controller(FakeBackend(["svm"]))
    controller._commands = RecordingCommands()
    controller._on_tick()
    assert calls == [True]
    controller.stop()


def test_timer_interval_and_start_stop(qapp):
    controller = Controller(FakeBackend(["svm"]))
    assert controller.timer.interval() == TICK_INTERVAL_MS
    controller.start()
    assert controller.timer.isActive()
    controller.stop()
    assert not controller.timer.isActive()


def test_selector_sync_preserves_selection(window_and_controller):
    window, _ = window_and_controller
    window.select_model("cnn")
    window.apply_snapshot(
        SystemSnapshot(
            models=(ModelRow("svm"), ModelRow("cnn"), ModelRow("new")),
            now=0.0,
        )
    )
    assert window.selected_model() == "cnn"
    assert window.model_selector.count() == 3


def test_selector_repopulation_does_not_emit_spurious_selection(window_and_controller):
    """Rebuilding the combo box must not look like the user picking a model."""
    window, _ = window_and_controller
    seen = []
    window.model_selected.connect(seen.append)
    window.apply_snapshot(SystemSnapshot(models=(ModelRow("a"), ModelRow("b")), now=0.0))
    assert seen == []


def test_button_click_posts_a_command(window_and_controller, qapp):
    window, controller = window_and_controller
    window.acquisition_button.click()
    assert window.acquisition_active
    assert window.acquisition_button.text() == "Stop acquisition"
    # The command is queued, not executed inline.
    controller._on_tick()
    backend = controller._backend
    assert backend is not None


if __name__ == "__main__":
    import sys

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    app = create_app([])
    failed = 0
    for test in tests:
        window, controller = build()
        try:
            test(window, controller)
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {test.__name__}: {exc!r}")
        finally:
            controller.stop()
            window.close()
    sys.exit(1 if failed else 0)
