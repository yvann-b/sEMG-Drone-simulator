"""Hardware-free tests for the calibration *configuration* feature (Task 1).

Covers:
* the pure data layer (``CalibrationConfig``): JSON round-trip, derived label
  vocabulary (rest always, noise only when enabled), validation, image paths;
* the gesture table model (add / delete / noise row / preview role);
* the Configure window (fields -> config, save/load round-trip, summary text).

No processes, no rings, no Bluetooth: Qt runs on the ``offscreen`` platform.

Run with:  python -m pytest tests/test_p0_calibration.py -v
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap

from p0.app import build, create_app
from p0.calibration_config import (
    NOISE_LABEL,
    REST_LABEL,
    CalibrationConfig,
    GestureSpec,
    image_exists,
    image_path,
)
from p0.calibration_window import CalibrationWindow
from p0.widgets.gesture_table import (
    COL_IMAGE,
    COL_PREVIEW,
    COL_REPETITIONS,
    GestureTableModel,
)


@pytest.fixture(scope="module")
def qapp():
    return create_app([])


# --------------------------------------------------------------------------- #
# Data layer — CalibrationConfig
# --------------------------------------------------------------------------- #

def test_labels_always_end_with_rest():
    cfg = CalibrationConfig(name="c", gestures=[GestureSpec("fist")])
    assert cfg.labels()[-1] == REST_LABEL
    assert cfg.labels() == ["fist", REST_LABEL]


def test_noise_appears_only_when_enabled():
    gestures = [GestureSpec("fist"), GestureSpec("point")]
    off = CalibrationConfig(name="c", noise_enabled=False, gestures=gestures)
    on = CalibrationConfig(name="c", noise_enabled=True, gestures=gestures)
    assert NOISE_LABEL not in off.labels()
    assert on.labels() == ["fist", "point", NOISE_LABEL, REST_LABEL]


def test_effective_gestures_does_not_duplicate_noise():
    cfg = CalibrationConfig(
        name="c", noise_enabled=True,
        gestures=[GestureSpec("fist"), GestureSpec(NOISE_LABEL)],
    )
    names = [g.name for g in cfg.effective_gestures()]
    assert names.count(NOISE_LABEL) == 1


def test_json_round_trip(tmp_path):
    cfg = CalibrationConfig(
        name="patient_a",
        movement_time_s=2.5,
        rest_time_s=1.5,
        noise_enabled=True,
        gestures=[GestureSpec("fist", "fist.png", 5), GestureSpec("point", None, 3)],
    )
    path = tmp_path / "patient_a.json"
    cfg.save(path)
    loaded = CalibrationConfig.load(path)
    assert loaded.to_dict() == cfg.to_dict()
    assert loaded.labels() == cfg.labels()


def test_from_dict_rejects_newer_version():
    with pytest.raises(ValueError):
        CalibrationConfig.from_dict({"version": 999, "name": "x"})


def test_validate_flags_blocking_problems():
    cfg = CalibrationConfig(
        name="",
        movement_time_s=0,
        rest_time_s=-1,
        gestures=[GestureSpec("", None, 0), GestureSpec("dup"), GestureSpec("dup")],
    )
    problems = cfg.validate()
    joined = " ".join(problems)
    assert "name" in joined.lower()
    assert "Movement time" in joined
    assert "Rest time" in joined
    assert "dup" in joined
    assert not cfg.is_valid()


def test_missing_image_is_only_a_warning():
    cfg = CalibrationConfig(name="c", gestures=[GestureSpec("fist", "does_not_exist_xyz.png")])
    problems = cfg.validate()
    assert any(p.startswith("Warning:") for p in problems)
    # A warning alone does not make the config invalid.
    assert cfg.is_valid()


def test_image_path_resolution():
    assert image_path(None) is None
    assert image_path("") is None
    assert image_path("fist.png").name == "fist.png"
    # A name we know is not there.
    assert image_exists("definitely_missing_xyz.png") is False


# --------------------------------------------------------------------------- #
# Gesture table model
# --------------------------------------------------------------------------- #

def test_model_add_and_remove_rows(qapp):
    model = GestureTableModel()
    model.add_gesture(GestureSpec("fist", None, 5))
    model.add_gesture(GestureSpec("point", None, 3))
    assert model.rowCount() == 2
    assert [g.name for g in model.gestures()] == ["fist", "point"]
    assert model.remove_gesture(0) is True
    assert [g.name for g in model.gestures()] == ["point"]


def test_model_noise_row_is_derived_not_stored(qapp):
    model = GestureTableModel(gestures=[GestureSpec("fist")])
    model.set_noise_enabled(True)
    assert model.rowCount() == 2
    assert model.is_noise_row(1) is True
    # Saving must exclude the derived noise row.
    assert [g.name for g in model.gestures()] == ["fist"]
    # The noise row cannot be deleted as a user row.
    assert model.remove_gesture(1) is False


def test_model_preview_role_returns_pixmap(qapp):
    model = GestureTableModel(gestures=[GestureSpec("fist", None, 1)])
    pixmap = model.data(model.index(0, COL_PREVIEW), Qt.ItemDataRole.DecorationRole)
    assert isinstance(pixmap, QPixmap)
    assert not pixmap.isNull()


def test_model_edit_updates_gesture(qapp):
    model = GestureTableModel(gestures=[GestureSpec("fist", None, 1)])
    changed = []
    model.changed.connect(lambda: changed.append(True))
    ok = model.setData(model.index(0, COL_REPETITIONS), 7, Qt.ItemDataRole.EditRole)
    assert ok
    assert model.gestures()[0].repetitions == 7
    assert changed == [True]

    model.setData(model.index(0, COL_IMAGE), "fist.png", Qt.ItemDataRole.EditRole)
    assert model.gestures()[0].image == "fist.png"


def test_model_repetitions_rejects_non_positive(qapp):
    model = GestureTableModel(gestures=[GestureSpec("fist", None, 5)])
    # setData clamps to >= 1 rather than storing 0/negative.
    model.setData(model.index(0, COL_REPETITIONS), 0, Qt.ItemDataRole.EditRole)
    assert model.gestures()[0].repetitions >= 1


# --------------------------------------------------------------------------- #
# Configure window
# --------------------------------------------------------------------------- #

def test_window_fields_drive_current_config(qapp):
    window = CalibrationWindow()
    window.name_edit.setText("session")
    window.movement_spin.setValue(4.0)
    window.rest_spin.setValue(2.0)
    window.model.add_gesture(GestureSpec("fist", None, 5))
    window.noise_check.setChecked(True)

    cfg = window.current_config()
    assert cfg.name == "session"
    assert cfg.movement_time_s == 4.0
    assert cfg.rest_time_s == 2.0
    assert cfg.noise_enabled is True
    assert cfg.labels() == ["fist", NOISE_LABEL, REST_LABEL]


def test_window_summary_mentions_rest_and_noise(qapp):
    window = CalibrationWindow()
    window.name_edit.setText("x")
    window.model.add_gesture(GestureSpec("fist", None, 5))
    window.noise_check.setChecked(True)
    text = window.summary_label.text()
    assert "fist" in text
    assert NOISE_LABEL in text
    assert REST_LABEL in text


def test_window_save_load_round_trip(qapp, tmp_path):
    window = CalibrationWindow()
    window.name_edit.setText("roundtrip")
    window.model.add_gesture(GestureSpec("fist", "fist.png", 5))
    cfg = window.current_config()

    path = tmp_path / "roundtrip.json"
    cfg.save(path)

    reopened = CalibrationWindow(config=CalibrationConfig.load(path), path=path)
    assert reopened.name_edit.text() == "roundtrip"
    assert [g.name for g in reopened.current_config().gestures] == ["fist"]


def test_window_delete_row(qapp):
    window = CalibrationWindow()
    window.model.add_gesture(GestureSpec("a", None, 1))
    window.model.add_gesture(GestureSpec("b", None, 1))
    window.model.remove_gesture(0)
    assert [g.name for g in window.current_config().gestures] == ["b"]


# --------------------------------------------------------------------------- #
# Main window wiring
# --------------------------------------------------------------------------- #

def test_main_window_has_config_selector(qapp):
    window, controller = build()
    try:
        assert window.config_selector is not None
        # With no recipe selected the pane shows its placeholder hint.
        assert "Configure calibration" in window.calibration_summary.text()
    finally:
        controller.stop()
        window.close()
        window.deleteLater()


def test_main_window_selecting_config_emits_and_stores(qapp):
    from p0.calibration_config import CALIBRATIONS_DIR

    CALIBRATIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = CALIBRATIONS_DIR / "_pytest_wired.json"
    window, controller = build()
    try:
        CalibrationConfig(name="wired", gestures=[GestureSpec("fist")]).save(path)
        # The selector lists calibrations/*.json, so point it at our file.
        window._refresh_config_selector(preferred=path)
        index = window._index_of_path(path)
        assert index >= 0, "selector should list the saved recipe"
        window._on_config_selected(index)
        assert controller.calibration_config_path is not None
        assert str(controller.calibration_config_path).endswith("_pytest_wired.json")
    finally:
        controller.stop()
        window.close()
        window.deleteLater()
        path.unlink(missing_ok=True)



# --------------------------------------------------------------------------- #
# Global repetition count
# --------------------------------------------------------------------------- #

def test_config_default_repetitions_round_trip(tmp_path):
    cfg = CalibrationConfig(
        name="rep", default_repetitions=12,
        gestures=[GestureSpec("fist", None, 12)],
    )
    path = tmp_path / "rep.json"
    cfg.save(path)
    loaded = CalibrationConfig.load(path)
    assert loaded.default_repetitions == 12
    assert loaded.to_dict() == cfg.to_dict()


def test_config_from_dict_old_file_defaults_repetitions():
    # Older files without the field still load (backward compatible).
    cfg = CalibrationConfig.from_dict({"version": 1, "name": "old", "gestures": []})
    assert cfg.default_repetitions == 5


def test_config_validate_flags_bad_default_repetitions():
    cfg = CalibrationConfig(name="c", default_repetitions=0,
                            gestures=[GestureSpec("fist")])
    assert any("repetitions" in p.lower() for p in cfg.validate())
    assert not cfg.is_valid()


def test_model_set_repetitions_all(qapp):
    model = GestureTableModel(
        gestures=[GestureSpec("fist", None, 5), GestureSpec("point", None, 3)]
    )
    model.set_repetitions_all(9)
    assert [g.repetitions for g in model.gestures()] == [9, 9]
    # Clamped to at least 1.
    model.set_repetitions_all(0)
    assert [g.repetitions for g in model.gestures()] == [1, 1]


def test_model_set_repetitions_all_ignores_noise_row(qapp):
    model = GestureTableModel(gestures=[GestureSpec("fist", None, 5)])
    model.set_noise_enabled(True)
    model.set_repetitions_all(7)
    # The user row changes; the derived noise row is not a user row.
    assert model.gestures()[0].repetitions == 7
    assert [g.name for g in model.gestures()] == ["fist"]


def test_window_global_repetitions_bulk_applies(qapp):
    window = CalibrationWindow()
    window.model.add_gesture(GestureSpec("fist", None, 5))
    window.model.add_gesture(GestureSpec("point", None, 5))
    assert window.repetitions_spin.value() == 5
    window.repetitions_spin.setValue(8)
    assert [g.repetitions for g in window.current_config().gestures] == [8, 8]
    assert window.current_config().default_repetitions == 8


def test_window_loads_default_repetitions_into_spin(qapp, tmp_path):
    cfg = CalibrationConfig(
        name="seeded", default_repetitions=11,
        gestures=[GestureSpec("fist", None, 11)],
    )
    path = tmp_path / "seeded.json"
    cfg.save(path)
    window = CalibrationWindow(config=CalibrationConfig.load(path), path=path)
    assert window.repetitions_spin.value() == 11


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
