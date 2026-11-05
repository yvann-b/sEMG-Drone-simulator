"""Typed ring buffers backed by ``CircularBuffer``.

``CircularBuffer`` stores opaque bytes (``uint8`` slots). This module adds a
*typed* layer on top: the caller declares a ``numpy.dtype`` once, ``item_size``
is derived from it (never hand-counted), and every read returns structured
records instead of raw bytes.

Layout reminder (from ``CircularBuffer``):

    slot i is item_size bytes, i = write_count % length
    write_count is a monotonic, never-wrapping counter

Subclasses (:mod:`utils.rings`) only pin a dtype; all the mechanics live here.
"""

from __future__ import annotations

import numpy as np

from utils.circular_buffer import CircularBuffer


class ShimmerRing(CircularBuffer):
    """``CircularBuffer`` whose slots are records of a fixed ``numpy.dtype``.

    Parameters
    ----------
    name:
        Shared-memory segment name (unique per ring instance).
    capacity:
        Number of slots (``length``).
    dtype:
        Structured numpy dtype describing one record. ``item_size`` is taken
        from ``dtype.itemsize`` so the slot size can never drift from the
        record layout.
    is_creating:
        ``True`` for the single producer that allocates the segment, ``False``
        for every subscriber.
    write_count:
        The shared ``Value('Q')`` counter. Must be provided when
        ``is_creating=False`` (it cannot be attached by name).
    """

    def __init__(self, name, capacity, dtype, is_creating, write_count=None):
        self.dtype = np.dtype(dtype)
        super().__init__(
            name=name,
            capacity=capacity,
            item_size=self.dtype.itemsize,  # derived, never hand-counted
            is_creating=is_creating,
            write_count=write_count,
        )

    # ------------------------------------------------------------------ #
    # pickle support: the base drops/rebuilds ``_view``; guard consistency.
    # ------------------------------------------------------------------ #
    def __setstate__(self, state):
        super().__setstate__(state)
        assert self._view.shape[1] == self.dtype.itemsize

    # ------------------------------------------------------------------ #
    # write
    # ------------------------------------------------------------------ #
    def write(self, record):
        """Append one structured record to the ring.

        ``record`` may be a ``np.void`` (one element of the dtype) or a
        1-element array of the dtype. It is normalised to raw bytes and
        written through the base single-producer ``write``.
        """
        raw = self._as_uint8(record)
        if raw.nbytes != self.item_size:
            raise ValueError(
                f"record {raw.nbytes}o != slot {self.item_size}o "
                f"(dtype {self.dtype})"
            )
        CircularBuffer.write(self, raw)

    @staticmethod
    def _as_uint8(record) -> np.ndarray:
        """Return ``record`` flattened to a contiguous ``uint8`` array."""
        arr = np.ascontiguousarray(record)
        # np.void / 0-d records -> treat as a single element
        return arr.reshape(-1).view(np.uint8)

    def _as_records(self, raw: np.ndarray) -> np.ndarray:
        """Reinterpret a copied ``uint8`` slot (or block) as structured rows."""
        return np.ascontiguousarray(raw).view(self.dtype).reshape(-1)

    # ------------------------------------------------------------------ #
    # typed reads (override the byte-level base methods)
    # ------------------------------------------------------------------ #
    def read_latest(self):
        """Latest record as a structured scalar, or ``None`` if empty.

        Returns ``(record, write_count)`` to mirror the base signature.
        """
        res = CircularBuffer.read_latest(self)
        if res is None:
            return None
        raw, current = res
        return self._as_records(raw)[0], current

    def read_next_fifo(self, cursor_id):
        """Next unread record for ``cursor_id``.

        Returns ``(record, last_seen, gap)`` or ``None`` when nothing new.
        ``gap > 0`` means ``gap`` samples were overwritten before they were
        read and are lost for good (callers doing calibration should abort).
        """
        res = CircularBuffer.read_next_fifo(self, cursor_id)
        if res is None:
            return None
        raw, last_seen, gap = res
        return self._as_records(raw)[0], last_seen, gap

    def read_last_n(self, n):
        """Snapshot of up to the ``n`` most recent records, oldest first.

        Returns ``(records, overrun)`` where ``records`` is a structured array
        of length ``<= n``. ``overrun`` is a heuristic (unlocked counter): it
        flags that the oldest slots of the batch may have been overwritten
        mid-copy. Consumers that only need the newest data may ignore it.
        """
        raw, overrun = CircularBuffer.read_last_n(self, n)
        if raw.shape[0] == 0:
            return np.empty((0,), dtype=self.dtype), overrun
        return self._as_records(raw), overrun

    # ------------------------------------------------------------------ #
    # cursor helpers
    # ------------------------------------------------------------------ #
    def seek_to_latest(self, cursor_id):
        """Jump ``cursor_id`` to the current write position.

        Use this at the start of a recording so the consumer begins "now"
        instead of replaying (potentially up to ``length``) stale pre-record
        samples. Returns the position the cursor was set to.
        """
        current = self.write_count.value
        self._cursors[cursor_id] = current
        return current
