"""P1 — Shimmer3 acquisition: Bluetooth packets -> converted ring records.

This is the *single producer* of the shared-memory rings. For every packet the
Shimmer sends it:

1. converts the raw values into physical units **once, at the root**
   (EMG -> mV, IMU -> Shimmer-calibrated units), and
2. writes **one** ``EmgAccelRing`` record and **one** ``ImuRing`` record,
   stamped with the **same** ``timestamp_ns``.

Why convert here and not downstream
-----------------------------------
* The EMG scale factor depends on the **ExG register gain**, which only the
  acquisition process can read from the device (:func:`raw_to_mv`).
  ``hand_gesture_recognition_v1.py`` filters values already in mV and
  thresholds on ``0.01 * rms``, so the ring must hold mV to stay compatible.
* The IMU calibration is factory data read off the device once via
  ``get_all_calibration()``. The Madgwick filter needs accel (= m/s^2) and mag
  in *consistent physical* units across different chips; raw counts from two
  chips are not comparable quantities.

Which accelerometer goes where
------------------------------
The Shimmer3 has **two** accelerometers:

* **LN**  — low-noise (KXRB5-2042), ~+/-2 g, finest resolution.
* **WR**  — wide-range (on the LSM303DLHC chip, *same chip as the magnetometer*).

``EmgAccelRing.accel`` gets **LN** (highest resolution for the classifier);
``ImuRing.accel`` gets **WR** (physically the magnetometer's chip-mate, best for
orientation fusion). Both arrive in the same packet, so this costs no extra
round-trip -- just a few extra payload bytes.

Ring ownership
--------------
P1 **never creates** the rings. It is handed already-created handles (the
``is_creating=True`` owner is P0, or the standalone ``main()`` for debugging).
P1 knows nothing about who reads them; nobody reading is a valid state.

Constants and conversion
------------------------
``DEVICE_CLOCK_HZ`` / ``DEVICE_TIMESTAMP_MODULO`` / ``ADC_REFERENCE_MV`` /
``ADC_MAX`` and :func:`raw_to_mv` are copies of the values used by the working
LSL prototypes (``shimmers/shimmer_imu_emg_lsl.py``).
"""

from __future__ import annotations

import argparse
import time

import numpy as np
from pyshimmer import DEFAULT_BAUDRATE, EChannelType, ShimmerBluetooth
from pyshimmer.dev.calibration import AllCalibration
from pyshimmer.dev.channels import ESensorGroup
from serial import Serial

from utils.emg_accel_ring import EmgAccelRing
from utils.imu_ring import ImuRing

# --------------------------------------------------------------------------- #
# Constants (kept identical to shimmers/shimmer_imu_emg_lsl.py)
# --------------------------------------------------------------------------- #

#: Shimmer internal clock: 32.768 kHz.
DEVICE_CLOCK_HZ = 32768.0
#: The packet timestamp is 24 bits wide (3-byte channel) -> wraps every 512 s.
DEVICE_TIMESTAMP_MODULO = 1 << 24
#: ADS1292R ADC reference and full-scale code. Needed by :func:`raw_to_mv`.
ADC_REFERENCE_MV = 2420.0
ADC_MAX = (1 << 23) - 1

# Gravity magnitude, used only for the connect-time sanity check on calibration.
GRAVITY_MS2 = 9.81

# --------------------------------------------------------------------------- #
# Channels (pyshimmer 1.0 names)
# --------------------------------------------------------------------------- #

EMG_CHANNELS = (
    EChannelType.EXG_ADS1292R_1_CH1_24BIT,
    EChannelType.EXG_ADS1292R_1_CH2_24BIT,
)
ACCEL_LN_CHANNELS = (
    EChannelType.ACCEL_LN_X,
    EChannelType.ACCEL_LN_Y,
    EChannelType.ACCEL_LN_Z,
)
ACCEL_WR_CHANNELS = (
    EChannelType.ACCEL_LSM303DLHC_X,
    EChannelType.ACCEL_LSM303DLHC_Y,
    EChannelType.ACCEL_LSM303DLHC_Z,
)
GYRO_CHANNELS = (
    EChannelType.GYRO_MPU9150_X,
    EChannelType.GYRO_MPU9150_Y,
    EChannelType.GYRO_MPU9150_Z,
)
MAG_CHANNELS = (
    EChannelType.MAG_LSM303DLHC_X,
    EChannelType.MAG_LSM303DLHC_Y,
    EChannelType.MAG_LSM303DLHC_Z,
)

