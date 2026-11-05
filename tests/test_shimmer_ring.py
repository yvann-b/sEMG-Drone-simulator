"""Hardware-free tests for the typed ring layer.

No Shimmer / Bluetooth needed: everything here runs against shared memory
created and torn down inside the test.

Run with:  python -m pytest tests/test_shimmer_ring.py -v
    or:    python tests/test_shimmer_ring.py
"""

from __future__ import annotations

import multiprocessing as mp
import uuid

import numpy as np

from utils.emg_accel_ring import EMG_ACCEL_DTYPE, EmgAccelRing
from utils.imu_ring import IMU_DTYPE, ImuRing


def _unique_name(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _random_emg_accel(n: int, start_ts: int = 0) -> np.ndarray:
    rec = np.zeros(n, dtype=EMG_ACCEL_DTYPE)
    rec["timestamp_ns"] = np.arange(start_ts, start_ts + n, dtype=np.int64)
    rec["emg"] = np.random.randn(n, 2).astype(np.float32)
    rec["accel"] = np.random.randn(n, 3).astype(np.float32)
    return rec


def _random_imu(n: int, start_ts: int = 0) -> np.ndarray:
    rec = np.zeros(n, dtype=IMU_DTYPE)
    rec["timestamp_ns"] = np.arange(start_ts, start_ts + n, dtype=np.int64)
    rec["accel"] = np.random.randn(n, 3).astype(np.float32)
    rec["gyro"] = np.random.randn(n, 3).astype(np.float32)
    rec["mag"] = np.random.randn(n, 3).astype(np.float32)
    return rec


# --------------------------------------------------------------------------- #
# item_size must equal dtype.itemsize (the "never hand-count" guarantee)
# --------------------------------------------------------------------------- #

def test_item_sizes():
    assert EMG_ACCEL_DTYPE.itemsize == 28, EMG_ACCEL_DTYPE
    assert IMU_DTYPE.itemsize == 44, IMU_DTYPE

    r1 = EmgAccelRing(_unique_name("ea"), 16, is_creating=True)
    r2 = ImuRing(_unique_name("imu"), 16, is_creating=True)
    try:
        assert r1.item_size == EMG_ACCEL_DTYPE.itemsize == 28
        assert r2.item_size == IMU_DTYPE.itemsize == 44
    finally:
        for r in (r1, r2):
            r.close()
            r.unlink()


# --------------------------------------------------------------------------- #
# Round-trip: write N records, read them back, assert bit-exact equality
# --------------------------------------------------------------------------- #

def test_roundtrip_last_n():
    cap = 100
    ring = EmgAccelRing(_unique_name("ea"), cap, is_creating=True)
    try:
        data = _random_emg_accel(cap)
        for i in range(cap):
            ring.write(data[i : i + 1])

        got, _overrun = ring.read_last_n(cap)
        assert got.shape == (cap,)
        assert np.array_equal(got, data)

        # a shorter window returns the newest tail, oldest-first
        got2, _ = ring.read_last_n(10)
        assert np.array_equal(got2, data[-10:])
    finally:
        ring.close()
        ring.unlink()


def test_roundtrip_latest_and_fifo():
    ring = ImuRing(_unique_name("imu"), 8, is_creating=True)
    try:
        data = _random_imu(5)
        for i in range(5):
            ring.write(data[i : i + 1])

        latest, current = ring.read_latest()
        assert current == 5
        assert np.array_equal(latest, data[-1])

        # FIFO drain with a fresh cursor, in order
        seen = []
        while True:
            res = ring.read_next_fifo("calib")
            if res is None:
                break
            rec, _idx, gap = res
            assert gap == 0
            seen.append(rec)
        assert len(seen) == 5
        assert np.array_equal(np.array(seen), data)
    finally:
        ring.close()
        ring.unlink()


def test_empty_reads():
    ring = ImuRing(_unique_name("imu"), 8, is_creating=True)
    try:
        assert ring.read_latest() is None
        assert ring.read_next_fifo("c") is None
        got, overrun = ring.read_last_n(4)
        assert got.shape == (0,)
        assert overrun == 0
    finally:
        ring.close()
        ring.unlink()


# --------------------------------------------------------------------------- #
# seek_to_latest: a recorder starts at "now", not at the oldest stale slot
# --------------------------------------------------------------------------- #

def test_seek_to_latest_skips_stale_data():
    ring = EmgAccelRing(_unique_name("ea"), 32, is_creating=True)
    try:
        old = _random_emg_accel(10, start_ts=0)
        for i in range(10):
            ring.write(old[i : i + 1])

        pos = ring.seek_to_latest("rec")
        assert pos == 10

        # nothing new -> None even though 10 stale samples exist
        assert ring.read_next_fifo("rec") is None

        new = _random_emg_accel(3, start_ts=100)
        for i in range(3):
            ring.write(new[i : i + 1])

        seen = []
        while True:
            res = ring.read_next_fifo("rec")
            if res is None:
                break
            seen.append(res[0])
        assert len(seen) == 3
        assert np.array_equal(np.array(seen), new)
    finally:
        ring.close()
        ring.unlink()


# --------------------------------------------------------------------------- #
# gap detection: writer overruns an unread cursor -> gap > 0
# --------------------------------------------------------------------------- #

def test_gap_reported():
    cap = 8
    ring = EmgAccelRing(_unique_name("ea"), cap, is_creating=True)
    try:
        ring.seek_to_latest("c")  # cursor at 0
        data = _random_emg_accel(cap + 5)  # write more than capacity
        for i in range(len(data)):
            ring.write(data[i : i + 1])
        rec, _idx, gap = ring.read_next_fifo("c")
        assert gap > 0, "overrunning the ring must report a gap"
    finally:
        ring.close()
        ring.unlink()


# --------------------------------------------------------------------------- #
# imu window helper: accel/gyro/mag co-timed per row
# --------------------------------------------------------------------------- #

def test_imu_read_window():
    ring = ImuRing(_unique_name("imu"), 32, is_creating=True)
    try:
        data = _random_imu(20)
        for i in range(20):
            ring.write(data[i : i + 1])
        win = ring.read_window(6)
        assert win.shape == (6,)
        assert np.array_equal(win, data[-6:])
        # co-timing: every row has one timestamp shared by all its fields
        assert np.all(win["timestamp_ns"] == data[-6:]["timestamp_ns"])
    finally:
        ring.close()
        ring.unlink()


# --------------------------------------------------------------------------- #
# write() rejects a wrongly-sized record instead of corrupting
# --------------------------------------------------------------------------- #

def test_wrong_size_rejected():
    ring = EmgAccelRing(_unique_name("ea"), 8, is_creating=True)
    try:
        bad = np.zeros(2, dtype=EMG_ACCEL_DTYPE)  # 2 records, not 1
        try:
            ring.write(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for wrong-size record")
    finally:
        ring.close()
        ring.unlink()


# --------------------------------------------------------------------------- #
# Pickle across a process boundary: child writes, parent reads typed records
# --------------------------------------------------------------------------- #

def _child_writer(ring, n):
    for i in range(n):
        rec = ring.make_record(
            timestamp_ns=i,
            emg=np.array([i, -i], dtype=np.float32),
            accel=np.array([i, i, i], dtype=np.float32),
        )
        ring.write(rec)
    ring.close()


def test_multiprocess_pickle():
    ctx = mp.get_context("spawn")
    ring = EmgAccelRing(_unique_name("ea"), 16, is_creating=True)
    try:
        n = 7
        p = ctx.Process(target=_child_writer, args=(ring, n))
        p.start()
        p.join(timeout=30)
        assert p.exitcode == 0

        got, _ = ring.read_last_n(n)
        assert got.shape == (n,)
        # child encoded timestamp==i, emg=[i, -i]; verify typed re-interpret
        assert np.all(got["timestamp_ns"] == np.arange(n))
        assert np.all(got["emg"][:, 0] == np.arange(n, dtype=np.float32))
        assert np.all(got["emg"][:, 1] == -np.arange(n, dtype=np.float32))
    finally:
        ring.close()
        ring.unlink()


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
