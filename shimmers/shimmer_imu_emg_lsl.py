from __future__ import annotations

import argparse
from dataclasses import dataclass

import numpy as np
from pylsl import StreamInfo, StreamOutlet, local_clock
from pyshimmer import DEFAULT_BAUDRATE, EChannelType, ShimmerBluetooth
from pyshimmer.dev.calibration import AllCalibration
from pyshimmer.dev.channels import ESensorGroup
from serial import Serial

DEVICE_CLOCK_HZ = 32768.0
DEVICE_TIMESTAMP_MODULO = 1 << 24
ADC_REFERENCE_MV = 2420.0
ADC_MAX = (1 << 23) - 1

# Official docs: EMG uses ExG chip 1 only (exg1_24bit=1).
EMG_CHANNELS = (
    EChannelType.EXG_ADS1292R_1_CH1_24BIT,
    EChannelType.EXG_ADS1292R_1_CH2_24BIT,
)

# Shimmer3 IMU / kinematic channels (pyshimmer 1.0 names).
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

# Order used by Shimmer AllCalibration (84 bytes / 4 sensors).
CALIB_LN_ACCEL = 0
CALIB_GYRO = 1
CALIB_MAG = 2
CALIB_WR_ACCEL = 3


def raw_to_mv(raw_value: int, gain: int) -> float:
    """Convert a signed 24-bit ADS1292R sample to millivolts (EMG User Guide)."""
    return raw_value * ADC_REFERENCE_MV / (gain * ADC_MAX)


def calibrate_inertial(
    raw_xyz: np.ndarray,
    calibration: AllCalibration,
    sensor_index: int,
) -> np.ndarray:
    """Apply Shimmer IMU calibration: Alignment @ ((raw - offset) / sensitivity)."""
    offset = np.asarray(calibration.get_offset_bias(sensor_index), dtype=float)
    sensitivity = np.asarray(calibration.get_sensitivity(sensor_index), dtype=float)
    alignment = (
        np.asarray(calibration.get_ali_mat(sensor_index), dtype=float).reshape(3, 3)
        / 100.0
    )
    if np.any(sensitivity == 0):
        return raw_xyz.astype(float)
    return alignment @ ((raw_xyz.astype(float) - offset) / sensitivity)


@dataclass
class ShimmerClock:
    """Convert the Shimmer 24-bit device clock to the LSL clock."""

    previous_raw: int | None = None
    wraps: int = 0
    lsl_offset: float | None = None

    def to_lsl(self, raw_timestamp: int) -> float:
        if (
            self.previous_raw is not None
            and raw_timestamp < self.previous_raw - DEVICE_TIMESTAMP_MODULO // 2
        ):
            self.wraps += 1

        self.previous_raw = raw_timestamp
        device_seconds = (
            raw_timestamp + self.wraps * DEVICE_TIMESTAMP_MODULO
        ) / DEVICE_CLOCK_HZ

        if self.lsl_offset is None:
            self.lsl_offset = local_clock() - device_seconds

        return device_seconds + self.lsl_offset


@dataclass(frozen=True)
class StreamChannel:
    label: str
    unit: str
    kind: str
    channel: EChannelType | None = None
    gain: int | None = None
    calib_index: int | None = None
    axis: int | None = None


