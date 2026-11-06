"""Hardware-free tests for P1 conversion + timestamp logic.

No Shimmer, no Bluetooth: everything here exercises the pure functions in
``p1_device_acquisition/shimmer_acquisition.py``.

Run with:  python -m pytest tests/test_shimmer_acquisition.py -v
    or:    python tests/test_shimmer_acquisition.py
"""

from __future__ import annotations

import numpy as np

from p1_device_acquisition.shimmer_acquisition import (
    ADC_MAX,
    ADC_REFERENCE_MV,
    DEVICE_CLOCK_HZ,
    DEVICE_TIMESTAMP_MODULO,
    ShimmerClock,
    _as_scalar,
    calibrate_inertial,
    raw_to_mv,
)


# --------------------------------------------------------------------------- #
# raw_to_mv: signed 24-bit ADS1292R counts -> millivolts
# --------------------------------------------------------------------------- #

def test_raw_to_mv_full_scale():
    # Full-scale code at gain 1 maps to the ADC reference voltage exactly.
    assert np.isclose(raw_to_mv(ADC_MAX, 1), ADC_REFERENCE_MV, rtol=1e-9)
    # Zero stays zero.
    assert raw_to_mv(0, 12) == 0.0
    # Higher gain shrinks the input range proportionally.
    assert np.isclose(raw_to_mv(ADC_MAX, 12), ADC_REFERENCE_MV / 12, rtol=1e-9)


# --------------------------------------------------------------------------- #
# calibrate_inertial: alignment @ ((raw - offset) / sensitivity)
# --------------------------------------------------------------------------- #

class _FakeCalibration:
    """Minimal stand-in exposing the three AllCalibration accessors."""

    def __init__(self, offset, sensitivity, ali):
        self._offset = list(offset)
        self._sensitivity = list(sensitivity)
        self._ali = list(ali)

    def get_offset_bias(self, _index):
        return self._offset

    def get_sensitivity(self, _index):
        return self._sensitivity

    def get_ali_mat(self, _index):
        return self._ali

    # Accept any sensor index: the fake holds one triad.
    def __getitem__(self, _index):
        return self


def test_calibrate_identity():
    """Identity alignment, offset 0, sensitivity 1 -> values pass through."""
    calib = _FakeCalibration(offset=[0, 0, 0], sensitivity=[1, 1, 1],
                             ali=[100, 0, 0, 0, 100, 0, 0, 0, 100])
    raw = np.array([123, -456, 789])
    out = calibrate_inertial(raw, calib, 0)
    assert np.allclose(out, raw)


def test_calibrate_offset_and_scale():
    calib = _FakeCalibration(offset=[10, 20, 30], sensitivity=[2, 2, 2],
                             ali=[100, 0, 0, 0, 100, 0, 0, 0, 100])
    raw = np.array([14, 24, 34])
    # ((raw - offset) / sensitivity) = ([4, 4, 4] / 2) = [2, 2, 2]
    out = calibrate_inertial(raw, calib, 0)
    assert np.allclose(out, [2.0, 2.0, 2.0])


def test_calibrate_alignment_matrix():
    """A 90-degree-ish axis swap demonstrates the matrix is actually applied."""
    # alignment row-major bytes / 100: swap X and Y, negate the new Y.
    ali = [0, 100, 0, -100, 0, 0, 0, 0, 100]
    calib = _FakeCalibration(offset=[0, 0, 0], sensitivity=[1, 1, 1], ali=ali)
    out = calibrate_inertial(np.array([3.0, 5.0, 7.0]), calib, 0)
    assert np.allclose(out, [5.0, -3.0, 7.0])


def test_calibrate_none_passthrough():
    """--no-calibrate: calibration is None -> raw values unchanged."""
    raw = np.array([100, 200, 300], dtype=np.float64)
    out = calibrate_inertial(raw, None, 0)
    assert np.array_equal(out, raw)


def test_calibrate_zero_sensitivity_guarded():
    """A zero sensitivity must not divide by zero (falls back to offset shift)."""
    calib = _FakeCalibration(offset=[1, 2, 3], sensitivity=[0, 0, 0],
                             ali=[100, 0, 0, 0, 100, 0, 0, 0, 100])
    out = calibrate_inertial(np.array([5.0, 5.0, 5.0]), calib, 0)
    assert np.allclose(out, [4.0, 3.0, 2.0])


# --------------------------------------------------------------------------- #
# _as_scalar: tolerate scalar or 1-element container
# --------------------------------------------------------------------------- #

def test_as_scalar_shapes():
    assert _as_scalar(42) == 42.0
    assert _as_scalar(np.int32(7)) == 7.0
    assert _as_scalar(np.array([9])) == 9.0
    assert _as_scalar([3]) == 3.0


# --------------------------------------------------------------------------- #
# ShimmerClock: monotonic, unwraps the 24-bit device clock
# --------------------------------------------------------------------------- #

def test_clock_unwraps_wraparound():
    clock = ShimmerClock()
    # A tick just before the wrap and one just after must be ~1 tick apart,
    # NOT ~DEVICE_TIMESTAMP_MODULO ticks apart.
    before = clock.to_ns(DEVICE_TIMESTAMP_MODULO - 1)
    after = clock.to_ns(0)
    delta = after - before
    expected = int(1e9 / DEVICE_CLOCK_HZ)  # one tick in ns
    assert 0 < delta <= expected + 1


def test_clock_is_monotonic():
    clock = ShimmerClock()
    ticks = [1000, 1001, 1002, 5000, 5001]
    stamps = [clock.to_ns(t) for t in ticks]
    assert all(b > a for a, b in zip(stamps, stamps[1:]))


def test_clock_anchors_on_monotonic_and_exposes_wall_anchor():
    clock = ShimmerClock()
    first = clock.to_ns(12345)
    # Anchored to the host monotonic clock on the first packet.
    import time

    assert abs(first - time.monotonic_ns()) < 1_000_000_000  # within 1 s
    # A wall-clock anchor is available for logging calendar time.
    assert clock.wall_anchor_ns is not None
    recovered_wall_ns = first + clock.wall_anchor_ns
    assert abs(recovered_wall_ns - time.time_ns()) < 1_000_000_000


if __name__ == "__main__":
    import sys

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    sys.exit(1 if failed else 0)
