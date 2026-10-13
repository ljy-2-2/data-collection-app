#!/usr/bin/env python3
"""Repack with private libraries/tools and the host desktop keyring service."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
APP = "xiaokai-lab-data-collection"
PREFIX = Path("/opt") / APP


def run(*args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def elf_type(path, segment):
    try:
        with path.open("rb") as handle:
            header = handle.read(64)
            if header[:6] != b"\x7fELF\x02\x01":
                return False
            offset = struct.unpack_from("<Q", header, 32)[0]
            size, count = struct.unpack_from("<HH", header, 54)
            for i in range(count):
                handle.seek(offset + i * size)
                if handle.read(4) == struct.pack("<I", segment):
                    return True
    except (OSError, struct.error):
        pass
    return False


def extract_private(deb, root):
    """Keep payloads only; never run dependency maintainer scripts or setuid files."""
    proc = subprocess.Popen(["dpkg-deb", "--fsys-tarfile", str(deb)], stdout=subprocess.PIPE)
    delayed = []
    with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
        for member in archive:
            name = member.name.removeprefix("./")
            if not (name.split("/")[0] in {"bin", "sbin", "lib", "lib64", "usr"}
                    or name.startswith(("etc/fonts/", "etc/ssl/"))):
                continue
            target = root / name
            if not target.resolve().is_relative_to(root):
                raise ValueError(f"Escaping dependency path: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(exist_ok=True)
            elif member.issym():
                if target.is_symlink() or target.is_file():
                    target.unlink()
                link = member.linkname
                if link.startswith("/"):
                    link = os.path.relpath(root / link.lstrip("/"), target.parent)
                if not (target.parent / link).resolve().is_relative_to(root):
                    raise ValueError(f"Escaping dependency link: {name}")
                target.symlink_to(link)
            elif member.islnk():
                delayed.append((target, root / member.linkname.removeprefix("./")))
            elif member.isfile():
                if target.is_symlink():
                    target.unlink()
                with archive.extractfile(member) as source, target.open("wb") as dest:
                    shutil.copyfileobj(source, dest)
                target.chmod(member.mode & 0o777)
    if proc.wait():
        raise RuntimeError(f"Cannot extract {deb}")
    for target, source in delayed:
        if not source.resolve().is_relative_to(root):
            raise ValueError("Escaping dependency hardlink")
        if target.exists() or target.is_symlink():
            target.unlink()
        shutil.copy2(source, target)


def private_libs():
    root = PREFIX / "runtime/sysroot"
    return ":".join(str(root / part) for part in (
        "lib/x86_64-linux-gnu", "usr/lib/x86_64-linux-gnu",
        "usr/lib/x86_64-linux-gnu/android", "usr/lib/android", "lib", "usr/lib"))


def patch_binaries(folder, patchelf):
    count = 0
    for path in folder.rglob("*"):
        if not path.is_file() or path.is_symlink() or not elf_type(path, 2):
            continue
        if path.name in {"libc.so.6", "ld-linux-x86-64.so.2"} or path.name.startswith(("ld-2.", "libc-2.")):
            continue
        old = subprocess.check_output([str(patchelf), "--print-rpath", str(path)], text=True).strip()
        old = ":".join(str(PREFIX / "runtime/sysroot") + item if item.startswith(("/lib", "/usr/lib")) else item
                       for item in old.split(":"))
        options = ["--force-rpath", "--set-rpath", ":".join(filter(None, [old, private_libs()]))]
        if elf_type(path, 3):
            options += ["--set-interpreter", PREFIX / "runtime/sysroot/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"]
        run(patchelf, *options, path)
        count += 1
    print(f"已配置 {count} 个 ELF 程序的私有加载路径", flush=True)


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


def write_private_container_runtime(stage):
    """Write relocatable package inputs without rebuilding/copying the sysroot.

    Keep the previous dockerd-managed containerd root and namespace so existing
    layers remain visible. An explicit socket prevents dockerd from choosing the
    host's containerd depending on service startup order.
    """
    stage = Path(stage)
    package = stage / str(PREFIX).lstrip("/")
    runtime = package / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    socket = "/run/xiaokai-collection/exec/containerd/containerd.sock"
    (runtime / "docker-daemon.json").write_text(json.dumps({
        "hosts": ["unix:///run/xiaokai-collection/docker.sock"], "group": "xiaokai-collection",
        "data-root": "/var/lib/xiaokai-lab-data-collection/docker", "exec-root": "/run/xiaokai-collection/exec",
        "containerd": socket,
        "containerd-namespace": "moby", "containerd-plugins-namespace": "plugins.moby",
        "pidfile": "/run/xiaokai-collection/docker.pid", "bridge": "none", "iptables": False,
        "ip6tables": False, "ip-forward": False, "ip-masq": False, "userland-proxy": False,
        "runtimes": {"xiaokai-runc": {"path": str(PREFIX / "runtime/bin/runc")}},
        "default-runtime": "xiaokai-runc", "log-driver": "local",
    }, indent=2))
    # Match Docker's managed containerd layout and disabled CRI plugin. Avoid
    # importing the host's /etc/containerd configuration into this private store.
    (runtime / "containerd.toml").write_text(
        'version = 2\n'
        'root = "/var/lib/xiaokai-lab-data-collection/docker/containerd/daemon"\n'
        'state = "/run/xiaokai-collection/exec/containerd/daemon"\n'
        'imports = []\n'
        'disabled_plugins = ["io.containerd.grpc.v1.cri"]\n'
        '[grpc]\n'
        f'  address = "{socket}"\n'
        '  max_recv_message_size = 16777216\n'
        '  max_send_message_size = 16777216\n')
    units = stage / "usr/lib/systemd/system"
    units.mkdir(parents=True, exist_ok=True)
    common = f"Environment=PATH={PREFIX}/runtime/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
    (units / "xiaokai-collection-containerd.service").write_text(
        "[Unit]\nDescription=Xiaokai private collection containerd\nAfter=local-fs.target\n"
        "Before=xiaokai-collection-docker.service\n"
        "[Service]\nType=notify\n" + common +
        f"ExecStart={PREFIX}/runtime/bin/containerd --config {PREFIX}/runtime/containerd.toml\n"
        # Only containerd owns this parent. A dockerd-only restart must never
        # remove its socket or shim state; preserve them for containerd recovery.
        "RuntimeDirectory=xiaokai-collection\nRuntimeDirectoryMode=0755\n"
        "RuntimeDirectoryPreserve=yes\nDelegate=yes\nKillMode=process\n"
        "Restart=always\nRestartSec=2\nTimeoutStartSec=120\nTimeoutStopSec=60\n"
        "[Install]\nWantedBy=multi-user.target\n")
    (units / "xiaokai-collection-docker.service").write_text(
        "[Unit]\nDescription=Xiaokai private collection container runtime\n"
        "Requires=xiaokai-collection-containerd.service\n"
        "After=network.target xiaokai-collection-containerd.service\n"
        "[Service]\nType=notify\nNotifyAccess=all\n" + common +
        f"ExecStart={PREFIX}/runtime/bin/dockerd --config-file={PREFIX}/runtime/docker-daemon.json\n"
        f"ExecStartPost={PREFIX}/runtime/python/bin/python3.12 {PREFIX}/scripts/setup_self_contained.py --load-image\n"
        "Delegate=yes\nKillMode=process\nRestart=on-failure\nTimeoutStartSec=600\nTimeoutStopSec=60\n"
        "[Install]\nWantedBy=multi-user.target\n")
    executable(stage / "DEBIAN/postinst", f'#!/bin/sh\nset -eu\nif [ "$1" = configure ]; then\n. {PREFIX}/runtime/env.sh\n{PREFIX}/runtime/python/bin/python3.12 {PREFIX}/scripts/setup_self_contained.py\nfi\n')
    executable(stage / "DEBIAN/prerm", '#!/bin/sh\nset -eu\nif [ "$1" = remove ] || [ "$1" = upgrade ]; then\nif [ -d /run/systemd/system ]; then\nsystemctl stop xiaokai-collection-docker.service xiaokai-collection-containerd.service xiaokai-collection-avahi.service\nfi\nfi\n')
    executable(stage / "DEBIAN/postrm", '#!/bin/sh\nset -eu\nif [ "$1" = remove ] || [ "$1" = purge ]; then\nrm -f /etc/systemd/system/multi-user.target.wants/xiaokai-collection-docker.service /etc/systemd/system/multi-user.target.wants/xiaokai-collection-containerd.service /etc/systemd/system/multi-user.target.wants/xiaokai-collection-avahi.service\nif [ -d /run/systemd/system ]; then systemctl daemon-reload; fi\nfi\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deb", type=Path)
    dependencies = parser.add_mutually_exclusive_group(required=True)
    dependencies.add_argument("--offline-bundle", type=Path)
    dependencies.add_argument("--sysroot", type=Path,
                              help="Reuse a complete private sysroot from a previously installed standalone package")
    parser.add_argument("--patchelf", type=Path, required=True)
    parser.add_argument("--docker-static", type=Path, required=True)
    parser.add_argument("--compose-static", type=Path, required=True)
    parser.add_argument("--gdk-sdk-root", type=Path, required=True)
    parser.add_argument("--robot-python-site", type=Path, action="append", required=True)
    parser.add_argument("--robot-native-dir", type=Path, action="append", default=[])
    parser.add_argument("--xr-service-root", type=Path, required=True)
    parser.add_argument("--xr-sdk-module", type=Path, required=True)
    parser.add_argument("--version", default="0.1.0+" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + ".standalone")
    args = parser.parse_args()
    robot_inputs = (args.gdk_sdk_root, args.robot_python_site, args.xr_service_root, args.xr_sdk_module)
    if any(robot_inputs) and not all(robot_inputs):
        parser.error("Full robot runtime requires G2 SDK, Python 3.10 packages, XR service and XR binding")
    patchelf = args.patchelf.resolve(strict=True)
    cache = ROOT / ".build-cache/self-contained"
    cache.mkdir(parents=True, exist_ok=True)
    if args.sysroot:
        sysroot = args.sysroot.resolve(strict=True)
        for relative in ("lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
                         "usr/bin/adb", "usr/bin/v4l2-ctl",
                         "usr/share/glib-2.0/schemas/gschemas.compiled"):
            if not (sysroot / relative).is_file():
                parser.error(f"Incomplete private sysroot: {relative}")
        if sysroot.is_relative_to(cache / "stage"):
            parser.error("The sysroot source cannot be inside the staging directory")
        print(f"复用完整私有依赖（保留符号链接）：{sysroot}", flush=True)
    else:
        repo = args.offline_bundle.resolve() / "repo"
        run("sha256sum", "--check", "--quiet", "SHA256SUMS", cwd=repo.parent)
        sysroot = cache / "sysroot"
        stamp = cache / "sysroot.ready"
        fingerprint = hashlib.sha256((repo.parent / "SHA256SUMS").read_bytes() + b"private-elf-v2").hexdigest()
        if not stamp.exists() or stamp.read_text() != fingerprint:
            shutil.rmtree(sysroot, ignore_errors=True)
            sysroot.mkdir()
            dependencies = sorted(p for p in repo.glob("*.deb") if not p.name.startswith(APP + "_"))
            for i, deb in enumerate(dependencies, 1):
                extract_private(deb, sysroot)
                if i % 50 == 0:
                    print(f"已内置 {i}/{len(dependencies)} 个组件", flush=True)
            patch_binaries(sysroot, patchelf)
            schemas = sysroot / "usr/share/glib-2.0/schemas"
            run("glib-compile-schemas", schemas)
            stamp.write_text(fingerprint)
    stage = cache / "stage"
    shutil.rmtree(stage, ignore_errors=True)
    run("dpkg-deb", "--raw-extract", args.deb.resolve(), stage)
    package = stage / str(PREFIX).lstrip("/")
    shutil.copytree(sysroot, package / "runtime/sysroot", symlinks=True)
    # Use unmodified upstream static Go programs. Relocating Go's ELF dynamic
    # sections can invalidate runtime metadata, especially runc re-execution.
    with tarfile.open(args.docker_static) as archive:
        for member in archive:
            if not member.isfile():
                continue
            name = Path(member.name).name
            if name not in {"docker", "dockerd", "docker-init", "docker-proxy", "containerd", "containerd-shim-runc-v2", "ctr", "runc"}:
                raise ValueError(f"Unexpected static runtime member {member.name}")
            target = package / "runtime/sysroot/usr/bin" / name
            with archive.extractfile(member) as source, target.open("wb") as dest:
                shutil.copyfileobj(source, dest)
            target.chmod(0o755)
            if elf_type(target, 3):
                raise ValueError(f"Docker component is not static: {name}")
    plugins = package / "runtime/sysroot/usr/libexec/docker/cli-plugins"
    shutil.rmtree(plugins)
    plugins.mkdir(parents=True)
    shutil.copy2(args.compose_static, plugins / "docker-compose")
    (plugins / "docker-compose").chmod(0o755)
    if elf_type(plugins / "docker-compose", 3):
        raise ValueError("Compose is not static")
    shutil.copy2(package / "runtime/sysroot/usr/bin/runc", package / "runtime/sysroot/usr/sbin/runc")
    patch_binaries(package / "runtime/python", patchelf)
    if all(robot_inputs):
        from bundle_robot_runtime import bundle_robot_runtime
        bundle_robot_runtime(package, sdk_source=args.gdk_sdk_root,
                             python_sites=args.robot_python_site,
                             extra_native_dirs=args.robot_native_dir,
                             service_root=args.xr_service_root, sdk_module=args.xr_sdk_module,
                             patchelf=patchelf, elf_type=elf_type, private_libs=private_libs())
    for name in ("setup_self_contained.py", "package_launcher.py"):
        shutil.copy2(ROOT / "scripts" / name, package / "scripts" / name)
    shutil.copy2(ROOT / "scripts/self_contained_env.sh", package / "runtime/env.sh")
    shutil.copy2(ROOT / "teleop_control/src/teleop_control/login_credentials.py",
                 package / "app/teleop_control/src/teleop_control/login_credentials.py")
    (package / "self-contained.json").write_text(json.dumps({
        "version": args.version, "base": "Ubuntu 22.04 amd64",
        "docker_static_sha256": hashlib.file_digest(args.docker_static.open("rb"), "sha256").hexdigest(),
        "compose_static_sha256": hashlib.file_digest(args.compose_static.open("rb"), "sha256").hexdigest(),
    }, indent=2))
    (package / "package-build.json").write_text(json.dumps({"version": args.version, "build_id": args.version}))
    binaries = package / "runtime/bin"
    binaries.mkdir(exist_ok=True)
    for name in ("dockerd", "containerd", "containerd-shim-runc-v2", "runc", "docker-init", "docker-proxy",
                 "adb", "curl", "lsusb", "v4l2-ctl", "udevadm", "avahi-daemon", "zenity", "ip", "ss"):
        matches = [sysroot / prefix / name for prefix in ("usr/bin", "usr/sbin", "bin", "sbin")]
        source = next((p for p in matches if p.exists()), None)
        if source is None:
            raise RuntimeError(f"缺少独立组件 {name}")
        (binaries / name).symlink_to(Path("../sysroot") / source.relative_to(sysroot))
    executable(binaries / "docker", '#!/bin/sh\nexec ' + str(PREFIX) + '/runtime/sysroot/usr/bin/docker --host unix:///run/xiaokai-collection/docker.sock --config ' + str(PREFIX) + '/runtime/docker-cli "$@"\n')
    cli = package / "runtime/docker-cli"
    cli.mkdir()
    (cli / "config.json").write_text(json.dumps({"cliPluginsExtraDirs": [str(PREFIX / "runtime/sysroot/usr/libexec/docker/cli-plugins")]}))
    write_private_container_runtime(stage)
    (package / "runtime/avahi-daemon.conf").write_text("[server]\nenable-dbus=no\n[wide-area]\nenable-wide-area=no\n")
    (package / "runtime/fonts.conf").write_text('<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig><dir>' + str(PREFIX / 'runtime/sysroot/usr/share/fonts') + '</dir><dir>/usr/share/fonts</dir><cachedir prefix="xdg">fontconfig</cachedir></fontconfig>')
    compose_path = package / "app/umi_collection/vendor/galaxis/docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text())
    compose["services"]["config-init"]["network_mode"] = "none"
    compose_path.write_text(yaml.safe_dump(compose, allow_unicode=True, sort_keys=False))
    rules = stage / "usr/lib/udev/rules.d/70-xiaokai-android.rules"
    rules.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sysroot / "lib/udev/rules.d/51-android.rules", rules)
    units = stage / "usr/lib/systemd/system"
    units.mkdir(parents=True, exist_ok=True)
    common = f"Environment=PATH={PREFIX}/runtime/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
    (units / "xiaokai-collection-avahi.service").write_text(
        "[Unit]\nDescription=Xiaokai collection mDNS discovery\nAfter=network.target\n"
        "[Service]\nType=simple\n" + common +
        f"ExecStart={PREFIX}/runtime/bin/avahi-daemon --no-chroot --file={PREFIX}/runtime/avahi-daemon.conf\n"
        "Restart=on-failure\n[Install]\nWantedBy=multi-user.target\n")
    executable(stage / "usr/bin" / APP,
               f'#!/bin/sh\n. {PREFIX}/runtime/env.sh\nexec {PREFIX}/runtime/python/bin/python3.12 {PREFIX}/scripts/package_launcher.py "$@"\n')
    size = sum(p.stat().st_size for p in package.rglob("*") if p.is_file()) // 1024
    (stage / "DEBIAN/control").write_text(
        f"Package: {APP}\nVersion: {args.version}\nSection: utils\nPriority: optional\nArchitecture: amd64\n"
        f"Maintainer: Xiaokai Lab <support@localhost>\nInstalled-Size: {size}\n"
        "Depends: gnome-keyring, libpam-gnome-keyring, dbus-user-session\n"
        "Description: Xiaokai self-contained cloud data collection desktop\n"
        " Private Python, Qt, graphics libraries, device tools and collection daemon.\n")
    # A source deb may contain generated checksums that no longer describe this repack.
    (stage / "DEBIAN/md5sums").unlink(missing_ok=True)
    artifact = ROOT / "dist-packages" / f"{APP}_{args.version}_amd64.deb"
    run("dpkg-deb", "--root-owner-group", "-Zzstd", "-z6", "--build", stage, artifact)
    with artifact.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    artifact.with_suffix(".deb.sha256").write_text(f"{digest}  {artifact.name}\n")
    print(f"生成完成：{artifact}", flush=True)


if __name__ == "__main__":
    main()
