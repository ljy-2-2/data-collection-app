"""Keep wired Kuavo traffic isolated from the host Wi-Fi/VPN network.

This module only manages Linux networking.  It never imports ROS or a robot
SDK and never sends a robot command.
"""

from __future__ import annotations

import argparse
import os
import pwd
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class KuavoNetworkConfig:
    namespace: str = "kuavons"
    parent_interface: str = "eno1"
    macvlan_interface: str = "kuavomac"
    namespace_cidr: str = "192.168.26.250/24"
    robot_ip: str = "192.168.26.1"
    ssh_port: int = 22
    ownership_alias: str = "kuavo-teleop-managed"

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "KuavoNetworkConfig":
        env = os.environ if environment is None else environment
        raw_ssh_port = env.get("KUAVO_SSH_PORT", str(cls.ssh_port))
        try:
            ssh_port = int(raw_ssh_port)
        except ValueError as error:
            raise ValueError("KUAVO_SSH_PORT must be an integer from 1 to 65535.") from error
        if not 1 <= ssh_port <= 65535:
            raise ValueError("KUAVO_SSH_PORT must be an integer from 1 to 65535.")
        return cls(
            namespace=env.get("KUAVO_NETNS", cls.namespace),
            parent_interface=env.get("KUAVO_PARENT_INTERFACE", cls.parent_interface),
            macvlan_interface=env.get("KUAVO_MACVLAN_INTERFACE", cls.macvlan_interface),
            namespace_cidr=env.get("KUAVO_NETNS_CIDR", cls.namespace_cidr),
            robot_ip=env.get("KUAVO_IP", cls.robot_ip),
            ssh_port=ssh_port,
        )


def _inside(config: KuavoNetworkConfig, *command: str) -> list[str]:
    return ["ip", "netns", "exec", config.namespace, *command]


def build_up_commands(config: KuavoNetworkConfig) -> list[list[str]]:
    return [
        ["ip", "netns", "add", config.namespace],
        [
            "ip", "link", "add", config.macvlan_interface,
            "link", config.parent_interface, "type", "macvlan", "mode", "bridge",
        ],
        ["ip", "link", "set", config.macvlan_interface, "netns", config.namespace],
        _inside(config, "ip", "link", "set", "lo", "up"),
        _inside(
            config, "ip", "addr", "replace", config.namespace_cidr,
            "dev", config.macvlan_interface,
        ),
        _inside(config, "ip", "link", "set", config.macvlan_interface, "up"),
        _inside(
            config, "ip", "link", "set", "dev", config.macvlan_interface,
            "alias", config.ownership_alias,
        ),
    ]


def build_down_commands(config: KuavoNetworkConfig) -> list[list[str]]:
    return [
        ["ip", "netns", "del", config.namespace],
        ["ip", "link", "del", config.macvlan_interface],
    ]


def build_run_command(
    config: KuavoNetworkConfig,
    *,
    invoking_user: str,
    command: Sequence[str],
    environment: Mapping[str, str],
) -> list[str]:
    user_info = pwd.getpwnam(invoking_user)
    child_environment = {
        "HOME": user_info.pw_dir,
        "USER": invoking_user,
        "LOGNAME": invoking_user,
        "KUAVO_ROBOT_IP": config.robot_ip,
    }
    for name in ("PATH", "PYTHONPATH", "ROS_MASTER_URI", "ROS_IP", "ROS_HOSTNAME"):
        if environment.get(name):
            child_environment[name] = environment[name]
    return _inside(
        config,
        "sudo", "-H", "-u", invoking_user, "env",
        *(f"{name}={value}" for name, value in child_environment.items()),
        *command,
    )


