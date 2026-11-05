from __future__ import annotations

import argparse
from dataclasses import dataclass

from pylsl import StreamInfo, StreamOutlet, local_clock
from pyshimmer import DEFAULT_BAUDRATE, EChannelType, ShimmerBluetooth
from pyshimmer.dev.channels import ESensorGroup
from serial import Serial


DEVICE_CLOCK_HZ = 32768.0
DEVICE_TIMESTAMP_MODULO = 1 << 24
ADC_REFERENCE_MV = 2420.0
ADC_MAX = (1 << 23) - 1

EMG_CHANNELS = (
    EChannelType.EXG_ADS1292R_1_CH1_24BIT,
    EChannelType.EXG_ADS1292R_1_CH2_24BIT,
)


def raw_to_mv(raw_value: int, gain: int) -> float:
    """Convertit une valeur ADS1292R signee sur 24 bits en millivolts."""
    return raw_value * ADC_REFERENCE_MV / (gain * ADC_MAX)


@dataclass
class ShimmerClock:
    """Convertit l'horloge 24 bits du Shimmer vers l'horloge LSL."""

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diffuse le signal EMG d'un Shimmer3 dans LSL."
    )
    parser.add_argument("--port", default="COM3", help="port Bluetooth sortant")            # Regarder dans les paramètres bluetooth pour le com sortant
    parser.add_argument("--rate", type=float, default=512.0, help="frequence Shimmer")
    parser.add_argument("--name", default="Shimmer3_EMG", help="nom du flux LSL")
    parser.add_argument(
        "--channels",
        type=int,
        choices=(1, 2),
        default=2,
        help="nombre de canaux EMG a diffuser (defaut : EMG1 et EMG2)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    print(f"Connexion au Shimmer sur {args.port}...")
    # Le context manager ShimmerBluetooth initialise lui-meme la connexion.
    # Il ne faut donc pas rappeler shimmer.initialize(), sinon pyshimmer tente
    # de demarrer deux fois son thread de lecture.
    with Serial(args.port, DEFAULT_BAUDRATE, timeout=None) as serial_port:
        with ShimmerBluetooth(serial_port) as shimmer:
            dev_name = shimmer.get_device_name()
            actual_srate = shimmer.get_sampling_rate()
            battery = shimmer.get_battery_state(in_percent=True)
            print(f"Connexion {dev_name} etablie.")
            print(f"Etat de la batterie : {round(battery,2)}%")
            print(f"Frquence d'échantillonnage : {actual_srate:g} Hz")

            shimmer.set_sampling_rate(args.rate)
            shimmer.set_sensors([ESensorGroup.EXG1_24BIT])

            actual_rate, buffer_size, active_channels = shimmer.get_inquiry()
            selected_channels = EMG_CHANNELS[: args.channels]
            missing = [channel for channel in selected_channels if channel not in active_channels]
            if missing:
                raise RuntimeError(
                    "Canal EMG absent de la configuration Shimmer : "
                    + ", ".join(channel.name for channel in missing)
                )

            # pyshimmer 1.0 expose les registres ExG via get_exg_register().
            register = shimmer.get_exg_register(0)
            gains = (register.ch1_gain, register.ch2_gain)
            selected_gains = gains[: args.channels]

            powerdown = (register.ch1_powerdown, register.ch2_powerdown)
            if any(powerdown[: args.channels]):
                raise RuntimeError(
                    "Un canal EMG selectionne est desactive dans la configuration Shimmer."
                )

            info = StreamInfo(
                name=args.name,
                type="EMG",
                channel_count=args.channels,
                nominal_srate=actual_rate,
                channel_format="float32",
                source_id=f"Shimmer3_EMG_{args.port}",
            )
            channels_xml = info.desc().append_child("channels")
            for index in range(args.channels):
                channel_xml = channels_xml.append_child("channel")
                channel_xml.append_child_value("label", f"EMG{index + 1}")
                channel_xml.append_child_value("unit", "mV")
                channel_xml.append_child_value("type", "EMG")
            acquisition = info.desc().append_child("acquisition")
            acquisition.append_child_value("manufacturer", "Shimmer")
            acquisition.append_child_value("model", "Shimmer3 EMG")
            acquisition.append_child_value("port", args.port)
            acquisition.append_child_value("adc", "ADS1292R")
            acquisition.append_child_value(
                "gains", ",".join(str(gain) for gain in selected_gains)
            )

            outlet = StreamOutlet(info)
            clock = ShimmerClock()

            def handle_packet(packet) -> None:
                sample = [
                    raw_to_mv(packet[channel], gain)
                    for channel, gain in zip(selected_channels, selected_gains)
                ]
                timestamp = clock.to_lsl(packet[EChannelType.TIMESTAMP])
                outlet.push_sample(sample, timestamp)

            shimmer.add_stream_callback(handle_packet)

            print(f"Connexion etablie : {actual_rate:g} Hz (buffer={buffer_size})")
            print(
                "Flux LSL pret : "
                f"{args.name} | "
                + ", ".join(f"EMG{i + 1}" for i in range(args.channels))
                + " | mV"
            )
            print("Dans LabRecorder : Update -> cocher le flux -> Start")

            shimmer.start_streaming()
            try:
                while input("Tapez 'exit' pour arreter : ").strip().lower() != "exit":
                    pass
            except (KeyboardInterrupt, EOFError):
                pass
            finally:
                shimmer.stop_streaming()


if __name__ == "__main__":
    main()
