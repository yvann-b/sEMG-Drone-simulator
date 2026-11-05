from __future__ import annotations

import argparse
import threading
from collections import deque

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from pyshimmer import DEFAULT_BAUDRATE, EChannelType, ShimmerBluetooth
from pyshimmer.dev.channels import ESensorGroup
from serial import Serial

# Raw channels only — no calibration, no LSL. Wire into the architecture later.
EMG = (
    ("EMG1", EChannelType.EXG_ADS1292R_1_CH1_24BIT),
    ("EMG2", EChannelType.EXG_ADS1292R_1_CH2_24BIT),
)
ACCEL_LN = (
    ("AccLN_X", EChannelType.ACCEL_LN_X),
    ("AccLN_Y", EChannelType.ACCEL_LN_Y),
    ("AccLN_Z", EChannelType.ACCEL_LN_Z),
)
GYRO = (
    ("Gyro_X", EChannelType.GYRO_MPU9150_X),
    ("Gyro_Y", EChannelType.GYRO_MPU9150_Y),
    ("Gyro_Z", EChannelType.GYRO_MPU9150_Z),
)
MAG = (
    ("Mag_X", EChannelType.MAG_LSM303DLHC_X),
    ("Mag_Y", EChannelType.MAG_LSM303DLHC_Y),
    ("Mag_Z", EChannelType.MAG_LSM303DLHC_Z),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live plot of raw Shimmer3 EMG + IMU values."
    )
    parser.add_argument("--port", default="COM3", help="Bluetooth outgoing COM port")
    parser.add_argument("--rate", type=float, default=512.0, help="Sampling rate (Hz)")
    parser.add_argument(
        "--window",
        type=float,
        default=5.0,
        help="Visible time window in seconds",
    )
    parser.add_argument(
        "--emg-only",
        action="store_true",
        help="Plot EMG only (skip IMU)",
    )
    parser.add_argument(
        "--imu-only",
        action="store_true",
        help="Plot IMU only (skip EMG)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.emg_only and args.imu_only:
        raise SystemExit("Choose at most one of --emg-only / --imu-only.")

    series: list[tuple[str, EChannelType]] = []
    sensors: list[ESensorGroup] = []

    if not args.imu_only:
        series.extend(EMG)
        sensors.append(ESensorGroup.EXG1_24BIT)
    if not args.emg_only:
        series.extend(ACCEL_LN)
        series.extend(GYRO)
        series.extend(MAG)
        sensors.extend(
            [ESensorGroup.ACCEL_LN, ESensorGroup.GYRO, ESensorGroup.MAG]
        )

    history_len = max(int(args.rate * args.window), 100)
    buffers = {label: deque(maxlen=history_len) for label, _ in series}
    lock = threading.Lock()
    sample_index = 0

    print(f"Connecting to Shimmer on {args.port}...")
    # Context manager already calls initialize(); do not call it again.
    with Serial(args.port, DEFAULT_BAUDRATE, timeout=None) as serial_port:
        with ShimmerBluetooth(serial_port) as shimmer:
            name = shimmer.get_device_name()
            battery = shimmer.get_battery_state(in_percent=True)
            print(f"Connected: {name} | battery {round(battery, 2)}%")

            shimmer.set_sampling_rate(args.rate)
            shimmer.set_sensors(sensors)
            actual_rate, buffer_size, active = shimmer.get_inquiry()
            print(f"Rate: {actual_rate:g} Hz | buffer={buffer_size}")
            print("Active:", ", ".join(ch.name for ch in active))

            missing = [ch for _, ch in series if ch not in active]
            if missing:
                raise RuntimeError(
                    "Missing channels: " + ", ".join(ch.name for ch in missing)
                )

            def on_packet(packet) -> None:
                nonlocal sample_index
                with lock:
                    for label, channel in series:
                        buffers[label].append(float(packet[channel]))
                    sample_index += 1

            shimmer.add_stream_callback(on_packet)
            shimmer.start_streaming()
            print("Streaming raw values. Close the plot window to stop.")

            # One subplot per sensor group keeps raw scales readable.
            groups: list[tuple[str, list[str]]] = []
            if not args.imu_only:
                groups.append(("EMG (raw)", [label for label, _ in EMG]))
            if not args.emg_only:
                groups.append(("Accel LN (raw)", [label for label, _ in ACCEL_LN]))
                groups.append(("Gyro (raw)", [label for label, _ in GYRO]))
                groups.append(("Mag (raw)", [label for label, _ in MAG]))

            fig, axes = plt.subplots(
                len(groups),
                1,
                sharex=True,
                figsize=(11, 2.4 * len(groups)),
                constrained_layout=True,
            )
            if len(groups) == 1:
                axes = [axes]

            lines: dict[str, object] = {}
            for ax, (title, labels) in zip(axes, groups):
                for label in labels:
                    (line,) = ax.plot([], [], label=label, lw=1.0)
                    lines[label] = line
                ax.set_ylabel(title)
                ax.legend(loc="upper right", fontsize=8)
                ax.grid(True, alpha=0.3)
            axes[-1].set_xlabel("sample")
            fig.suptitle(f"{name} — raw stream @ {actual_rate:g} Hz")

            def update(_frame):
                with lock:
                    for label, line in lines.items():
                        y = list(buffers[label])
                        x = list(range(sample_index - len(y), sample_index))
                        line.set_data(x, y)
                    xmin = max(0, sample_index - history_len)
                    xmax = max(history_len, sample_index)
                for ax in axes:
                    ax.set_xlim(xmin, xmax)
                    ax.relim()
                    ax.autoscale_view(scalex=False, scaley=True)
                return list(lines.values())

            _anim = FuncAnimation(
                fig, update, interval=50, blit=False, cache_frame_data=False
            )
            try:
                plt.show()
            finally:
                shimmer.stop_streaming()


if __name__ == "__main__":
    main()
