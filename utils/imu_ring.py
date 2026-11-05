"""ImuRing — ring of ``(timestamp_ns, accel[3], gyro[3], mag[3])`` records.

Orientation stream for the (model-agnostic) Madgwick filter. accel, gyro and
mag are fused into ONE slot so the filter reads a single co-timed record per
step, which is exactly the co-timing its recurrence needs.

Slot layout (item_size derived from itemsize, never hand-counted)::

    int64 + float32[3] * 3 = 8 + 36 = 44 bytes
    44 % 4 == 0  ->  every float32 field is 4-byte aligned (no padding needed)
"""

from __future__ import annotations

import numpy as np

from utils.shimmer_ring import ShimmerRing

IMU_DTYPE = np.dtype(
    [
        ("timestamp_ns", np.int64),
        ("accel", np.float32, (3,)),
        ("gyro", np.float32, (3,)),
        ("mag", np.float32, (3,)),
    ]
)


class ImuRing(ShimmerRing):
    """Ring of ``(timestamp_ns, accel[3], gyro[3], mag[3])`` records."""

    def __init__(self, name, capacity, is_creating, write_count=None):
        super().__init__(
            name=name,
            capacity=capacity,
            dtype=IMU_DTYPE,
            is_creating=is_creating,
            write_count=write_count,
        )

    @staticmethod
    def make_record(timestamp_ns, accel, gyro, mag) -> np.ndarray:
        """Build a 1-element structured record ready for :meth:`write`."""
        rec = np.zeros((1,), dtype=IMU_DTYPE)
        rec["timestamp_ns"] = timestamp_ns
        rec["accel"] = accel
        rec["gyro"] = gyro
        rec["mag"] = mag
        return rec

    def read_window(self, n):
        """Return the last ``n`` IMU records as a structured array.

        Rows are ordered oldest -> newest. ``accel``, ``gyro`` and ``mag`` of a
        row share the same ``timestamp_ns``, which is exactly the co-timing the
        Madgwick recurrence needs. ``overrun`` is intentionally dropped here;
        the filter only depends on the most recent state.
        """
        records, _overrun = self.read_last_n(n)
        return records
