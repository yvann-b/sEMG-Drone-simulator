"""P1 — Device acquisition.

Bluetooth Shimmer driver that converts raw packets into physical units and
writes them into the shared-memory rings owned by P0.

Run standalone (debugging, no P0)::

    python -m p1_device_acquisition.shimmer_acquisition --port COM3 --seconds 10

See ``docs/ARCHITECTURE.md`` §3 (P1) and ``docs/PROJECT_LAYOUT.md``.
"""