class CommandRunner:
    def __init__(self, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    def run(
        self,
        command: Sequence[str],
        *,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        # A real child may emit binary video on stdout. Keep tracing separate;
        # dry-run stdout remains the human-readable command plan.
        print(f"+ {shlex.join(command)}",
              file=sys.stdout if self.dry_run else sys.stderr, flush=True)
        if self.dry_run:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.run(
            list(command), check=check, capture_output=capture_output, text=True
        )


def _require_root() -> None:
    if os.geteuid() != 0:
        raise PermissionError("This command manages a network namespace; run it with sudo.")


def _namespace_exists(config: KuavoNetworkConfig, runner: CommandRunner) -> bool:
    result = runner.run(["ip", "netns", "list"], capture_output=True)
    return any(
        line.split(maxsplit=1)[0] == config.namespace
        for line in result.stdout.splitlines()
        if line
    )


def _root_link_exists(interface: str, runner: CommandRunner) -> bool:
    result = runner.run(
        ["ip", "link", "show", "dev", interface], check=False, capture_output=True
    )
    return result.returncode == 0


def _link_is_owned(
    config: KuavoNetworkConfig,
    runner: CommandRunner,
    *,
    inside_namespace: bool,
) -> bool:
    command = ["ip", "-d", "link", "show", "dev", config.macvlan_interface]
    if inside_namespace:
        command = _inside(config, *command)
    result = runner.run(command, check=False, capture_output=True)
    return (
        result.returncode == 0
        and f"alias {config.ownership_alias}" in result.stdout
    )


def _show_status(config: KuavoNetworkConfig, runner: CommandRunner) -> int:
    runner.run(
        ["ip", "-br", "link", "show", "dev", config.parent_interface],
        check=False,
    )
    if not _namespace_exists(config, runner):
        print(f"Kuavo namespace {config.namespace!r} is not configured.")
        return 1
    if not _link_is_owned(config, runner, inside_namespace=True):
        print(
            f"ERROR: namespace {config.namespace!r} is not owned by this tool.",
            file=sys.stderr,
        )
        return 2
    runner.run(_inside(config, "ip", "-br", "addr"), check=False)
    runner.run(_inside(config, "ip", "route"), check=False)
    print(
        "KUAVO_NETWORK_STATUS configured=true robot_commands_sent=false "
        "host_routes_changed=false"
    )
    return 0


def _address_is_available(config: KuavoNetworkConfig, runner: CommandRunner) -> bool:
    namespace_ip = config.namespace_cidr.split("/", maxsplit=1)[0]
    result = runner.run(
        [
            "busybox", "arping", "-D", "-I", config.parent_interface,
            "-c", "3", "-w", "3", namespace_ip,
        ],
        check=False,
    )
    return result.returncode == 0


def _cleanup_partial(config: KuavoNetworkConfig, runner: CommandRunner) -> None:
    if _namespace_exists(config, runner):
        runner.run(["ip", "netns", "del", config.namespace], check=False)
    if _root_link_exists(config.macvlan_interface, runner):
        runner.run(["ip", "link", "del", config.macvlan_interface], check=False)


def _check_port(value: int | None) -> int | None:
    if value is not None and (type(value) is not int or not 1 <= value <= 65535):
        raise ValueError("check port must be an integer from 1 to 65535")
    return value


def _test(config: KuavoNetworkConfig, runner: CommandRunner,
          *, check_port: int | None = None) -> int:
    _check_port(check_port)
    port = config.ssh_port if check_port is None else check_port
    if not runner.dry_run:
        _require_root()
        if not _namespace_exists(config, runner):
            print("Kuavo network is not configured; run 'up' first.", file=sys.stderr)
            return 1
        if not _link_is_owned(config, runner, inside_namespace=True):
            print("Refusing to use an unowned Kuavo namespace.", file=sys.stderr)
            return 1

    arp = runner.run(
        _inside(
            config, "busybox", "arping", "-I", config.macvlan_interface,
            "-c", "3", "-w", "3", config.robot_ip,
        ),
        check=False,
    )
    ping = runner.run(
        _inside(
            config, "ping", "-I", config.macvlan_interface,
            "-c", "2", "-W", "1", config.robot_ip,
        ),
        check=False,
    )
    tcp = runner.run(
        _inside(
            config, "nc", "-zv", "-w", "3",
            config.robot_ip, str(port),
        ),
        check=False,
    )
    if runner.dry_run:
        return 0
    if arp.returncode != 0:
        print(
            "Kuavo did not answer ARP; check the body Ethernet cable and port.",
            file=sys.stderr,
        )
        return 2
    if tcp.returncode != 0:
        print(
            (f"Kuavo Ethernet is reachable, but SSH is not ready on port {port}."
             if check_port is None else
             f"Kuavo Ethernet is reachable, but the TCP service is not ready on port {port}."),
            file=sys.stderr,
        )
        return 3
    port_status = "ssh=true" if check_port is None else f"tcp_port={port} tcp=true"
    print(
        "KUAVO_NETWORK_READY arp=true "
        f"ping={'true' if ping.returncode == 0 else 'blocked'} {port_status} "
        "host_routes_changed=false robot_commands_sent=false"
    )
    return 0


def _up(config: KuavoNetworkConfig, runner: CommandRunner,
        *, check_port: int | None = None) -> int:
    _check_port(check_port)
    if runner.dry_run:
        for command in build_up_commands(config):
            runner.run(command)
        return _test(config, runner, check_port=check_port)

    _require_root()
    parent = runner.run(
        ["ip", "-br", "link", "show", "dev", config.parent_interface],
        capture_output=True,
    )
    if "LOWER_UP" not in parent.stdout:
        raise RuntimeError(
            f"{config.parent_interface} has no cable link; no network changes were made."
        )
    if _namespace_exists(config, runner):
        if not _link_is_owned(config, runner, inside_namespace=True):
            raise RuntimeError(
                f"Namespace {config.namespace!r} exists but is not owned by this tool."
            )
        runner.run(
            _inside(
                config, "ip", "addr", "replace", config.namespace_cidr,
                "dev", config.macvlan_interface,
            )
        )
        runner.run(
            _inside(config, "ip", "link", "set", config.macvlan_interface, "up")
        )
        print("KUAVO_NETWORK_REUSED owned=true")
        return _test(config, runner, check_port=check_port)
    if _root_link_exists(config.macvlan_interface, runner):
        raise RuntimeError(
            f"Root interface {config.macvlan_interface!r} already exists; refusing to overwrite it."
        )
    if not _address_is_available(config, runner):
        raise RuntimeError(
            f"Address {config.namespace_cidr} is already in use; nothing was changed."
        )
    try:
        for command in build_up_commands(config):
            runner.run(command)
    except (OSError, subprocess.CalledProcessError):
        print("Setup failed; removing only resources created by this tool.", file=sys.stderr)
        _cleanup_partial(config, runner)
        raise
    print(
        "KUAVO_NETWORK_CONFIGURED host_routes_changed=false "
        "wifi_vpn_unchanged=true robot_commands_sent=false"
    )
    return _test(config, runner, check_port=check_port)


def _down(config: KuavoNetworkConfig, runner: CommandRunner) -> int:
    if runner.dry_run:
        for command in build_down_commands(config):
            runner.run(command)
        return 0
    _require_root()
    namespace_exists = _namespace_exists(config, runner)
    root_link_exists = _root_link_exists(config.macvlan_interface, runner)
    if namespace_exists and not _link_is_owned(config, runner, inside_namespace=True):
        raise RuntimeError("Refusing to delete an unowned Kuavo namespace.")
    if root_link_exists and not _link_is_owned(
        config, runner, inside_namespace=False
    ):
        raise RuntimeError("Refusing to delete an unowned root interface.")
    if namespace_exists:
        runner.run(["ip", "netns", "del", config.namespace])
    if root_link_exists and _root_link_exists(config.macvlan_interface, runner):
        runner.run(["ip", "link", "del", config.macvlan_interface])
    print(
        "KUAVO_NETWORK_REMOVED robot_settings_changed=false"
        if namespace_exists or root_link_exists
        else "KUAVO_NETWORK_NOT_CONFIGURED"
    )
    return 0


def _run_command(
    config: KuavoNetworkConfig,
    runner: CommandRunner,
    command: Sequence[str],
    *,
    requested_user: str | None,
) -> int:
    if not command:
        raise ValueError("'run' needs a command after '--'.")
    invoking_user = (
        requested_user
        or os.environ.get("SUDO_USER")
        or (os.environ.get("USER") if runner.dry_run else None)
    )
    if not invoking_user or invoking_user == "root":
        raise RuntimeError("Invoke this tool with sudo from your normal user.")
    if not runner.dry_run:
        _require_root()
        if not _namespace_exists(config, runner):
            raise RuntimeError("Kuavo namespace does not exist; run 'up' first.")
        if not _link_is_owned(config, runner, inside_namespace=True):
            raise RuntimeError("Refusing to enter an unowned Kuavo namespace.")
    result = runner.run(
        build_run_command(
            config,
            invoking_user=invoking_user,
            command=command,
            environment=os.environ,
        ),
        check=False,
    )
    return result.returncode


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Isolate Kuavo body Ethernet without changing host VPN routes."
    )
    parser.add_argument("--dry-run", action="store_true")
    subparsers = parser.add_subparsers(dest="action", required=True)
    up_parser = subparsers.add_parser("up", help="create/reuse kuavons and test connectivity")
    up_parser.add_argument("--check-port", type=int,
                          help="check this TCP port instead of the default SSH port")
    subparsers.add_parser("status", help="show the isolated network")
    test_parser = subparsers.add_parser("test", help="check ARP, ping and a TCP port (default SSH)")
    test_parser.add_argument("--check-port", type=int,
                            help="check this TCP port instead of the default SSH port")
    run_parser = subparsers.add_parser("run", help="run a command inside kuavons")
    run_parser.add_argument("--user")
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    subparsers.add_parser("down", help="remove only owned kuavons resources")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    runner = CommandRunner(dry_run=args.dry_run)
    try:
        config = KuavoNetworkConfig.from_environment()
        if args.action == "up":
            return _up(config, runner, check_port=args.check_port)
        if args.action == "status":
            if not args.dry_run:
                _require_root()
            return _show_status(config, runner)
        if args.action == "test":
            return _test(config, runner, check_port=args.check_port)
        if args.action == "down":
            return _down(config, runner)
        if args.action == "run":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            return _run_command(
                config, runner, command, requested_user=args.user
            )
    except (
        OSError,
        PermissionError,
        RuntimeError,
        ValueError,
        subprocess.CalledProcessError,
        KeyError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
