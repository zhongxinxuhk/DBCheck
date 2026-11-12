# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""Build, exercise and archive a native DBPulse distribution from PyCharm or CLI."""


import argparse
import hashlib
import importlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check_dependencies(windows=False):
    from modules.core.package_check import REQUIRED_MODULES, REQUIRED_ASSETS

    for name in ("PyInstaller",) + REQUIRED_MODULES + (("tkinter",) if windows else ()):
        importlib.import_module(name)
    for relative in REQUIRED_ASSETS:
        if not (ROOT / relative).is_file():
            raise FileNotFoundError("Missing release input " + relative)
    from modules.core.bootstrap_security import initial_admin_password

    if initial_admin_password() != "admin123":
        raise RuntimeError("Installation password must be admin123")
    print("Release inputs and dependencies verified", flush=True)


def snapshot_source(container):
    names = (
        "web_ui.py",
        "build",
        "modules",
        "web_templates",
        "i18n",
        "templates",
        "assets",
        "plugins",
        "drivers",
        "db",
        "LICENSE",
        "NOTICE",
    )
    with tempfile.TemporaryDirectory(prefix="dbpulse-build-source-") as temporary:
        archive = Path(temporary) / "source.tar"
        with tarfile.open(archive, "w") as package:

            def include(info):
                parts = Path(info.name).parts
                if any(
                    part in {"__pycache__", ".git", "tests", ".DS_Store"}
                    for part in parts
                ):
                    return None
                if info.name.endswith((".pyc", ".db", ".log")):
                    return None
                return info

            for name in names:
                package.add(ROOT / name, arcname=name, filter=include)
        subprocess.run(
            [
                "docker",
                "cp",
                str(archive),
                container + ":/tmp/dbpulse-build-source.tar",
            ],
            check=True,
        )
    subprocess.run(
        [
            "docker",
            "exec",
            container,
            "sh",
            "-c",
            "mkdir -p /workspace && tar -xf /tmp/dbpulse-build-source.tar -C /workspace",
        ],
        check=True,
    )


def windows_container(args):
    container = os.environ.get("DBPULSE_WINDOWS_BUILDER")
    if not container:
        raise RuntimeError(
            "Windows binaries require Windows Python. On this Mac select the configured Windows build container, or run this configuration on Windows."
        )
    subprocess.run(
        ["docker", "start", container], check=True, stdout=subprocess.DEVNULL
    )
    snapshot_source(container)
    command = [
        "docker",
        "exec",
        "-w",
        "/workspace",
        "-e",
        "WINEDEBUG=-all",
        "-e",
        "DBPULSE_WINE_BUILD=1",
        "-e",
        "WINEPREFIX=/root/.wine10-dbpulse",
        "-e",
        "WINEDLLOVERRIDES=mscoree,mshtml=",
        "-e",
        "PYTHONPATH=Z:/opt/dbpulse-windows-deps",
        "-e",
        "DBCHECK_BUILD_INPUTS=Z:/build/windows-inputs",
        "-e",
        "DBPULSE_BUILD_JAVA_RUNTIME=Z:/opt/dbpulse-java-windows",
        container,
        "xvfb-run",
        "-a",
        "/opt/wine-stable/bin/wine64",
        "Z:/opt/windows-python/python.exe",
        "build/build_release.py",
        "--platform",
        "windows",
        "--workpath",
        "Z:/build/windows-work",
        "--distpath",
        "Z:/build/dist",
    ]
    if args.check_only:
        command.append("--check-only")
    started = time.time()
    subprocess.run(command, check=True)
    if not args.check_only:
        mounts = json.loads(
            subprocess.check_output(
                ["docker", "inspect", container, "--format", "{{json .Mounts}}"],
                text=True,
            )
        )
        shared = (
            Path(
                next(
                    mount["Source"]
                    for mount in mounts
                    if mount["Destination"] == "/build"
                )
            )
            / "dist"
        )
        args.distpath.mkdir(parents=True, exist_ok=True)
        for entry in shared.glob("DBPulse-windows-*"):
            if entry.stat().st_mtime < started:
                continue
            target = args.distpath / entry.name
            if not target.exists():
                if entry.is_dir():
                    shutil.copytree(entry, target)
                else:
                    shutil.copy2(entry, target)
        print("Windows releases copied to " + str(args.distpath.resolve()), flush=True)


