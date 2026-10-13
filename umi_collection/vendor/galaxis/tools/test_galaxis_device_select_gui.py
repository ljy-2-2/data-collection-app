import importlib.util
from pathlib import Path
import sys


MODULE_PATH = Path(__file__).with_name("galaxis_device_select_gui.py")
SPEC = importlib.util.spec_from_file_location("galaxis_device_select_gui", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
gui = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gui
SPEC.loader.exec_module(gui)


def _dev(path, vid=0x303A, pid=0x1001):
    return gui.EspDev(path, vid, pid, "", "1-1.1")


def test_auto_find_accepts_only_side_labelled_protocol_devices():
    ports = [_dev("/dev/ttyACM0"), _dev("/dev/ttyUSB0", 0x1A86, 0x7523), _dev("/dev/ttyS0")]

    def probe(port, _baud):
        return {
            "/dev/ttyACM0": ("left", "accepted as left: L003,1.0"),
            "/dev/ttyUSB0": ("right", "accepted as right: R003,2.0"),
            "/dev/ttyS0": (None, "rejected: unrelated serial output"),
        }[port.device]

    accepted, diagnostics = gui.auto_find_esp_devices(115200, ports, probe)

    assert [port.device for port in accepted] == ["/dev/ttyACM0", "/dev/ttyUSB0"]
    assert "303a:1001" in diagnostics["/dev/ttyACM0"]
    assert "1a86:7523" in diagnostics["/dev/ttyUSB0"]
    assert "rejected" in diagnostics["/dev/ttyS0"]


def test_probe_ignores_rom_text_and_disables_reset_lines_before_open():
    instances = []

    class FakeSerial:
        def __init__(self, port, baudrate, timeout):
            assert port is None
            assert baudrate == 115200
            assert timeout == 0.1
            self.port = None
            self.dtr = True
            self.rts = True
            self.lines = iter([b"ESP-ROM:esp32c6-20220919\n", b"# boot\n", b"L003,1.25\n"])
            instances.append(self)

        def open(self):
            assert self.port == "/dev/ttyACM0"
            assert self.dtr is False
            assert self.rts is False

        def reset_input_buffer(self):
            pass

        def readline(self):
            return next(self.lines)

        def close(self):
            self.closed = True

    side, detail = gui.probe_encoder_port(
        _dev("/dev/ttyACM0"), 115200, serial_class=FakeSerial
    )

    assert side == "left"
    assert "L003,1.25" in detail
    assert instances[0].closed is True


def test_probe_reports_open_failure_for_diagnostics():
    class BrokenSerial:
        def __init__(self, **_kwargs):
            self.dtr = True
            self.rts = True
            self.port = None

        def open(self):
            raise PermissionError("port is busy")

        def close(self):
            pass

    side, detail = gui.probe_encoder_port(
        _dev("/dev/ttyACM1"), 115200, serial_class=BrokenSerial
    )

    assert side is None
    assert "PermissionError" in detail
    assert "port is busy" in detail
