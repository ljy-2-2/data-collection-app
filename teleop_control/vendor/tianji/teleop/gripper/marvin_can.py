"""Reuse the existing Marvin SDK connection: On{Set,Get}ChData{A,B}, channel 1.

Vendor README §7 shows little-endian CAN ID followed by payload. Header width
and receive layout must be confirmed for the installed endpoint firmware using
read-only register probes; the vendor's 32/64-bit wording is contradictory.
"""

import ctypes
import math
import struct
import time
from collections import deque
from .damiao import Driver, TransportBusy
from .compatibility import voltage_telemetry


class MarvinCAN:
    read_separator_required = True

    def prepare_register_read(self, motor_id, rid):
        """Separate identical queries with another read, never a parameter write.

        Site measurements: changed queries reply, immediately repeated payloads
        do not. The layer suppressing duplicates is not yet established.
        """
        other = 0x08 if rid != 0x08 else 0x07
        header = struct.pack("<HBB", motor_id, 0x33, other)
        self.send(0x7FF, header)
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            packet = self.recv()
            if packet is None:
                time.sleep(0.001)
                continue
            cid, data = packet
            if cid >= 0 and len(data) == 8 and data[:4] == header:
                return

    def __init__(self, robot, side, motor, config, diagnostics=False):
        self.trace = deque(maxlen=32) if diagnostics else None
        self.channel = robot.config["arm_channels"][side]
        self.tx_width = config["tx_id_bytes"]
        self.rx_width = config["rx_id_bytes"]
        self.master_id = motor["master_id"]
        self.motor_id = motor["motor_id"]
        self.last_control = None
        self.send_deadline = None
        lib = robot.api.robot
        self.set_data = getattr(lib, "OnSetChData" + self.channel)
        self.get_data = getattr(lib, "OnGetChData" + self.channel)
        self.clear_data = getattr(lib, "OnClearChData" + self.channel)
        byte_ptr = ctypes.POINTER(ctypes.c_ubyte)
        self.set_data.argtypes = [byte_ptr, ctypes.c_long, ctypes.c_long]
        self.set_data.restype = ctypes.c_bool
        self.get_data.argtypes = [byte_ptr, ctypes.POINTER(ctypes.c_long)]
        self.get_data.restype = ctypes.c_long
        self.clear_data.argtypes = []
        self.clear_data.restype = ctypes.c_bool
        if not self.clear_data():
            raise RuntimeError(f"{side}: cannot clear endpoint receive cache")

    def send(self, can_id, payload):
        if not 0 <= can_id <= 0x7FF or not 0 < len(payload) <= 8:
            raise ValueError("standard CAN ID and 1..8 payload bytes required")
        control = (can_id, bytes(payload))
        if can_id != 0x7FF and control == self.last_control:
            # Site endpoint suppresses identical consecutive payloads. Separate
            # holding/disable frames with a read, never dither a physical target.
            self.send(0x7FF, struct.pack("<HBB", self.motor_id, 0x33, 0x08))
            # The separator was accepted even if the following control write
            # is busy. Do not insert another read on every retry and starve it.
            self.last_control = None
        data = can_id.to_bytes(self.tx_width, "little") + payload
        buf = (ctypes.c_ubyte * 256)()
        buf[: len(data)] = data
        # Native endpoint sender shares one pending slot. It may be busy while
        # its I/O thread transmits the preceding frame (e.g. target then enable).
        deadline = time.monotonic() + 0.01
        if self.send_deadline is not None:
            deadline = min(deadline, self.send_deadline)
        while not self.set_data(buf, len(data), 1):
            if time.monotonic() >= deadline:
                raise TransportBusy(
                    f"Marvin {self.channel}: endpoint send busy/failed within cycle budget"
                )
            time.sleep(min(0.0005, max(0, deadline - time.monotonic())))
        if can_id != 0x7FF:
            self.last_control = control
        if self.trace is not None:
            self.trace.append(
                {"direction": "tx_sdk_accepted", "channel": 1, "hex": data.hex(" ")}
            )

    def recv(self):
        buf = (ctypes.c_ubyte * 256)()
        channel = ctypes.c_long(1)
        size = self.get_data(buf, ctypes.byref(channel))
        if size == 0:
            return None
        if not 0 < size <= 256:
            raise OSError(f"Marvin endpoint invalid receive length {size}")
        if self.trace is not None:
            self.trace.append(
                {
                    "direction": "rx",
                    "channel": channel.value,
                    "size": size,
                    "hex": bytes(buf[:size]).hex(" "),
                }
            )
        if channel.value != 1:
            return (-1, b"")
        raw = bytes(buf[:size])
        if len(raw) != self.rx_width + 8:
            raise RuntimeError(
                f"CAN reply layout mismatch: {raw.hex(' ')}; check rx_id_bytes with vendor"
            )
        cid = (
            int.from_bytes(raw[: self.rx_width], "little")
            if self.rx_width
            else self.master_id
        )
        if cid > 0x7FF:
            raise RuntimeError(f"not a standard CAN reply: {raw.hex(' ')}")
        return cid, raw[self.rx_width :]

    def close(self):
        # Robot owns the connection. Never release it from the gripper driver.
        pass


