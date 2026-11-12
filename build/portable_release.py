#!/usr/bin/env python3
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""Native build entry point for the shared multi-platform DBPulse portable release."""

import argparse
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modules.core.portable import platform_key
from portable_bundle import PLATFORMS, download_verified


def native(args):
    platform = platform_key()
    if args.platform != platform:
        raise ValueError(
            "Build platform mismatch, expected "
            + args.platform
            + ", running "
            + platform
        )
    if sys.version_info[:2] != (3, 12):
        raise ValueError("Native portable builds require Python 3.12")
    requirements = (
        ROOT
        / "build/portable"
        / (
            "requirements-macos-x86_64.txt"
            if platform == "macos-x86_64"
            else "requirements-lock.txt"
        )
    )
    if not args.skip_install:
        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--only-binary=:all:",
            "-r",
            str(requirements),
        ]
        if args.wheels:
            command += ["--no-index", "--find-links", str(args.wheels.resolve())]
        subprocess.run(command, check=True)
    for module in (
        "flask",
        "gevent",
        "cryptography",
        "bcrypt",
        "docx",
        "docxtpl",
        "pymysql",
        "psycopg2",
        "oracledb",
        "jpype",
        "jaydebeapi",
        "tzdata",
    ):
        importlib.import_module(module)
    system = platform.split("-", 1)[0]
    spec = (
        ROOT
        / "build"
        / (
            "dbcheck_"
            + {"windows": "windows", "linux": "linux", "macos": "macos"}[system]
            + ".spec"
        )
    )
    output = args.output.resolve() / platform
    work = args.work.resolve() / platform
    environment = dict(
        os.environ,
        DBPULSE_PORTABLE_BUILD="1",
        PYINSTALLER_CONFIG_DIR=str(work / "cache"),
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            str(spec),
            "--noconfirm",
            "--distpath",
            str(output),
            "--workpath",
            str(work),
        ],
        cwd=ROOT,
        env=environment,
        check=True,
    )
    import PyInstaller, cryptography

    metadata = {
        "platform": platform,
        "python": sys.version.split()[0],
        "pyinstaller": PyInstaller.__version__,
        "cryptography": cryptography.__version__,
        "requirements_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
    }
    (output / "component.json").write_text(json.dumps(metadata, indent=2))


def prepare_drivers(args):
    entries = json.loads((ROOT / "build/portable/driver-downloads.json").read_text())
    args.cache.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        archive = args.cache / entry["filename"]
        download_verified(entry["url"], archive, entry["sha256"])
        destination = ROOT / "drivers" / entry["db_type"] / entry["version"]
        destination.mkdir(parents=True, exist_ok=True)
        if entry["archive"]:
            with tarfile.open(archive) as package:
                for member in package.getmembers():
                    if member.isfile() and member.name.endswith(".jar"):
                        (destination / Path(member.name).name).write_bytes(
                            package.extractfile(member).read()
                        )
        else:
            shutil.copy2(archive, destination / archive.name)
        expected = {
            "gbase8a": "com/gbase/jdbc/Driver.class",
            "sqlserver": "com/microsoft/sqlserver/jdbc/SQLServerDriver.class",
        }[entry["db_type"]]
        for jar in destination.glob("*.jar"):
            with zipfile.ZipFile(jar) as package:
                if expected not in package.namelist():
                    raise ValueError("Downloaded JDBC driver class mismatch")
    print("Driver inputs verified")


def prepare_python(args):
    mapping = {
        "windows-x86_64": "x86_64-pc-windows-msvc",
        "macos-arm64": "aarch64-apple-darwin",
        "macos-x86_64": "x86_64-apple-darwin",
        "linux-x86_64": "x86_64-unknown-linux-gnu",
        "linux-arm64": "aarch64-unknown-linux-gnu",
    }
    request = urllib.request.Request(
        "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest",
        headers={"User-Agent": "DBPulse-portable-build"},
    )
    if os.environ.get("GITHUB_TOKEN"):
        request.add_header("Authorization", "Bearer " + os.environ["GITHUB_TOKEN"])
    with urllib.request.urlopen(request, timeout=45) as response:
        release = json.load(response)
    candidates = [
        asset
        for asset in release["assets"]
        if asset["name"].startswith("cpython-3.12.")
        and asset["name"].endswith(
            "-" + mapping[args.platform] + "-install_only.tar.gz"
        )
    ]
    if len(candidates) != 1 or not candidates[0].get("digest", "").startswith(
        "sha256:"
    ):
        raise ValueError("Standalone Python input has no unique verified release")
    asset = candidates[0]
    args.cache.mkdir(parents=True, exist_ok=True)
    archive = args.cache / asset["name"]
    expected = asset["digest"].split(":", 1)[1]
    download_verified(asset["browser_download_url"], archive, expected)
    output = args.output.resolve() / args.platform
    output.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as package:
        # Cyclic ncurses aliases are irrelevant to this headless application.
        members = [
            member
            for member in package.getmembers()
            if not member.name.startswith("python/share/terminfo")
        ]
        package.extractall(output, members=members, filter="data")
    metadata = {
        "source": asset["browser_download_url"],
        "sha256": expected,
        "platform": args.platform,
    }
    (output / "source.json").write_text(json.dumps(metadata, indent=2))
    print(
        output
        / "python"
        / ("python.exe" if args.platform.startswith("windows") else "bin/python3.12")
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    compile = commands.add_parser("native")
    compile.add_argument("--platform", choices=PLATFORMS, required=True)
    compile.add_argument("--output", type=Path, default=ROOT / ".portable-build/native")
    compile.add_argument("--work", type=Path, default=ROOT / ".portable-build/work")
    compile.add_argument("--skip-install", action="store_true")
    compile.add_argument("--wheels", type=Path)
    drivers = commands.add_parser("drivers")
    drivers.add_argument(
        "--cache", type=Path, default=ROOT / ".portable-build/downloads"
    )
    python = commands.add_parser("python")
    python.add_argument("--platform", choices=PLATFORMS, required=True)
    python.add_argument(
        "--cache", type=Path, default=ROOT / ".portable-build/downloads"
    )
    python.add_argument(
        "--output", type=Path, default=ROOT / ".portable-build/native-python"
    )
    args = parser.parse_args()
    if args.command == "native":
        native(args)
    elif args.command == "drivers":
        prepare_drivers(args)
    else:
        prepare_python(args)


if __name__ == "__main__":
    main()