#: Sensor groups enabled on the device (one Bluetooth packet carries them all).
#: EXG1 is the only ExG chip wired to EMG; LN + WR + GYRO + MAG cover both rings.
SENSOR_GROUPS = (
    ESensorGroup.EXG1_24BIT,
    ESensorGroup.ACCEL_LN,
    ESensorGroup.ACCEL_WR,
    ESensorGroup.GYRO,
    ESensorGroup.MAG,
)

# --------------------------------------------------------------------------- #
# AllCalibration sensor indices.
#
# Authoritative order, from pyshimmer's GetAllCalibrationCommand docstring
# (84 bytes / 4 sensors, 21 bytes each):
#     [ 0:21] ACCEL_LN   [21:42] GYRO   [42:63] MAG   [63:84] ACCEL_WR
# --------------------------------------------------------------------------- #
CALIB_LN_ACCEL = 0
CALIB_GYRO = 1
CALIB_MAG = 2
CALIB_WR_ACCEL = 3


# --------------------------------------------------------------------------- #
# Conversion helpers
# --------------------------------------------------------------------------- #


def raw_to_mv(raw_value: int, gain: int) -> float:
    """Convert a signed 24-bit ADS1292R sample to millivolts.

    ``gain`` is the ExG register channel gain (read once at connect time). The
    conversion is the one documented in the Shimmer EMG user guide and used by
    ``shimmers/shimmer_emg_lsl.py``.
    """
    return raw_value * ADC_REFERENCE_MV / (gain * ADC_MAX)


def calibrate_inertial(
    raw_xyz: np.ndarray,
    calibration: AllCalibration | None,
    sensor_index: int,
) -> np.ndarray:
    """Apply Shimmer IMU calibration: ``alignment @ ((raw - offset) / sensitivity)``.

    ``calibration`` is the ``AllCalibration`` read from the device. When it is
    ``None`` (``--no-calibrate``) the raw values are passed through unchanged,
    so the caller decides whether the ring holds physical units or counts.

    ``offset`` and ``sensitivity`` are per-axis 16-bit integers; ``alignment``
    is nine signed bytes forming a 3x3 matrix stored scaled by 100.
    """
    raw = np.asarray(raw_xyz, dtype=np.float64)
    if calibration is None:
        return raw

    offset = np.asarray(calibration.get_offset_bias(sensor_index), dtype=np.float64)
    sensitivity = np.asarray(calibration.get_sensitivity(sensor_index), dtype=np.float64)
    alignment = (
        np.asarray(calibration.get_ali_mat(sensor_index), dtype=np.float64).reshape(3, 3)
        / 100.0
    )

    # A zero sensitivity would divide by zero; fall back to a pure offset shift.
    if np.any(sensitivity == 0):
        return raw - offset
    return alignment @ ((raw - offset) / sensitivity)


def _as_scalar(value) -> float:
    """Return ``value`` as a Python float.

    ``DataPacket.__getitem__`` normally returns a plain int, but a
    stream callback may hand back a 1-element container depending on the
    pyshimmer configuration. Accepting both keeps the hot path robust.
    """
    if isinstance(value, (int, float, np.integer, np.floating)):
        return float(value)
    return float(np.asarray(value).reshape(-1)[0])


# --------------------------------------------------------------------------- #
# Timestamp: 24-bit device clock -> unwrapped ns on the host monotonic clock
# --------------------------------------------------------------------------- #


