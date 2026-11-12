# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""Version, verify, assemble and atomically publish native portable releases."""

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "build"))
from portable_bundle import stage_resources, add_component, archive_bundle, digest

TARGETS = ("macos-arm64", "linux-x86_64", "linux-arm64", "windows-x86_64")
TRANSFER_TARGETS = ("windows-x86_64", "linux-x86_64", "linux-arm64")
TRAIN = re.compile(r"^v1\.0\.(\d+)$")
MARKER = "<!-- dbpulse-release-pipeline:v1 -->"


class GitHub:
    def __init__(self, repository=None):
        self.repository = repository or os.environ["GITHUB_REPOSITORY"]
        self.token = os.environ["GITHUB_TOKEN"]

    def request(self, path, method="GET", data=None):
        url = "https://api.github.com/repos/" + self.repository + "/" + path
        request = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "DBPulse-release-pipeline",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            body = response.read()
            return json.loads(body) if body else None

    def pages(self, path):
        result = []
        page = 1
        while True:
            items = self.request(
                path + ("&" if "?" in path else "?") + f"per_page=100&page={page}"
            )
            result.extend(items)
            if len(items) < 100:
                return result
            page += 1

    def upload(self, release_id, path):
        endpoint = (
            f"/repos/{self.repository}/releases/{release_id}/assets?name="
            + urllib.parse.quote(path.name)
        )
        connection = http.client.HTTPSConnection("uploads.github.com", timeout=900)
        try:
            connection.putrequest("POST", endpoint)
            connection.putheader("Authorization", "Bearer " + self.token)
            connection.putheader("User-Agent", "DBPulse-release-pipeline")
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(path.stat().st_size))
            connection.endheaders()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    connection.send(chunk)
            response = connection.getresponse()
            body = response.read()
            if response.status != 201:
                raise RuntimeError(
                    f"Asset upload failed ({response.status}): {path.name}"
                )
            value = json.loads(body)
            if value["size"] != path.stat().st_size or value.get("digest") not in (
                None,
                "sha256:" + digest(path),
            ):
                raise RuntimeError("Uploaded asset verification failed: " + path.name)
        finally:
            connection.close()


def choose_version(refs, releases, sha):
    releases_by_tag = {release["tag_name"]: release for release in releases}
    versions = []
    for ref in refs:
        tag = ref["ref"].removeprefix("refs/tags/")
        match = TRAIN.fullmatch(tag)
        if match is None:
            continue
        versions.append(int(match[1]))
        release = releases_by_tag.get(tag)
        if ref["object"]["sha"] == sha and (
            release is None or MARKER in (release.get("body") or "")
        ):
            return tag, bool(release and not release["draft"])
    return "v1.0." + str(max(versions, default=0) + 1), False


def plan():
    github = GitHub()
    refs = [
        {"ref": "refs/tags/" + item["name"], "object": {"sha": item["commit"]["sha"]}}
        for item in github.pages("tags")
    ]
    tag, published = choose_version(
        refs, github.pages("releases"), os.environ["GITHUB_SHA"]
    )
    values = {"tag": tag, "published": str(published).lower()}
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(
                "".join(key + "=" + value + "\n" for key, value in values.items())
            )
    print(json.dumps(values))


def stamp(version, root=ROOT):
    if TRAIN.fullmatch(version) is None:
        raise ValueError("Release version must use v1.0.N")
    path = root / "modules/config/version.py"
    text, count = re.subn(
        r"(?m)^__version__\s*=\s*['\"][^'\"]+['\"]",
        "__version__ = '" + version + "'",
        path.read_text(encoding="utf-8"),
    )
    if count != 1:
        raise ValueError("Unable to locate one application version")
    path.write_text(text, encoding="utf-8")
    path = root / "modules/config/version.json"
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    value.update(
        version=version,
        lastUpdated=datetime.now(timezone(timedelta(hours=8))).date().isoformat(),
    )
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def command(bundle, platform, *arguments):
    executable = (
        bundle
        / "bin"
        / platform
        / ("dbcheck.exe" if platform.startswith("windows") else "dbcheck")
    )
    executable.chmod(executable.stat().st_mode | 0o100)
    return [str(executable.resolve()), *arguments]


