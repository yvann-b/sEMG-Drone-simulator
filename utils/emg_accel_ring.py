"""EmgAccelRing — ring of ``(timestamp_ns, emg[2], accel[3])`` records.

Self-sufficient classifier stream. The accelerometer is duplicated here (it is
also present in :class:`~utils.imu_ring.ImuRing`) on purpose: a classifier can
read its whole window from ONE ring, avoiding a cross-ring timestamp join and
avoiding copying gyro/mag bytes it would discard.

Slot layout (item_size derived from itemsize, never hand-counted)::

    int64 + float32[2] + float32[3] = 8 + 8 + 12 = 28 bytes
    28 % 4 == 0  ->  every float32 field is 4-byte aligned
"""

from __future__ import annotations

import numpy as np

from utils.shimmer_ring import ShimmerRing

EMG_ACCEL_DTYPE = np.dtype(
    [
        ("timestamp_ns", np.int64),
        ("emg", np.float32, (2,)),
        ("accel", np.float32, (3,)),
    ]
)


class EmgAccelRing(ShimmerRing):
    """Ring of ``(timestamp_ns, emg[2], accel[3])`` records."""

    def __init__(self, name, capacity, is_creating, write_count=None):
        super().__init__(
            name=name,
            capacity=capacity,
            dtype=EMG_ACCEL_DTYPE,
            is_creating=is_creating,
            write_count=write_count,
        )

    @staticmethod
    def make_record(timestamp_ns, emg, accel) -> np.ndarray:
        """Build a 1-element structured record ready for :meth:`write`."""
        rec = np.zeros((1,), dtype=EMG_ACCEL_DTYPE)
        rec["timestamp_ns"] = timestamp_ns
        rec["emg"] = emg
        rec["accel"] = accel
        return rec
