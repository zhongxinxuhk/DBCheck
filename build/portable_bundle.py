#!/usr/bin/env python3
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""Assemble one multi-platform DBPulse release with shared state and bundled Java."""

import argparse
import hashlib
import json
import os
import shutil
import stat
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = (
    "windows-x86_64",
    "linux-x86_64",
    "linux-arm64",
    "macos-arm64",
    "macos-x86_64",
)
RESOURCES = (
    "assets",
    "web_templates",
    "templates",
    "i18n",
    "plugins",
    "modules/config",
    "modules/pro/rules",
    "modules/rag",
    "modules/user_management",
    "db",
)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def download_runtime(platform, cache):
    system, arch = platform.split("-", 1)
    remote_system = {"windows": "windows", "linux": "linux", "macos": "mac"}[system]
    remote_arch = {"x86_64": "x64", "arm64": "aarch64"}[arch]
    query = urllib.parse.urlencode(
        {
            "architecture": remote_arch,
            "image_type": "jre",
            "os": remote_system,
            "vendor": "eclipse",
        }
    )
    request = urllib.request.Request(
        "https://api.adoptium.net/v3/assets/latest/11/hotspot?" + query,
        headers={"User-Agent": "DBPulse-portable-build"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        assets = json.load(response)
    if len(assets) != 1:
        raise ValueError("Java runtime selection is ambiguous for " + platform)
    package = assets[0]["binary"]["package"]
    archive = cache / package["name"]
    cache.mkdir(parents=True, exist_ok=True)
    if not archive.is_file() or digest(archive) != package["checksum"]:
        temporary = archive.with_name(archive.name + ".download")
        with urllib.request.urlopen(
            package["link"], timeout=120
        ) as source, temporary.open("wb") as target:
            shutil.copyfileobj(source, target)
        if digest(temporary) != package["checksum"]:
            raise ValueError("Java archive SHA-256 mismatch")
        temporary.replace(archive)
    return archive, {
        "source": package["link"],
        "sha256": package["checksum"],
        "version": assets[0]["version"]["semver"],
    }


def extract_runtime(archive, destination, platform):
    if destination.exists():
        raise ValueError("Runtime output exists, choose a fresh staging directory")
    with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
        unpacked = Path(scratch)
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as package:
                for member in package.infolist():
                    if (
                        not (unpacked / member.filename)
                        .resolve()
                        .is_relative_to(unpacked)
                    ):
                        raise ValueError("Unsafe runtime archive path")
                package.extractall(unpacked)
        else:
            with tarfile.open(archive) as package:
                package.extractall(unpacked, filter="data")
        binary = "java.exe" if platform.startswith("windows-") else "java"
        homes = [
            path.parent.parent
            for path in unpacked.rglob(binary)
            if path.parent.name == "bin"
        ]
        if len(homes) != 1:
            raise ValueError("Runtime archive does not contain one Java home")
        # Materialize links so extracting/copying the package through Windows preserves Unix resources.
        shutil.copytree(homes[0], destination, symlinks=False)
        (destination / "bin" / binary).chmod(
            (destination / "bin" / binary).stat().st_mode | stat.S_IXUSR
        )


def validate_binary(path, platform):
    data = path.read_bytes()
    header = data[:64]
    if platform.startswith("windows-"):
        offset = int.from_bytes(header[60:64], "little") if len(header) >= 64 else 0
        valid = (
            header.startswith(b"MZ")
            and data[offset : offset + 4] == b"PE\x00\x00"
            and int.from_bytes(data[offset + 4 : offset + 6], "little") == 0x8664
        )
    elif platform.startswith("linux-"):
        machine = int.from_bytes(header[18:20], "little") if len(header) >= 20 else 0
        valid = header.startswith(b"\x7fELF") and machine == (
            {"linux-x86_64": 62, "linux-arm64": 183}[platform]
        )
    else:
        cpu = int.from_bytes(header[4:8], "little") if len(header) >= 8 else 0
        valid = header.startswith(b"\xcf\xfa\xed\xfe") and cpu == (
            {"macos-x86_64": 0x1000007, "macos-arm64": 0x100000C}[platform]
        )
    if not valid:
        raise ValueError("Executable format/architecture mismatch for " + platform)


def stage_resources(destination, required_platforms=None):
    required_platforms = list(required_platforms or PLATFORMS)
    if (
        not required_platforms
        or len(set(required_platforms)) != len(required_platforms)
        or set(required_platforms) - set(PLATFORMS)
    ):
        raise ValueError("Required release platforms are invalid")
    destination.mkdir(parents=True, exist_ok=True)
    marker = destination / "dbpulse-portable.json"
    if marker.exists():
        metadata = json.loads(marker.read_text())
        if metadata.get("layout") != "shared-multi-platform":
            raise ValueError("Existing destination is not a DBPulse portable bundle")
        return metadata
    if any(destination.iterdir()):
        raise ValueError("Resource staging destination must be empty")
    for name in RESOURCES:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(
                source,
                destination / name,
                symlinks=False,
                ignore=shutil.ignore_patterns(
                    "__pycache__", ".DS_Store", "*.pyc", "*.db", "*.db-wal", "*.db-shm"
                ),
            )
    shutil.copytree(
        ROOT / "drivers",
        destination / "drivers",
        symlinks=False,
        ignore=shutil.ignore_patterns(".DS_Store", "oracle_client"),
    )
    # A historical 8s driver was stored under 8a. Keep it out of clean releases.
    for jar in (destination / "drivers/gbase8a").rglob("*.jar"):
        with zipfile.ZipFile(jar) as package:
            valid_driver = "com/gbase/jdbc/Driver.class" in package.namelist()
        if not valid_driver:
            jar.unlink()
    shutil.copy2(ROOT / "modules/config/version.json", destination / "version.json")
    for name in ("LICENSE", "NOTICE"):
        shutil.copy2(ROOT / name, destination / name)
    shutil.copy2(ROOT / "build/portable/start.sh", destination / "start.sh")
    shutil.copy2(ROOT / "build/portable/start.sh", destination / "start.command")
    shutil.copy2(ROOT / "build/portable/start.bat", destination / "start.bat")
    for name in ("start.sh", "start.command"):
        (destination / name).chmod(0o755)
    (destination / "data").mkdir()
    version = json.loads(
        (ROOT / "modules/config/version.json").read_text(encoding="utf-8-sig")
    )["version"]
    metadata = {
        "schema_version": 1,
        "layout": "shared-multi-platform",
        "version": version,
        "platforms": {},
        "required_platforms": required_platforms,
        "complete": False,
        "data_directory": "data",
        "drivers_directory": "drivers",
        "created": time.time(),
    }
    marker.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    readme = """DBPulse 多平台便携目录