class RoutedDriver:
    """Each arm has its own endpoint bus, so both grippers may use motor ID 1."""

    def __init__(self, robot, config):
        self.drivers = {
            s: Driver(MarvinCAN(robot, s, m, config), {s: m})
            for s, m in config["motors"].items()
        }

    @property
    def feedback(self):
        return {s: d.feedback[s] for s, d in self.drivers.items() if s in d.feedback}

    @property
    def last_position_commands(self):
        return {s:d.last_position_commands[s] for s,d in self.drivers.items() if s in d.last_position_commands}

    @property
    def current_limit_reports(self):
        return {s: d.current_limit_reports[s] for s, d in self.drivers.items()
                if s in d.current_limit_reports}

    @property
    def feedback_timing(self):
        return {s: d.feedback_timing[s] for s, d in self.drivers.items()}

    def drain_feedback(self):
        return {s: d.drain_feedback()[s] for s, d in self.drivers.items()}

    def set_send_deadline(self, side, deadline):
        self.drivers[side].set_send_deadline(side, deadline)

    def preflight(self):
        for d in self.drivers.values():
            d.preflight()

    def poll(self):
        for d in self.drivers.values():
            d.poll()

    def enable(self, side, p):
        self.drivers[side].enable(side, p)

    def send(self, side, p):
        self.drivers[side].send(side, p)

    def send_position(self, side, p):
        self.drivers[side].send_position(side, p)

    def disable(self, side):
        self.drivers[side].disable(side)

    def close(self):
        errors = []
        for d in self.drivers.values():
            try:
                d.close()
            except Exception as error:
                errors.append(str(error))
        if errors:
            raise RuntimeError("; ".join(errors))


def inspect(robot, config):
    """Only read parameter requests (0x33), no disable/enable/motion/flash writes."""
    result = {}
    for s, m in config["motors"].items():
        bus = None
        report = {
            "ok": False,
            "arm_channel": robot.config["arm_channels"][s],
            "motor_id": m["motor_id"],
            "master_id": m["master_id"],
            "protocol": "DM-J4310-2EC common",
            "expected_can_bitrate_code": m.get("expected_can_bitrate_code"),
            "parameters": {},
            "motion_tested": False,
            "motion_precheck_issues": ["参数尚未完整读取"],
        }
        result[s] = report
        try:
            bus = MarvinCAN(robot, s, m, config, diagnostics=True)
            d = Driver(bus, {s: m})
            for name, rid, fmt in (
                ("mode", 0x0A, "I"),
                ("motor_id", 0x08, "I"),
                ("master_id", 0x07, "I"),
                ("can_bitrate_code", 0x23, "I"),
                ("timeout_ticks_50us", 0x09, "I"),
                ("pmax", 0x15, "f"),
                ("vmax", 0x16, "f"),
                ("tmax", 0x17, "f"),
                ("position_rad", 0x50, "f"),
                ("bus_voltage", 0x3C, "f"),
            ):
                report["parameters"][name] = d.register(s, rid, fmt, attempts=3)
            expected = m.get("expected_can_bitrate_code")
            if expected is not None and report["parameters"]["can_bitrate_code"] != expected:
                raise ValueError(f"{s}: measured can_br differs from configured expectation {expected}; "
                                 "configuration does not change endpoint or motor bitrate")
            report["ok"] = True
            p = report["parameters"]
            issues = []
            expected_mode = int(m.get("control_mode", 4))
            if p["mode"] != expected_mode:
                issues.append(f"CTRL_MODE当前为{p['mode']}，运行时将自动切换为{expected_mode}（易失，退出恢复）")
            if not m.get("watchdog_ticks") and not 1000 <= p["timeout_ticks_50us"] <= 6000:
                issues.append("TIMEOUT必须为1000..6000（50..300ms）或设置watchdog_ticks由程序写入")
            lo, hi = sorted((m["open_rad"], m["closed_rad"]))
            if not math.isfinite(p["position_rad"]) or not lo - 0.001 <= p["position_rad"] <= hi + 0.001:
                issues.append("实际位置超出配置行程；需要确认零点与开合角度")
            if not all(math.isfinite(p[k]) and p[k] > 0 for k in ("pmax", "vmax", "tmax")):
                issues.append("PMAX/VMAX/TMAX量程无效")
            elif max(abs(lo), abs(hi)) >= p["pmax"]:
                issues.append("配置行程超出PMAX")
            try:
                report["voltage_telemetry"] = voltage_telemetry(p["bus_voltage"])
                report["warnings"] = ([report["voltage_telemetry"]["warning"]]
                                      if report["voltage_telemetry"]["warning"] else [])
            except RuntimeError as error:
                issues.append(str(error))
            if config.get("endpoint_verified") is not True:
                issues.append("末端与重复保持命令/看门狗尚未完成现场验证")
            if m.get("commissioned") is not True:
                issues.append("本侧行程与夹持电流尚未完成标定")
            if max(abs(lo), abs(hi)) >= math.pi - 0.02:
                issues.append("标定超出上电单圈安全范围，不能直接跨断电复用")
            if "max_current_a" in m:
                try:
                    p.update(d.read_current_limit(s))
                except ValueError as error:
                    issues.append(str(error))
            report["motion_precheck_issues"] = issues
        except (OSError, ValueError, RuntimeError) as error:
            report["error"] = str(error)
        finally:
            report["transport_trace"] = list(bus.trace) if bus is not None else []
        # No Driver.close(): that method sends disable; this path is read-only.
    return result


