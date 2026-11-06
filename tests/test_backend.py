"""Hardware-free tests for the P0 backend wiring.

Covers ``p0.backend.DeviceBackend`` -- the ring owner + P1 spawner -- without
spawning any real P1 child or touching Bluetooth:

* it creates one EmgAccelRing + ImuRing per Shimmer, with the right names;
* the control-plane stubs do nothing harmful;
* ``close()`` stops and unlinks cleanly (idempotent enough to call after a test).

Spawning P1 for real is exercised by ``test_shimmer_acquisition`` (child on a
dead port) and by the manual run; here we keep it to the ring lifecycle.

Run with:  python -m tests.test_backend
    or:    python -m pytest tests/test_backend.py -v
"""

from __future__ import annotations


def _unique_ports(n: int) -> list[str]:
    # Deliberately invalid ports: we never connect here, but this keeps any
    # accidental spawn from succeeding.
    return [f"COM{900 + i}" for i in range(n)]


def test_creates_one_ring_pair_per_shimmer():
    from p0.backend import DeviceBackend

    backend = DeviceBackend(3, ports=_unique_ports(3))
    try:
        assert [r.name for r in backend.emg_rings] == [
            "emg_ring_0",
            "emg_ring_1",
            "emg_ring_2",
        ]
        assert [r.name for r in backend.imu_rings] == [
            "imu_ring_0",
            "imu_ring_1",
            "imu_ring_2",
        ]
        # item sizes are the typed layer's, not hand-counted
        assert backend.emg_rings[0].item_size == 28
        assert backend.imu_rings[0].item_size == 44
    finally:
        backend.close()


def test_ports_pad_with_default():
    from p0.backend import DEFAULT_PORT, DeviceBackend

    backend = DeviceBackend(2, ports=["COM7"])
    try:
        assert backend.ports == ["COM7", DEFAULT_PORT]
    finally:
        backend.close()



def test_control_plane_stubs_are_inert():
    from p0.backend import DeviceBackend

    backend = DeviceBackend(1, ports=_unique_ports(1))
    try:
        # No model processes -> no rows.
        assert backend.drain_model_queues(0.0) == []
        # Forwarding is accepted and can be drained (no simulator yet).
        backend.forward("flex", 1.0)
        drained = backend.drain_ps()
        assert drained and drained[0]["label"] == "flex"
        # Selection is remembered.
        backend.select("svm")
        assert backend.selected == "svm"
    finally:
        backend.close()


def test_close_unlinks_rings_so_they_can_be_recreated():
    from p0.backend import DeviceBackend

    backend = DeviceBackend(1, ports=_unique_ports(1))
    backend.close()

    # If unlink worked, creating the same-named rings again must not raise
    # FileExistsError.
    from utils.emg_accel_ring import EmgAccelRing
    from utils.imu_ring import ImuRing

    emg = EmgAccelRing("emg_ring_0", 16, is_creating=True)
    imu = ImuRing("imu_ring_0", 16, is_creating=True)
    try:
        assert emg.item_size == 28 and imu.item_size == 44
    finally:
        emg.close()
        emg.unlink()
        imu.close()
        imu.unlink()


def test_context_manager_closes():
    from p0.backend import DeviceBackend

    with DeviceBackend(1, ports=_unique_ports(1)) as backend:
        assert backend.number_shimmer == 1
    # After the with-block, the rings are unlinked; re-creating works.
    from utils.emg_accel_ring import EmgAccelRing

    ring = EmgAccelRing("emg_ring_0", 8, is_creating=True)
    try:
        assert ring.length == 8
    finally:
        ring.close()
        ring.unlink()


class _FakeProcess:
    """Minimal stand-in for ``mp.Process`` with controllable liveness."""

    def __init__(self, alive: bool = True) -> None:
        self.alive = alive

    def is_alive(self) -> bool:
        return self.alive


def _fake_entry(backend, index, port, alive=True):
    from p0.backend import _ShimmerProcess

    return _ShimmerProcess(
        index=index,
        port=port,
        emg_ring=backend.emg_rings[index],
        imu_ring=backend.imu_rings[index],
        process=_FakeProcess(alive),
        stop_event=None,
        event_conn=None,
    )


def test_p1_states_empty_when_not_started():
    from p0.backend import DeviceBackend

    backend = DeviceBackend(1, ports=_unique_ports(1))
    try:
        assert backend.p1_states() == []
        assert backend.p1_state() == "offline"  # aggregate
        assert backend.last_error is None
    finally:
        backend.close()


def test_p1_states_three_states_without_spawning():
    """Drive write_count / aliveness directly -- no real child needed."""
    from p0.backend import DeviceBackend

    backend = DeviceBackend(1, ports=_unique_ports(1))
    try:
        entry = _fake_entry(backend, 0, "COMx", alive=True)
        backend._shimmer_procs = [entry]

        # Alive, no samples yet -> connecting.
        assert backend.p1_states() == ["connecting"]

        # Samples advance -> online.
        backend.emg_rings[0].write_count.value += 10
        assert backend.p1_states() == ["online"]

        # No further advance -> connecting again (no new samples).
        assert backend.p1_states() == ["connecting"]

        # Child dies (no error report) -> offline.
        entry.process.alive = False
        assert backend.p1_states() == ["offline"]
    finally:
        backend._shimmer_procs = []  # do not let close() join a fake
        backend.close()


def test_p1_states_one_entry_per_shimmer():
    """Two Shimmers -> two states, so a dead one is visible next to a live one."""
    from p0.backend import DeviceBackend

    backend = DeviceBackend(2, ports=_unique_ports(2))
    try:
        live = _fake_entry(backend, 0, backend.ports[0], alive=True)
        dead = _fake_entry(backend, 1, backend.ports[1], alive=False)
        backend._shimmer_procs = [live, dead]

        states = backend.p1_states()
        assert len(states) == 2
        assert states[0] == "connecting"   # alive, no samples yet
        assert states[1] == "offline"      # dead, no error reported

        # If the dead child's port has an error recorded, it reads "error".
        backend._errors_by_port[backend.ports[1]] = "SerialException: nope"
        assert backend.p1_states()[1] == "error"

        # Aggregate prefers online > error > connecting > offline.
        backend.emg_rings[0].write_count.value += 1
        assert backend.p1_state() == "online"
    finally:
        backend._shimmer_procs = []
        backend.close()


def test_status_bar_sensors_adapt_to_count():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from p0.app import create_app
    from p0.widgets.status_bar import StatusBar

    app = create_app([])
    bar = StatusBar()
    try:
        # One sensor.
        bar.set_sensors(("online",))
        assert len(bar.sensor_labels) == 1
        assert bar.sensor_labels[0].text() == "Sensor 1: online"
        assert "#1a7f37" in bar.sensor_labels[0].styleSheet()

        # Two sensors -> rebuilt to two chips, colours per state.
        bar.set_sensors(("online", "error"))
        assert len(bar.sensor_labels) == 2
        assert bar.sensor_labels[0].text() == "Sensor 1: online"
        assert bar.sensor_labels[1].text() == "Sensor 2: error"
        assert "#b42318" in bar.sensor_labels[1].styleSheet()

        # Back to one -> one chip again.
        bar.set_sensors(("connecting",))
        assert len(bar.sensor_labels) == 1
        assert bar.sensor_labels[0].text() == "Sensor 1: connecting"

        # None state is treated as offline (default colour).
        bar.set_sensors((None,))
        assert bar.sensor_labels[0].text() == "Sensor 1: offline"
        assert bar.sensor_labels[0].styleSheet() == ""
    finally:
        bar.deleteLater()
        _ = app


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
