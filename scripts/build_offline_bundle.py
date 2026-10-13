#!/usr/bin/env python3
"""Download a full Ubuntu dependency closure, without using host package state."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
import hashlib
import gzip
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
RELEASES = {"22.04": "jammy", "24.04": "noble"}


def run(*args, **kwargs):
    print(shlex.join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def apt_options(state: Path, archives: Path) -> list[str]:
    # Empty status is deliberate: the build host's installed dependencies must
    # never be silently omitted from a bundle intended for another computer.
    values = {
        "Dir::Etc::sourcelist": state / "sources.list",
        "Dir::Etc::sourceparts": state / "empty",
        "Dir::Etc::main": state / "empty.conf",
        "Dir::Etc::parts": state / "empty",
        "Dir::Etc::preferences": state / "empty.conf",
        "Dir::Etc::preferencesparts": state / "empty",
        "Dir::State": state,
        "Dir::State::status": state / "status",
        "Dir::State::lists": state / "lists",
        "Dir::Cache": state / "cache",
        "Dir::Cache::archives": archives,
        "Dir::Log": state / "log",
        "APT::Architecture": "amd64",
        "Acquire::Languages": "none",
        "Acquire::Retries": "3",
        "Acquire::http::Timeout": "30",
        "APT::Update::Error-Mode": "any",
        "APT::Install-Recommends": "true",
    }
    return [item for key, value in values.items() for item in ("-o", f"{key}={value}")]


def download_packages(options, env, state, repo, deb):
    plan = subprocess.check_output(
        ["apt-get", *options, "--print-uris", "--download-only", "--yes", "install", str(deb)],
        env=env, text=True,
    )
    # Read strong hashes from the Ubuntu-signed APT indexes. --print-uris on
    # Jammy prints only MD5; never use that as the download trust boundary.
    hashes = {}
    for index in (state / "lists").glob("*_binary-amd64_Packages*"):
        content = subprocess.check_output(["/usr/lib/apt/apt-helper", "cat-file", str(index)], text=True)
        for paragraph in content.split("\n\n"):
            fields = dict(line.split(": ", 1) for line in paragraph.splitlines()
                          if line.startswith(("Filename: ", "SHA256: ")))
            if "Filename" in fields and "SHA256" in fields:
                hashes[Path(fields["Filename"]).name] = fields["SHA256"]
    jobs = []
    for line in plan.splitlines():
        if not line.startswith("'"):
            continue
        uri, filename, size, *_ = shlex.split(line)
        if urlsplit(uri).scheme not in {"http", "https"}:
            continue
        assert Path(filename).name == filename
        digest = hashes[Path(unquote(urlsplit(uri).path)).name]
        jobs.append((uri, filename, int(size), digest))

    def fetch(job):
        uri, filename, size, digest = job
        temporary = repo / "partial" / filename
        candidates = [uri]
        if uri.startswith("http://archive.ubuntu.com/ubuntu/"):
            candidates.extend(uri.replace("http://archive.ubuntu.com/ubuntu", mirror, 1)
                              for mirror in ("https://mirrors.tuna.tsinghua.edu.cn/ubuntu",
                                             "https://mirrors.ustc.edu.cn/ubuntu"))
        for attempt in range(3):
            try:
                # Retain partial large downloads across interrupted connections.
                subprocess.run([
                    "curl", "--fail", "--location", "--silent", "--show-error",
                    "--noproxy", "*", "--retry", "4", "--retry-all-errors",
                    "--retry-delay", "1", "--connect-timeout", "10",
                    "--speed-time", "60", "--speed-limit", "1024",
                    "--continue-at", "-", "--output", str(temporary),
                    candidates[min(attempt, len(candidates) - 1)],
                ], check=True)
                with temporary.open("rb") as data:
                    actual = hashlib.file_digest(data, "sha256").hexdigest()
                if temporary.stat().st_size != size or actual != digest:
                    temporary.unlink()
                    raise ValueError(f"Ubuntu 包签名索引校验不匹配：{filename}")
                temporary.replace(repo / filename)
                print(f"已下载并校验：{filename}", flush=True)
                return
            except Exception as exc:
                print(f"重试下载 {filename}：{exc}", flush=True)
                if attempt == 2:
                    raise

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(fetch, jobs))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deb", type=Path)
    parser.add_argument("--ubuntu", required=True, choices=RELEASES)
    parser.add_argument("--mirror", default="http://archive.ubuntu.com/ubuntu")
    parser.add_argument("--output", type=Path, default=ROOT / "dist-packages")
    args = parser.parse_args()
    deb = args.deb.resolve(strict=True)
    metadata = subprocess.check_output(
        ["dpkg-deb", "-f", str(deb), "Package", "Version", "Architecture"], text=True
    )
    fields = dict(line.split(": ", 1) for line in metadata.splitlines())
    if fields["Architecture"] != "amd64":
        parser.error("当前应用 deb 仅支持 amd64")
    name = f"xiaokai-cloud-offline_{fields['Version']}_ubuntu{args.ubuntu}_amd64"
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    bundle = output / name
    repo = bundle / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    state = ROOT / ".build-cache" / f"offline-apt-{args.ubuntu}"
    for path in (state / "lists/partial", state / "cache", state / "log", state / "empty", repo / "partial"):
        path.mkdir(parents=True, exist_ok=True)
    (state / "status").write_text("")
    # APT loads its configuration before processing CLI -o arguments.
    # Bootstrap this first so host hooks and third-party config never execute.
    bootstrap = state / "apt.conf"
    (state / "empty.conf").touch()
    bootstrap.write_text(f'Dir::Etc::parts "{state}/empty";\nDir::Etc::main "{state}/empty.conf";\n')
    apt_env = {**os.environ, "APT_CONFIG": str(bootstrap)}
    codename = RELEASES[args.ubuntu]
    keyring = "/usr/share/keyrings/ubuntu-archive-keyring.gpg"
    (state / "sources.list").write_text("".join(
        f"deb [arch=amd64 signed-by={keyring}] {args.mirror.rstrip('/')} {suite} main restricted universe multiverse\n"
        for suite in (codename, codename + "-updates", codename + "-security")
    ))
    options = apt_options(state, repo)
    run("apt-get", *options, "update", env=apt_env)
    download_packages(options, apt_env, state, repo, deb)
    run("apt-get", *options, "--download-only", "--yes", "--no-remove", "install", deb, env=apt_env)
    shutil.copy2(deb, repo / deb.name)
    shutil.rmtree(repo / "partial", ignore_errors=True)
    (repo / "lock").unlink(missing_ok=True)
    with (repo / "Packages").open("w") as handle:
        run("dpkg-scanpackages", "--multiversion", ".", "/dev/null", cwd=repo, stdout=handle)
    write_release(repo)
    # Prove the local repo alone satisfies the complete graph from empty status.
    (state / "sources.list").write_text(f"deb [trusted=yes] {repo.as_uri()} ./\n")
    shutil.rmtree(state / "lists")
    (state / "lists/partial").mkdir(parents=True)
    run("apt-get", *options, "update", env=apt_env)
    with (bundle / "dependency-check.txt").open("w") as handle:
        run("apt-get", *options, "--simulate", "--no-remove", "install",
            f"{fields['Package']}={fields['Version']}", stdout=handle, env=apt_env)
    shutil.copy2(ROOT / "scripts/offline_install.sh", bundle / "install.sh")
    (bundle / "install.sh").chmod(0o755)
    settings = {"TARGET_UBUNTU": args.ubuntu, "TARGET_ARCH": "amd64",
                "APP_PACKAGE": fields["Package"], "APP_VERSION": fields["Version"]}
    (bundle / "bundle.env").write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in settings.items()))
    (bundle / "安装说明.txt").write_text(
        f"小凯数据采集平台 云端离线安装包\n适用：Ubuntu {args.ubuntu}，x86_64 / amd64\n\n"
        "解压整个压缩包，在解压目录打开终端，以使用应用的普通桌面用户执行：\n"
        "bash install.sh\n\n脚本会请求管理员密码、校验文件，并从随包提供的本地依赖安装软件。"
        "无需联网下载依赖，不会修改系统软件源配置。请勿单独使用 dpkg -i 安装其中的主 deb。\n"
        "仅检查依赖是否齐全：bash install.sh --check\n"
        "依赖冲突会停止安装；不要使用强制覆盖或删除系统包的命令。\n\n"
        "安装后登录云端、上传数据需要联网。平台：http://180.100.209.48:5173\n"
        "API：http://180.100.209.48:5173/api\n"
        "旧版本升级保留用户配置。如旧配置指向本地，请关闭应用，以普通用户运行：\n"
        'python3 /opt/xiaokai-lab-data-collection/app/scripts/configure_platform.py --env-file "${XDG_CONFIG_HOME:-$HOME/.config}/xiaokai-lab-data-collection/.env" '
        "--api-url http://180.100.209.48:5173/api --web-url http://180.100.209.48:5173\n",
        encoding="utf-8",
    )
    archive = output / (name + ".tar.gz")
    seal_bundle(bundle, archive)
    print(f"完成：{archive}\n系统依赖包数量：{len(list(repo.glob('*.deb'))) - 1}", flush=True)


def write_release(repo):
    (repo / "Packages.gz").write_bytes(gzip.compress((repo / "Packages").read_bytes(), mtime=0))
    release = ("Origin: Xiaokai Offline\nLabel: Xiaokai Offline\nArchitectures: amd64 all\n"
               f"Date: {format_datetime(datetime.now(timezone.utc), usegmt=True)}\nSHA256:\n")
    for name in ("Packages", "Packages.gz"):
        data = (repo / name).read_bytes()
        release += f" {hashlib.sha256(data).hexdigest()} {len(data):16d} {name}\n"
    (repo / "Release").write_text(release)


def seal_bundle(bundle, archive):
    with (bundle / "SHA256SUMS").open("w") as handle:
        for path in sorted(bundle.rglob("*")):
            if path.is_file() and path.name != "SHA256SUMS":
                with path.open("rb") as data:
                    digest = hashlib.file_digest(data, "sha256").hexdigest()
                handle.write(f"{digest}  {path.relative_to(bundle)}\n")
    with tarfile.open(archive, "w:gz", compresslevel=1) as handle:
        handle.add(bundle, arcname=bundle.name)
    with archive.open("rb") as data:
        digest = hashlib.file_digest(data, "sha256").hexdigest()
    archive.with_suffix(archive.suffix + ".sha256").write_text(f"{digest}  {archive.name}\n")


if __name__ == "__main__":
    main()