def scan_ids(robot, config, start=0, end=255, progress=None, wait_s=0.15, passes=2):
    """Enumerate explicit ESC IDs using only 0x7ff / 0x33 / CTRL_MODE reads.

    Accept any reply arbitration ID, but require the echoed queried ESC ID,
    function, register and a valid mode. Never change configuration automatically.
    """
    import struct

    if (
        not 0 <= start <= end <= 0x7FF
        or not 0.01 <= wait_s <= 1
        or not 1 <= passes <= 3
    ):
        raise ValueError("invalid scan range/timing")
    result = {}
    for side, motor in config["motors"].items():
        report = {
            "arm_channel": robot.config["arm_channels"][side],
            "range": [start, end],
            "found": [],
            "requests_sent": 0,
            "rx_packets": 0,
            "completed": False,
        }
        result[side] = report
        bus, queried, found = None, set(), {}
        try:
            bus = MarvinCAN(robot, side, motor, config, diagnostics=True)
            for attempt in range(passes):
                for motor_id in range(start, end + 1):
                    if progress and (motor_id == start or (motor_id - start) % 16 == 0):
                        progress(
                            f"{side}/{bus.channel}：第 {attempt + 1}/{passes} 轮，扫描 ID {motor_id}～{min(end, motor_id + 15)}"
                        )
                    bus.send(0x7FF, struct.pack("<HBB", motor_id, 0x33, 0x0A))
                    queried.add(motor_id)
                    report["requests_sent"] += 1
                    deadline = time.monotonic() + wait_s
                    while time.monotonic() < deadline:
                        packet = bus.recv()
                        if packet is None:
                            time.sleep(0.001)
                            continue
                        report["rx_packets"] += 1
                        cid, data = packet
                        if cid < 0 or len(data) != 8 or data[2:4] != b"\x33\x0a":
                            continue
                        echoed = int.from_bytes(data[:2], "little")
                        mode = struct.unpack("<I", data[4:])[0]
                        if echoed not in queried or mode not in range(5):
                            continue
                        master = cid if config["rx_id_bytes"] else None
                        key = (echoed, master)
                        if key not in found:
                            found[key] = {
                                "motor_id": echoed,
                                "master_id": master,
                                "mode": mode,
                                "replies": 0,
                                "teleop_id_supported": 1 <= echoed <= 15,
                            }
                            if progress:
                                progress(
                                    f"{side} 发现电机 ID={echoed}，Master ID={master}，模式={mode}"
                                )
                        found[key]["replies"] += 1
            report["completed"] = True
        except (OSError, ValueError, RuntimeError) as error:
            report["error"] = str(error)
        finally:
            report["found"] = list(found.values())
            report["transport_trace_tail"] = list(bus.trace) if bus is not None else []
        # Do not call Driver.close(): scanning must not send disable commands.
    return result