class ShimmerClock:
    """Map the Shimmer's 24-bit device clock onto ``time.monotonic_ns()``.

    The device clock counts at :data:`DEVICE_CLOCK_HZ` and wraps every
    ``MODULO`` ticks. We unwrap the wraps (same rule as ``ShimmerClock`` in the
    LSL prototypes) and anchor the first packet to ``time.monotonic_ns()``.

    **Why monotonic, not wall clock.** ``time.time_ns()`` is driven by the
    system clock and can step backwards (NTP sync, manual change). The rings use
    ``timestamp_ns`` for *durations* -- window length, gap detection, Madgwick's
    ``dt`` -- and a backwards step would make ``dt`` negative. ``monotonic_ns()``
    is guaranteed non-decreasing, which is exactly what the rings measure.

    **Recovering wall-clock time.** :attr:`wall_anchor_ns` is captured once as
    ``time.time_ns() - time.monotonic_ns()``. A logger can therefore recover
    calendar time as ``timestamp_ns + wall_anchor_ns`` without the rings ever
    depending on a clock that can jump.
    """

    def __init__(self) -> None:
        self._previous_raw: int | None = None
        self._wraps = 0
        self._offset_ns: int | None = None
        #: Add to a ``timestamp_ns`` to obtain wall-clock ns (captured lazily).
        self.wall_anchor_ns: int | None = None

    def to_ns(self, raw_timestamp: int) -> int:
        """Convert a raw 24-bit device timestamp to unwrapped monotonic ns."""
        raw_timestamp = int(raw_timestamp)

        if (
            self._previous_raw is not None
            and raw_timestamp < self._previous_raw - DEVICE_TIMESTAMP_MODULO // 2
        ):
            self._wraps += 1
        self._previous_raw = raw_timestamp

        unwrapped = raw_timestamp + self._wraps * DEVICE_TIMESTAMP_MODULO
        device_ns = int(unwrapped * (1e9 / DEVICE_CLOCK_HZ))

        if self._offset_ns is None:
            now = time.monotonic_ns()
            self._offset_ns = now - device_ns
            self.wall_anchor_ns = time.time_ns() - now

        return device_ns + self._offset_ns


# --------------------------------------------------------------------------- #
# Acquisition process
# --------------------------------------------------------------------------- #


