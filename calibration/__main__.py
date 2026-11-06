"""Allow ``python -m calibration`` to exercise the recorder on its own.

Two modes are supported, both hardware-free:

* ``--simulate`` (default): P0 creates a ring, a background thread writes fake
  samples into it at ``--sampling-rate``, and Pc records against a shortened
  timeline. This is the fastest way to see a ``.npz`` appear without a Shimmer.
* ``--attach NAME``: subscribe to an *existing* ring by name. ``write_count``
  cannot be attached by name, so this mode needs the producer's counter; it is
  kept for custom scripts and is not the supported path (P0 spawns Pc).

Normal use is from P0 (the *Start calibration* button), not from here.
"""

from __future__ import annotations

import argparse
import threading
import time

from calibration.recorder import DATA_DIR, Pc
from p0.calibration_config import CalibrationConfig
from utils.emg_accel_ring import EmgAccelRing


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pc -- ring -> data/calib_*.npz")
    parser.add_argument(
        "--config", required=True, help="Path to a calibrations/*.json recipe"
    )
    parser.add_argument("--session", default=None, help="Session name")
    parser.add_argument("--seed", type=int, default=None, help="Timeline shuffle seed")
    parser.add_argument(
        "--sampling-rate", type=float, default=512.0, help="Simulated sample rate (Hz)"
    )
    parser.add_argument(
        "--ring-capacity", type=int, default=8192, help="Ring capacity in slots"
    )
    parser.add_argument(
        "--ring-index", type=int, default=0, help="Ring index -> emg_ring_<i>"
    )
    return parser.parse_args(argv)


def _feed_fake(ring: EmgAccelRing, rate: float, stop: threading.Event) -> None:
    """Write synthetic records into the ring until ``stop`` is set."""
    period = 1.0 / rate
    ts = time.monotonic_ns()
    while not stop.is_set():
        ts += int(period * 1e9)
        emg = [float(ts % 1000) * 0.01, -(float(ts % 700) * 0.01)]
        accel = [0.0, 0.0, 9.81]
        ring.write(EmgAccelRing.make_record(ts, emg, accel))
        time.sleep(period)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = CalibrationConfig.load(args.config)

    name = f"emg_ring_{args.ring_index}"
    ring = EmgAccelRing(name, args.ring_capacity, is_creating=True)
    stop = threading.Event()
    feeder = threading.Thread(
        target=_feed_fake, args=(ring, args.sampling_rate, stop), daemon=True
    )

    try:
        feeder.start()
        # Give the feeder a moment to fill the head so seek_to_latest is sane.
        time.sleep(0.2)
        pc = Pc(
            ring,
            config,
            config_path=args.config,
            session_name=args.session,
            output_dir=DATA_DIR,
            seed=args.seed,
            sampling_rate_hz=args.sampling_rate,
        )
        path = pc.run()
        if path is not None:
            print(f"[Pc] Wrote {path}")
        return 0 if path is not None else 1
    finally:
        stop.set()
        feeder.join(timeout=1.0)
        ring.close()
        ring.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