def build_channels(
    emg_count: int,
    gains: tuple[int, ...],
    include_ln: bool,
    include_wr: bool,
    include_gyro: bool,
    include_mag: bool,
    calibrate: bool,
) -> list[StreamChannel]:
    channels: list[StreamChannel] = []

    for index in range(emg_count):
        channels.append(
            StreamChannel(
                label=f"EMG{index + 1}",
                unit="mV",
                kind="EMG",
                channel=EMG_CHANNELS[index],
                gain=gains[index],
            )
        )

    imu_unit = "calibrated" if calibrate else "raw"
    groups = []
    if include_ln:
        groups.append(("Accel_LN", ACCEL_LN_CHANNELS, "ACCEL", CALIB_LN_ACCEL, imu_unit))
    if include_wr:
        groups.append(("Accel_WR", ACCEL_WR_CHANNELS, "ACCEL", CALIB_WR_ACCEL, imu_unit))
    if include_gyro:
        groups.append(("Gyro", GYRO_CHANNELS, "GYRO", CALIB_GYRO, imu_unit))
    if include_mag:
        groups.append(("Mag", MAG_CHANNELS, "MAG", CALIB_MAG, imu_unit))

    for prefix, axis_channels, kind, calib_index, unit in groups:
        for axis, channel in enumerate(axis_channels):
            axis_name = "XYZ"[axis]
            channels.append(
                StreamChannel(
                    label=f"{prefix}_{axis_name}",
                    unit=unit,
                    kind=kind,
                    channel=channel,
                    calib_index=calib_index if calibrate else None,
                    axis=axis,
                )
            )

    return channels


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Stream Shimmer3 EMG + IMU (LN/WR accel, gyro, mag) to Lab Streaming Layer. "
            "Docs: EMG uses exg1_24bit; IMU uses accel / accel_d / gyro / mag."
        )
    )
    parser.add_argument(
        "--port",
        default="COM3",
        help="Windows Bluetooth outgoing COM port (pair PIN 1234)",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=512.0,
        help="Sampling rate in Hz (512 Hz recommended for sEMG)",
    )
    parser.add_argument("--name", default="Shimmer3_IMU_EMG", help="LSL stream name")
    parser.add_argument(
        "--emg-channels",
        type=int,
        choices=(0, 1, 2),
        default=2,
        help="Number of EMG channels (0 disables EMG)",
    )
    parser.add_argument(
        "--no-ln-accel",
        action="store_true",
        help="Disable low-noise accelerometer (accel=1)",
    )
    parser.add_argument(
        "--wr-accel",
        action="store_true",
        help="Enable wide-range accelerometer (accel_d=1)",
    )
    parser.add_argument("--no-gyro", action="store_true", help="Disable gyroscope")
    parser.add_argument("--no-mag", action="store_true", help="Disable magnetometer")
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Apply onboard IMU calibration matrices (raw otherwise)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    include_ln = not args.no_ln_accel
    include_wr = args.wr_accel
    include_gyro = not args.no_gyro
    include_mag = not args.no_mag

    sensors: list[ESensorGroup] = []
    if args.emg_channels > 0:
        sensors.append(ESensorGroup.EXG1_24BIT)
    if include_ln:
        sensors.append(ESensorGroup.ACCEL_LN)
    if include_wr:
        sensors.append(ESensorGroup.ACCEL_WR)
    if include_gyro:
        sensors.append(ESensorGroup.GYRO)
    if include_mag:
        sensors.append(ESensorGroup.MAG)

    if not sensors:
        raise SystemExit("Enable at least one sensor (EMG and/or IMU).")

    print(f"Connecting to Shimmer on {args.port}...")
    with Serial(args.port, DEFAULT_BAUDRATE, timeout=None) as serial_port:
        with ShimmerBluetooth(serial_port) as shimmer:
            dev_name = shimmer.get_device_name()
            battery = shimmer.get_battery_state(in_percent=True)
            print(f"Connected: {dev_name}")
            print(f"Battery: {round(battery, 2)}%")

            shimmer.set_sampling_rate(args.rate)
            shimmer.set_sensors(sensors)

            actual_rate, buffer_size, active_channels = shimmer.get_inquiry()
            print(f"Sampling rate: {actual_rate:g} Hz (buffer={buffer_size})")
            print("Active channels:", ", ".join(ch.name for ch in active_channels))

            gains = (12, 12)
            if args.emg_channels > 0:
                register = shimmer.get_exg_register(0)
                gains = (register.ch1_gain, register.ch2_gain)
                powerdown = (register.ch1_powerdown, register.ch2_powerdown)
                if any(powerdown[: args.emg_channels]):
                    raise RuntimeError(
                        "Selected EMG channel is powered down. "
                        "Configure EMG mode in Consensys (Chip1 active, gain ~12)."
                    )

            calibration = shimmer.get_all_calibration() if args.calibrate else None

            stream_channels = build_channels(
                emg_count=args.emg_channels,
                gains=gains,
                include_ln=include_ln,
                include_wr=include_wr,
                include_gyro=include_gyro,
                include_mag=include_mag,
                calibrate=args.calibrate,
            )

            missing = [
                ch.channel
                for ch in stream_channels
                if ch.channel is not None and ch.channel not in active_channels
            ]
            if missing:
                raise RuntimeError(
                    "Configured channel missing from Shimmer stream: "
                    + ", ".join(channel.name for channel in missing)
                )

            info = StreamInfo(
                name=args.name,
                type="Mixed",
                channel_count=len(stream_channels),
                nominal_srate=actual_rate,
                channel_format="float32",
                source_id=f"Shimmer3_IMU_EMG_{args.port}",
            )
            channels_xml = info.desc().append_child("channels")
            for stream_channel in stream_channels:
                channel_xml = channels_xml.append_child("channel")
                channel_xml.append_child_value("label", stream_channel.label)
                channel_xml.append_child_value("unit", stream_channel.unit)
                channel_xml.append_child_value("type", stream_channel.kind)

            acquisition = info.desc().append_child("acquisition")
            acquisition.append_child_value("manufacturer", "Shimmer")
            acquisition.append_child_value("model", "Shimmer3 EMG + IMU")
            acquisition.append_child_value("port", args.port)
            acquisition.append_child_value(
                "sensors",
                ",".join(sensor.name for sensor in sensors),
            )

            outlet = StreamOutlet(info)
            clock = ShimmerClock()

            # Cache IMU axis groups for one calibration call per triad.
            imu_groups: dict[int, list[StreamChannel]] = {}
            for stream_channel in stream_channels:
                if stream_channel.calib_index is not None:
                    imu_groups.setdefault(stream_channel.calib_index, []).append(
                        stream_channel
                    )

            def handle_packet(packet) -> None:
                sample = [0.0] * len(stream_channels)
                calibrated_cache: dict[int, np.ndarray] = {}

                for index, stream_channel in enumerate(stream_channels):
                    assert stream_channel.channel is not None
                    raw_value = packet[stream_channel.channel]

                    if stream_channel.kind == "EMG":
                        assert stream_channel.gain is not None
                        sample[index] = raw_to_mv(raw_value, stream_channel.gain)
                        continue

                    if (
                        calibration is not None
                        and stream_channel.calib_index is not None
                        and stream_channel.axis is not None
                    ):
                        calib_index = stream_channel.calib_index
                        if calib_index not in calibrated_cache:
                            group = imu_groups[calib_index]
                            raw_xyz = np.array(
                                [packet[member.channel] for member in group],
                                dtype=float,
                            )
                            calibrated_cache[calib_index] = calibrate_inertial(
                                raw_xyz, calibration, calib_index
                            )
                        sample[index] = float(
                            calibrated_cache[calib_index][stream_channel.axis]
                        )
                    else:
                        sample[index] = float(raw_value)

                timestamp = clock.to_lsl(packet[EChannelType.TIMESTAMP])
                outlet.push_sample(sample, timestamp)

            shimmer.add_stream_callback(handle_packet)

            print(
                "LSL stream ready: "
                f"{args.name} | {len(stream_channels)} channels | "
                + ", ".join(ch.label for ch in stream_channels)
            )
            print("In LabRecorder: Update -> select stream -> Start")

            shimmer.start_streaming()
            try:
                while input("Type 'exit' to stop: ").strip().lower() != "exit":
                    pass
            except (KeyboardInterrupt, EOFError):
                pass
            finally:
                shimmer.stop_streaming()


if __name__ == "__main__":
    main()