def run_check(bundle, platform, arguments, result_marker=None, timeout=360):
    environment = dict(
        os.environ,
        DBPULSE_PORTABLE_HOME=str(bundle.resolve()),
        PYTHONIOENCODING="utf-8",
        PYTHONUTF8="1",
    )
    process = subprocess.run(
        command(bundle, platform, *arguments),
        cwd=bundle,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    lines = process.stdout.splitlines()
    candidates = (
        [
            line.split(result_marker, 1)[1]
            for line in lines
            if result_marker and line.startswith(result_marker)
        ]
        if result_marker
        else [line for line in lines if line.startswith("{")]
    )
    result = json.loads(candidates[-1]) if candidates else {}
    if process.returncode != 0 or result.get("ok") is not True:
        print(process.stdout[-12000:])
        raise RuntimeError("Frozen runtime check failed: " + " ".join(arguments))
    return result


def verify_component(bundle, platform, output):
    results = {
        "platform": platform,
        "source_commit": os.environ.get("GITHUB_SHA"),
        "version": json.loads((bundle / "version.json").read_text())["version"],
    }
    results["portable"] = run_check(bundle, platform, ["--portable-self-test"])
    results["application"] = run_check(
        bundle, platform, ["--package-self-test"], "__DBPULSE_PACKAGE_CHECK__"
    )
    if platform.startswith("windows"):
        result_file = output.resolve().with_suffix(".desktop.json")
        result_file.parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.run(
            [
                str((bundle / "bin" / platform / "DBPulse.exe").resolve()),
                "--desktop-self-test",
                "--allow-headless-tray",
                "--result-file",
                str(result_file),
            ],
            timeout=120,
        )
        if process.returncode or not result_file.is_file():
            raise RuntimeError("Windows status window check failed")
        results["desktop"] = json.loads(result_file.read_text(encoding="utf-8"))
        if not results["desktop"].get("ok"):
            raise RuntimeError("Windows desktop feature check failed")
    # Tests use disposable state. Publish only the pristine staged distribution.
    for path in (
        bundle / "data",
        bundle / "bin" / platform / "data",
        bundle / "bin" / platform / "_internal/data",
    ):
        if path.exists():
            shutil.rmtree(path)
    (bundle / "data").mkdir()
    results["clean_installation"] = True
    results["ok"] = True
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"platform": platform, "ok": True}))