def native(args):
    check_dependencies(args.platform == "windows")
    if args.check_only:
        return
    work = (args.workpath or ROOT / "build_pyinstaller_tmp" / args.platform).resolve()
    destination = args.distpath.resolve()
    native_output = work / "native"
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--workpath",
            str(work / "pyinstaller"),
            "--distpath",
            str(native_output),
            str(ROOT / "build" / ("dbcheck_" + args.platform + ".spec")),
        ],
        cwd=ROOT,
        check=True,
    )
    product = native_output / (
        "DBPulse-"
        + {"windows": "Windows", "macos": "macOS", "linux": "Linux"}[args.platform]
    )
    java_runtime = os.environ.get("DBPULSE_BUILD_JAVA_RUNTIME")
    if java_runtime:
        shutil.copytree(java_runtime, product / "runtime/java", dirs_exist_ok=True)
    binary = product / ("dbcheck.exe" if args.platform == "windows" else "dbcheck")
    check = subprocess.run(
        [str(binary), "--package-self-test"],
        cwd=product,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=360,
    )
    print(check.stdout, flush=True)
    if check.returncode or "__DBPULSE_PACKAGE_CHECK__" not in check.stdout:
        raise RuntimeError("Frozen application self-test failed")
    result = json.loads(
        next(
            line.split("__DBPULSE_PACKAGE_CHECK__", 1)[1]
            for line in check.stdout.splitlines()
            if line.startswith("__DBPULSE_PACKAGE_CHECK__")
        )
    )
    if args.platform == "windows":
        desktop_result = work / "desktop-check.json"
        desktop_result.unlink(missing_ok=True)
        desktop = subprocess.run(
            [
                str(product / "DBPulse.exe"),
                "--desktop-self-test",
                "--result-file",
                str(desktop_result),
            ],
            cwd=product,
            timeout=45,
        )
        if (
            desktop.returncode
            or not desktop_result.is_file()
            or not json.loads(desktop_result.read_text()).get("ok")
        ):
            raise RuntimeError("Windows status window / tray self-test failed")
        result["desktop"] = json.loads(desktop_result.read_text())
        (product / "start.bat").write_bytes(
            b'@echo off\r\ncd /d "%~dp0"\r\nstart "" "%~dp0DBPulse.exe"\r\nexit /b 0\r\n'
        )
    else:
        launcher = (
            product / "start.command"
            if args.platform == "macos"
            else product / "start.sh"
        )
        launcher.write_text(
            '#!/bin/sh\ncd "$(dirname "$0")"\nexec ./dbcheck web\n', encoding="utf-8"
        )
        launcher.chmod(0o755)
    # Self-tests can initialize legacy runtime paths beside the staging executable.
    # Release archives always start from fresh installation data.
    for runtime_data in (product / "data", product / "_internal/data"):
        if runtime_data.exists():
            shutil.rmtree(runtime_data)
    (product / "README.txt").write_text(
        "DBPulse\n\n初始账号 admin\n初始密码 admin123\n管理地址 http://127.0.0.1:5003\n"
        + (
            "\nWindows 双击 DBPulse.exe 或 start.bat。关闭状态窗口后在右下角托盘继续运行。\n托盘菜单可显示窗口、启动、停止和退出。\n窗口下方实时显示运行日志，可导出数据备份 ZIP 或导入恢复。\n导入会保留当前数据副本，恢复完成后使用备份中的账号密码登录。\n"
            if args.platform == "windows"
            else ""
        ),
        encoding="utf-8",
    )
    result["clean_installation"] = True
    (product / "build-verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    from modules.config.version import __version__

    version = __version__
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    arch = "x86_64" if args.platform == "windows" else platform.machine()
    destination.mkdir(parents=True, exist_ok=True)
    name = f"DBPulse-{args.platform}-{arch}-{version}-{stamp}"
    release = destination / name
    shutil.copytree(product, release)
    archive = shutil.make_archive(
        str(release), "zip", root_dir=destination, base_dir=name
    )
    with zipfile.ZipFile(archive) as package:
        forbidden = {
            ".db_key",
            "um_rbac.db",
            "instances.db",
            "bootstrap_admin_password",
        }
        if any(Path(name).name in forbidden for name in package.namelist()):
            raise RuntimeError("Release archive contains runtime installation state")
    digest = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
    Path(archive + ".sha256").write_text(digest + "  " + Path(archive).name + "\n")
    print("Release verified: " + str(release), flush=True)
    print("Archive: " + archive, flush=True)


def main():
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--platform", choices=["windows", "macos", "linux"], required=True
    )
    parser.add_argument("--workpath", type=Path)
    parser.add_argument("--distpath", type=Path, default=ROOT / "dist")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    host = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    if args.platform != host:
        if args.platform == "windows":
            windows_container(args)
            return
        raise RuntimeError("Target platform requires a matching native build host")
    native(args)


if __name__ == "__main__":
    main()