Windows 双击 start.bat，Linux 执行 bash start.sh，macOS 双击 start.command。
二进制、Python 依赖与 Java 运行时随包提供。Web 默认地址为 http://127.0.0.1:5003。

所有平台读取同一个 data 目录及 data/.db_key，数据源、用户、集群、规则与配置可随整个目录迁移。
切换系统前停止应用，再复制整个便携目录。加密密钥和 SQLite 数据库须一起保留。
不要将 bin 或 runtime 子目录单独当作完整应用复制。

从旧版迁入时，在尚未启动的新便携目录执行 start.bat --portable-import 原安装目录，
或 bash start.sh --portable-import 原安装目录。迁移会复制 SQLite 快照及原加密密钥，保留原安装。
目标 data 已有内容时会停止迁移。用户放在安装目录之外的证书、SSH 密钥及备份路径需一并迁入目录。
本地证书与 SSH 文件可使用相对于便携根目录的路径。

Linux 版本基于 glibc 2.28。二进制分别适用于清单列出的 CPU 架构。
首次安装默认账号为 admin，默认密码为 admin123。

"""
    (destination / "使用说明.txt").write_text(readme, encoding="utf-8-sig")
    return metadata


def add_component(destination, platform, component, runtime, receipt=None):
    metadata = stage_resources(destination)
    if platform not in PLATFORMS:
        raise ValueError("Unsupported platform")
    name = "dbcheck.exe" if platform.startswith("windows-") else "dbcheck"
    validate_binary(component / name, platform)
    for state in (component / "data", component / "_internal/data"):
        if state.is_dir() and any(state.iterdir()):
            raise ValueError("Native component contains installation data")
    target = destination / "bin" / platform
    if target.exists():
        raise ValueError("Component already staged for " + platform)
    shutil.copytree(component, target, symlinks=False)
    java_target = destination / "runtime" / platform / "jre"
    java_target.parent.mkdir(parents=True, exist_ok=True)
    if runtime.is_dir():
        shutil.copytree(runtime, java_target, symlinks=False)
    else:
        extract_runtime(runtime, java_target, platform)
    java_binary = (
        java_target
        / "bin"
        / ("java.exe" if platform.startswith("windows-") else "java")
    )
    if not java_binary.is_file():
        raise ValueError("Missing bundled Java executable")
    java_binary.chmod(java_binary.stat().st_mode | stat.S_IXUSR)
    (target / name).chmod((target / name).stat().st_mode | stat.S_IXUSR)
    java_receipt = dict(receipt or {})
    java_receipt.pop("archive", None)
    component_receipt = component.parent / "component.json"
    if component_receipt.is_file():
        shutil.copy2(component_receipt, target / "component.json")
    metadata["platforms"][platform] = {
        "executable": str((target / name).relative_to(destination)).replace("\\", "/"),
        "sha256": digest(target / name),
        "java_runtime": java_receipt,
        "minimum_glibc": "2.28" if platform.startswith("linux-") else None,
    }
    launcher = target / "DBPulse.exe"
    if platform.startswith("windows-") and launcher.is_file():
        validate_binary(launcher, platform)
        metadata["platforms"][platform]["launcher"] = str(
            launcher.relative_to(destination)
        ).replace("\\", "/")
        metadata["platforms"][platform]["launcher_sha256"] = digest(launcher)
    metadata["complete"] = all(
        key in metadata["platforms"] for key in metadata["required_platforms"]
    )
    (destination / "dbpulse-portable.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2)
    )


def archive_bundle(destination, archive):
    metadata = json.loads((destination / "dbpulse-portable.json").read_text())
    if not metadata.get("complete"):
        raise ValueError("Portable release is missing platform components")
    if any((destination / "data").iterdir()):
        raise ValueError(
            "Release contains runtime user data, use a clean resource staging directory"
        )
    required = metadata.get("required_platforms", list(PLATFORMS))
    if (
        not required
        or len(set(required)) != len(required)
        or set(required) - set(PLATFORMS)
        or set(metadata["platforms"]) != set(required)
    ):
        raise ValueError("Release platform manifest does not match its components")
    for platform in required:
        name = metadata["platforms"][platform]["executable"]
        validate_binary(destination / name, platform)
        if digest(destination / name) != metadata["platforms"][platform]["sha256"]:
            raise ValueError("Staged binary was modified for " + platform)
        launcher = metadata["platforms"][platform].get("launcher")
        if (
            launcher
            and digest(destination / launcher)
            != metadata["platforms"][platform]["launcher_sha256"]
        ):
            raise ValueError("Staged desktop launcher was modified for " + platform)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(destination.rglob("*")):
            if path.is_symlink():
                raise ValueError("Portable release still contains a symbolic link")
            if path.is_file():
                output.write(
                    path, Path(destination.name) / path.relative_to(destination)
                )
    checksum = digest(archive)
    archive.with_suffix(archive.suffix + ".sha256").write_text(
        checksum + "  " + archive.name + "\n"
    )
    print(
        json.dumps(
            {
                "archive": str(archive),
                "sha256": checksum,
                "bytes": archive.stat().st_size,
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    runtime = subparsers.add_parser("runtime")
    runtime.add_argument("--platform", choices=PLATFORMS, required=True)
    runtime.add_argument("--cache", type=Path, required=True)
    stage = subparsers.add_parser("stage")
    stage.add_argument("--output", type=Path, required=True)
    stage.add_argument("--required-platforms", nargs="+", choices=PLATFORMS)
    stage.add_argument("--platform", choices=PLATFORMS)
    stage.add_argument("--component", type=Path)
    stage.add_argument("--runtime", type=Path)
    stage.add_argument("--receipt", type=Path)
    archive = subparsers.add_parser("archive")
    archive.add_argument("--directory", type=Path, required=True)
    archive.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "runtime":
        path, receipt = download_runtime(args.platform, args.cache)
        receipt["archive"] = str(path)
        (args.cache / (args.platform + "-java.json")).write_text(
            json.dumps(receipt, indent=2)
        )
        print(json.dumps(receipt))
    elif args.command == "stage":
        stage_resources(args.output, args.required_platforms)
        if args.platform:
            if not args.component or not args.runtime:
                parser.error("--platform requires --component and --runtime")
            receipt = json.loads(args.receipt.read_text()) if args.receipt else None
            add_component(
                args.output, args.platform, args.component, args.runtime, receipt
            )
    else:
        archive_bundle(args.directory, args.output)


if __name__ == "__main__":
    main()