def assemble(components, destination, version, sha):
    stamp(version)
    destination.mkdir(parents=True, exist_ok=True)
    assets = []
    variants = [(platform, (platform,)) for platform in TARGETS] + [
        ("Windows-Linux-Portable", TRANSFER_TARGETS)
    ]
    with tempfile.TemporaryDirectory(prefix="dbpulse-release-") as temporary:
        for label, platforms in variants:
            bundle = Path(temporary) / ("DBPulse-" + version + "-" + label)
            stage_resources(bundle, platforms)
            verification = {}
            for platform in platforms:
                runtime = components / "runtime" / platform
                add_component(
                    bundle,
                    platform,
                    components / "bin" / platform,
                    runtime / "jre",
                    json.loads((runtime / "receipt.json").read_text()),
                )
                verification[platform] = json.loads(
                    (components / "verification" / (platform + ".json")).read_text()
                )
                if (
                    verification[platform].get("ok") is not True
                    or verification[platform].get("version") != version
                    or verification[platform].get("source_commit") != sha
                ):
                    raise ValueError(
                        "Native component verification/version is missing: " + platform
                    )
            (bundle / "build-verification.json").write_text(
                json.dumps(
                    {
                        "version": version,
                        "source_commit": sha,
                        "components": verification,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            output = destination / (bundle.name + ".zip")
            archive_bundle(bundle, output)
            assets.append(
                {
                    "name": output.name,
                    "sha256": digest(output),
                    "bytes": output.stat().st_size,
                    "platforms": list(platforms),
                }
            )
            shutil.rmtree(bundle)
    manifest = {
        "version": version,
        "source_commit": sha,
        "assets": assets,
        "minimum_glibc": "2.28",
    }
    (destination / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    paths = [destination / item["name"] for item in assets] + [
        destination / "release-manifest.json"
    ]
    (destination / "SHA256SUMS.txt").write_text(
        "".join(digest(path) + "  " + path.name + "\n" for path in paths)
    )
    for sidecar in destination.glob("*.zip.sha256"):
        sidecar.unlink()


def unpack(archive, output):
    if output.exists():
        raise ValueError("Verification extraction directory already exists")
    output.mkdir(parents=True)
    with zipfile.ZipFile(archive) as package:
        for info in package.infolist():
            path = (output / info.filename).resolve()
            if not path.is_relative_to(output.resolve()) or "\\" in info.filename:
                raise ValueError("Unsafe verification archive path")
        package.extractall(output)
    roots = [
        path
        for path in output.iterdir()
        if path.is_dir() and (path / "dbpulse-portable.json").is_file()
    ]
    if len(roots) != 1:
        raise ValueError("Release archive does not contain one portable root")
    return roots[0].resolve()


def transfer_seed(archive, output):
    with tempfile.TemporaryDirectory(prefix="DBPulse CI transfer ") as temporary:
        bundle = unpack(archive, Path(temporary) / "extracted")
        result = run_check(bundle, "windows-x86_64", ["--portable-transfer-seed"])
        if not result["ok"]:
            raise ValueError("Windows configuration fixture failed")
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as package:
            for path in (bundle / "data").rglob("*"):
                if path.is_file():
                    package.write(path, path.relative_to(bundle))
        print(json.dumps(result))


def transfer_verify(archive, fixture, platform, output):
    with tempfile.TemporaryDirectory(prefix="DBPulse CI verify ") as temporary:
        bundle = unpack(archive, Path(temporary) / "extracted")
        with zipfile.ZipFile(fixture) as package:
            for info in package.infolist():
                if not info.filename.startswith("data/") or not (
                    bundle / info.filename
                ).resolve().is_relative_to((bundle / "data").resolve()):
                    raise ValueError("Invalid transfer fixture path")
            package.extractall(bundle)
        result = run_check(bundle, platform, ["--portable-transfer-check"])
        if result.get("source_platform") != "windows-x86_64":
            raise ValueError("Transfer state was not created by the Windows binary")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2))
        print(json.dumps(result))


def verify_assets(directory, version, sha):
    manifest = json.loads((directory / "release-manifest.json").read_text())
    if manifest["version"] != version or manifest["source_commit"] != sha:
        raise ValueError("Release artifacts belong to a different version or commit")
    if len(manifest["assets"]) != 5:
        raise ValueError(
            "Release needs four native packages and the Windows/Linux bundle"
        )
    for asset in manifest["assets"]:
        path = directory / asset["name"]
        if (
            path.name != asset["name"]
            or path.stat().st_size != asset["bytes"]
            or digest(path) != asset["sha256"]
        ):
            raise ValueError("Release asset checksum/size mismatch")
        if path.stat().st_size >= 2 * 1024**3:
            raise ValueError("Release asset exceeds GitHub upload limit")
        with zipfile.ZipFile(path) as package:
            names = package.namelist()
            if any(
                (
                    len(Path(name).parts) > 1
                    and Path(name).parts[1] == "data"
                    and not name.endswith("/")
                )
                or name.endswith(("/.db_key", "/instances.db", "/um_rbac.db"))
                for name in names
            ):
                raise ValueError("Release asset contains runtime state")
    return manifest


def publish(directory, version, sha):
    manifest = verify_assets(directory, version, sha)
    github = GitHub()
    release = next(
        (value for value in github.pages("releases") if value["tag_name"] == version),
        None,
    )
    try:
        ref = github.request("git/ref/tags/" + version)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        ref = None
    if ref and ref["object"]["sha"] != sha:
        raise ValueError("Release tag already points at another commit")
    if release and (MARKER not in release.get("body", "") or not release["draft"]):
        raise ValueError("Existing published/manual release cannot be overwritten")
    if ref is None:
        github.request("git/refs", "POST", {"ref": "refs/tags/" + version, "sha": sha})
    notes = f"""{MARKER}
DBPulse {version}

本版本由推送后的检查与原生编译工作流生成，源提交为 `{sha}`。

- macOS ARM64、Linux x64 / ARM64、Windows x64 启动包。
- Windows-Linux-Portable 包包含 Windows x64、Linux x64 和 Linux ARM64 程序。工程师可在 Windows 配置后停止软件，将整个目录复制到 Linux，执行 `bash start.sh`。
- 所有平台共用 data、数据库密码密钥、台账、账号、任务、报告和自定义规则，包含 Java 运行时。
- Windows 双击 start.bat，macOS 双击 start.command。首次安装账号 admin，密码 admin123。
- Linux 要求 glibc 2.28 及以上。SHA256SUMS.txt 提供下载校验值。

四个平台的运行时检查与 Windows 配置迁移验证全部通过后发布。
"""
    if release is None:
        release = github.request(
            "releases",
            "POST",
            {
                "tag_name": version,
                "name": "DBPulse " + version,
                "body": notes,
                "draft": True,
                "prerelease": False,
            },
        )
    expected = {asset["name"] for asset in manifest["assets"]} | {
        "release-manifest.json",
        "SHA256SUMS.txt",
    }
    for asset in github.pages(f'releases/{release["id"]}/assets'):
        if asset["name"] in expected:
            github.request("releases/assets/" + str(asset["id"]), "DELETE")
    for name in sorted(expected):
        github.upload(release["id"], directory / name)
    github.request(
        "releases/" + str(release["id"]),
        "PATCH",
        {"draft": False, "make_latest": "true", "body": notes},
    )
    print(json.dumps({"tag": version, "url": release["html_url"], "published": True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")
    stamp_command = commands.add_parser("stamp")
    stamp_command.add_argument("--version", required=True)
    check = commands.add_parser("check-component")
    check.add_argument("--bundle", type=Path, required=True)
    check.add_argument("--platform", choices=TARGETS, required=True)
    check.add_argument("--output", type=Path, required=True)
    assemble_command = commands.add_parser("assemble")
    assemble_command.add_argument("--components", type=Path, required=True)
    assemble_command.add_argument("--output", type=Path, required=True)
    assemble_command.add_argument("--version", required=True)
    assemble_command.add_argument("--sha", required=True)
    seed = commands.add_parser("transfer-seed")
    seed.add_argument("--archive", type=Path, required=True)
    seed.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("transfer-verify")
    verify.add_argument("--archive", type=Path, required=True)
    verify.add_argument("--fixture", type=Path, required=True)
    verify.add_argument("--platform", choices=TARGETS, required=True)
    verify.add_argument("--output", type=Path, required=True)
    release = commands.add_parser("publish")
    release.add_argument("--directory", type=Path, required=True)
    release.add_argument("--version", required=True)
    release.add_argument("--sha", required=True)
    args = parser.parse_args()
    if args.command == "plan":
        plan()
    elif args.command == "stamp":
        stamp(args.version)
    elif args.command == "check-component":
        verify_component(args.bundle.resolve(), args.platform, args.output)
    elif args.command == "assemble":
        assemble(args.components, args.output, args.version, args.sha)
    elif args.command == "transfer-seed":
        transfer_seed(args.archive, args.output)
    elif args.command == "transfer-verify":
        transfer_verify(args.archive, args.fixture, args.platform, args.output)
    else:
        publish(args.directory, args.version, args.sha)


if __name__ == "__main__":
    main()