class ShimmerAcquisition:
    """P1 — read one Shimmer and write both rings, sample per sample.

    Parameters
    ----------
    emg_ring, imu_ring:
        Already-created ring handles. P1 does **not** own their shared memory;
        it only calls ``write()``.
    port:
        Bluetooth outgoing COM port.
    rate:
        Requested sampling rate (Hz).
    calibrate:
        ``True`` -> write calibrated physical units into ``ImuRing``.
        ``False`` -> write raw counts (``--no-calibrate`` escape hatch).
    """

    def __init__(
        self,
        emg_ring: EmgAccelRing,
        imu_ring: ImuRing,
        *,
        port: str = "COM3",
        rate: float = 512.0,
        calibrate: bool = True,
    ) -> None:
        self.emg_ring = emg_ring
        self.imu_ring = imu_ring
        self.port = port
        self.rate = rate
        self.calibrate = calibrate

        self._clock = ShimmerClock()
        self._gains: tuple[int, int] = (12, 12)
        self._calibration: AllCalibration | None = None

        # Counters, useful for the standalone harness and for logging.
        self.emg_written = 0
        self.imu_written = 0
        self.packets_dropped = 0

        self._stopped = False
        #: Optional shared stop flag set by the parent (P0). When present,
        #: :meth:`run` exits as soon as it is set, which is how P0 stops a
        #: child that is blocked in its streaming loop. Standalone use leaves
        #: this ``None`` and relies on :meth:`stop` / Ctrl+C instead.
        self.stop_event = None

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        """Connect, stream forever, and stop cleanly.

        The context managers mirror ``shimmers/shimmer_raw_plot.py``:
        ``ShimmerBluetooth`` already calls ``initialize()``, so it must **not**
        be called again (that would start a second reader thread).
        """
        print(f"[P1] Connecting to Shimmer on {self.port}...")
        with Serial(self.port, DEFAULT_BAUDRATE, timeout=None) as serial_port:
            with ShimmerBluetooth(serial_port) as shimmer:
                self._prepare(shimmer)

                shimmer.add_stream_callback(self._on_packet)
                shimmer.start_streaming()
                print("[P1] Streaming. Writing converted samples into the rings.")
                try:
                    while not self._should_stop():
                        time.sleep(0.05)
                finally:
                    shimmer.stop_streaming()
                    print(
                        f"[P1] Stopped. EmgAccel={self.emg_written} "
                        f"Imu={self.imu_written} dropped={self.packets_dropped}"
                    )

    def _should_stop(self) -> bool:
        """True if either the local flag or the parent's shared flag is set."""
        if self._stopped:
            return True
        return self.stop_event is not None and self.stop_event.is_set()

    def stop(self) -> None:
        """Ask :meth:`run` to exit at the next check (thread/process safe)."""
        self._stopped = True

    # -------------------------------------------------------------- prepare
    def _prepare(self, shimmer: ShimmerBluetooth) -> None:
        """Everything that must happen ONCE, before streaming starts."""
        name = shimmer.get_device_name()
        battery = shimmer.get_battery_state(in_percent=True)
        print(f"[P1] Connected: {name} | battery {round(battery, 2)}%")

        shimmer.set_sampling_rate(self.rate)
        shimmer.set_sensors(SENSOR_GROUPS)

        actual_rate, buffer_size, active = shimmer.get_inquiry()
        print(f"[P1] Rate: {actual_rate:g} Hz | buffer={buffer_size}")

        # 1) EMG gain -- raw_to_mv cannot run without it. Fail fast if a
        #    channel is powered down (misconfiguration in Consensys).
        register = shimmer.get_exg_register(0)
        self._gains = (register.ch1_gain, register.ch2_gain)
        if register.ch1_powerdown or register.ch2_powerdown:
            raise RuntimeError(
                "An EMG channel is powered down. Enable Chip1 (gain ~12) in "
                "Consensys and rewrite the configuration to the device."
            )

        # 2) Factory calibration, read from the device ONCE (over-air transfer,
        #    so it must not be inside the per-sample callback).
        self._calibration = shimmer.get_all_calibration() if self.calibrate else None

        # 3) Every channel we will read must be in the packet, or
        #    packet[channel] raises KeyError mid-stream.
        needed = (
            *EMG_CHANNELS,
            *ACCEL_LN_CHANNELS,
            *ACCEL_WR_CHANNELS,
            *GYRO_CHANNELS,
            *MAG_CHANNELS,
        )
        missing = [ch for ch in needed if ch not in active]
        if missing:
            raise RuntimeError(
                "Missing channels: " + ", ".join(ch.name for ch in missing)
            )

        if self._calibration is not None:
            self._sanity_check_calibration(self._calibration)
        else:
            print("[P1] WARNING: calibration disabled -- ImuRing holds raw counts.")

    @staticmethod
    def _sanity_check_calibration(calibration: AllCalibration) -> None:
        """Cheap guard against a wrong sensor index or a corrupt cal block.

        Uses the known hardware scale of the LN accelerometer: at rest its
        magnitude is ~1 g, i.e. a calibrated sensitivity of a few hundred to a
        few thousand LSB per m/s^2. A zero or wildly off sensitivity means the
        block (or our index) is wrong -- which would silently corrupt every
        record, so we refuse to start instead.
        """
        for label, index in (
            ("ACCEL_LN", CALIB_LN_ACCEL),
            ("GYRO", CALIB_GYRO),
            ("MAG", CALIB_MAG),
            ("ACCEL_WR", CALIB_WR_ACCEL),
        ):
            sensitivity = np.asarray(
                calibration.get_sensitivity(index), dtype=np.float64
            )
            if sensitivity.size != 3 or np.any(sensitivity == 0):
                raise RuntimeError(
                    f"Calibration block for {label} (sensor {index}) looks invalid: "
                    f"sensitivity={sensitivity.tolist()}. Refusing to write "
                    "corrupt data -- check get_all_calibration() on the device."
                )
        print("[P1] Calibration block validated (LN/GYRO/MAG/WR).")

    # --------------------------------------------------------------- packet
    def _on_packet(self, packet) -> None:
        """Convert one packet and write both rings, co-timed.

        Runs on pyshimmer's reader thread. It is the single producer, so no
        lock is needed. Any conversion error is swallowed (and counted) so a
        single malformed packet can never kill the Bluetooth reader thread.
        """
        if packet is None:
            return

        try:
            # EMG: ADC counts -> mV, using the gain read at connect time.
            emg = np.array(
                [
                    raw_to_mv(_as_scalar(packet[channel]), gain)
                    for channel, gain in zip(EMG_CHANNELS, self._gains)
                ],
                dtype=np.float32,
            )

            accel_ln = calibrate_inertial(
                [_as_scalar(packet[ch]) for ch in ACCEL_LN_CHANNELS],
                self._calibration,
                CALIB_LN_ACCEL,
            ).astype(np.float32)
            accel_wr = calibrate_inertial(
                [_as_scalar(packet[ch]) for ch in ACCEL_WR_CHANNELS],
                self._calibration,
                CALIB_WR_ACCEL,
            ).astype(np.float32)
            gyro = calibrate_inertial(
                [_as_scalar(packet[ch]) for ch in GYRO_CHANNELS],
                self._calibration,
                CALIB_GYRO,
            ).astype(np.float32)
            mag = calibrate_inertial(
                [_as_scalar(packet[ch]) for ch in MAG_CHANNELS],
                self._calibration,
                CALIB_MAG,
            ).astype(np.float32)

            # One timestamp for every field of this packet -> co-timed rows.
            timestamp_ns = np.int64(
                self._clock.to_ns(_as_scalar(packet[EChannelType.TIMESTAMP]))
            )

            self.emg_ring.write(
                EmgAccelRing.make_record(timestamp_ns, emg, accel_ln)
            )
            self.imu_ring.write(
                ImuRing.make_record(timestamp_ns, accel_wr, gyro, mag)
            )

            self.emg_written += 1
            self.imu_written += 1

        except Exception as exc:  # noqa: BLE001 -- never kill the reader thread
            self.packets_dropped += 1
            print(f"[P1] packet conversion failed: {exc!r}")


# --------------------------------------------------------------------------- #
# Standalone harness (the "run it by hand" path of PROJECT_LAYOUT.md §5)
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="P1 - Shimmer3 -> EmgAccelRing + ImuRing (converted values)."
    )
    parser.add_argument("--port", default="COM3", help="Bluetooth outgoing COM port")
    parser.add_argument("--rate", type=float, default=512.0, help="Sampling rate (Hz)")
    parser.add_argument(
        "--ring-index",
        type=int,
        default=0,
        help="Ring index -> emg_ring_<i> / imu_ring_<i>",
    )
    parser.add_argument(
        "--ring-capacity", type=int, default=8192, help="Ring capacity in slots"
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=10.0,
        help="Standalone run duration (0 = until Ctrl+C)",
    )
    parser.add_argument(
        "--no-calibrate",
        action="store_true",
        help="Write raw IMU counts instead of calibrated values",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Create the rings, acquire for ``--seconds``, then tear the rings down."""
    args = parse_args(argv)

    emg_name = f"emg_ring_{args.ring_index}"
    imu_name = f"imu_ring_{args.ring_index}"

    emg_ring = EmgAccelRing(emg_name, args.ring_capacity, is_creating=True)
    imu_ring = ImuRing(imu_name, args.ring_capacity, is_creating=True)

    acquisition = ShimmerAcquisition(
        emg_ring,
        imu_ring,
        port=args.port,
        rate=args.rate,
        calibrate=not args.no_calibrate,
    )

    try:
        if args.seconds > 0:
            # Timed run: stop after --seconds from a watchdog thread.
            import threading

            threading.Timer(args.seconds, acquisition.stop).start()
        acquisition.run()
    except KeyboardInterrupt:
        acquisition.stop()
    finally:
        # Only the creator unlinks. close() detaches, unlink() frees the shm.
        for ring in (emg_ring, imu_ring):
            ring.close()
            ring.unlink()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
