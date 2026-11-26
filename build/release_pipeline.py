# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

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
from portable_bundle import (
    add_component,
    archive_bundle,
    digest,
    stage_resources,
    verify_bundled_runtime,
)

TARGETS = ("windows-x86_64", "linux-x86_64", "linux-arm64")
TRANSFER_TARGETS = TARGETS
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

    def upload(self, release_id, path, *, label=None):
        endpoint = (
            f"/repos/{self.repository}/releases/{release_id}/assets?name="
            + urllib.parse.quote(path.name)
        )
        if label:
            endpoint += '&label=' + urllib.parse.quote(label)
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
    verify_bundled_runtime(bundle, platform)
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
    expected = {
        f"DBPulse-{version}-{label}.zip"
        for label in (*TARGETS, "Windows-Linux-Portable")
    }
    names = [asset["name"] for asset in manifest["assets"]]
    if len(names) != len(expected) or set(names) != expected:
        raise ValueError(
            "Release needs Windows x64, Linux x64, Linux ARM64 and the Windows/Linux bundle"
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


def git_output(root, *arguments):
    return subprocess.check_output(
        ["git", *arguments], cwd=root, text=True, encoding="utf-8"
    ).strip()


def previous_release(releases, version, sha, root=ROOT):
    """Find the latest published release belonging to this commit's history."""
    candidates = sorted(
        (
            item for item in releases
            if item["tag_name"] != version
            and not item["draft"]
            and not item.get("prerelease", False)
        ),
        key=lambda item: item.get("published_at") or "",
        reverse=True,
    )
    for item in candidates:
        tag = item["tag_name"]
        commit = git_output(
            root, "rev-parse", "--verify", "refs/tags/" + tag + "^{commit}"
        )
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, sha], cwd=root,
            check=False,
        )
        if result.returncode == 0:
            return tag
        if result.returncode != 1:
            raise RuntimeError("Cannot determine release ancestry: " + tag)
    return ""


def changelog_section(version, root=ROOT):
    path = root / "CHANGELOG.md"
    if not path.is_file():
        return ""
    match = re.search(
        r"^##[ \t]+\[?" + re.escape(version) + r"\]?(?=[ \t\r\n]|$)[^\n]*\n(.*?)(?=^## |\Z)",
        path.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL,
    )
    return match[1].strip() if match else ""


def release_notes(version, sha, base="", root=ROOT, repository=None):
    """Prefer version-specific editorial notes, otherwise group actual commits."""
    repository = repository or os.environ.get("GITHUB_REPOSITORY", "zhongxinxuhk/DBCheck")
    # Resolve revisions before invoking log, including annotated release tags.
    head = git_output(root, "rev-parse", "--verify", sha + "^{commit}")
    baseline = (
        git_output(root, "rev-parse", "--verify", base + "^{commit}") if base else ""
    )
    revision = baseline + ".." + head if baseline else head
    comparison = (
        f"https://github.com/{repository}/compare/{baseline}...{head}"
        if base else f"https://github.com/{repository}/commits/{head}"
    )
    details = changelog_section(version, root)
    origin = (
        f"迭代范围为 `{base}` 至 `{head[:12]}`。"
        if base else "首次正式发布，汇总截至本次提交的记录。"
    )
    if details:
        origin += "迭代详情取自 CHANGELOG.md 对应版本。"
    else:
        groups = {name: [] for name in ("新增功能", "功能改动", "BUG 修复", "其他更新")}
        commits = git_output(root, "log", "--no-merges", "--format=%H%x09%s", revision)
        records = commits.splitlines() if commits else []
        for record in records[:200]:
            commit, subject = record.split("\t", 1)
            match = re.match(r"(\w+)(?:\([^)]*\))?!?:\s*(.*)", subject)
            kind, title = (match[1].lower(), match[2]) if match else ("", subject)
            if kind == "feat" or re.match(r"^(新增|增加|添加)", title):
                group = "新增功能"
            elif kind in ("fix", "bugfix", "hotfix", "revert") or re.match(
                r"^(修复|修正|解决)", title
            ):
                group = "BUG 修复"
            elif kind in ("perf", "refactor", "style") or re.match(
                r"^(调整|优化|改进|变更|重构|更新)", title
            ):
                group = "功能改动"
            else:
                group = "其他更新"
            groups[group].append(
                f"- {subject} ([{commit[:7]}](https://github.com/{repository}/commit/{commit}))"
            )
        details = "\n\n".join(
            "### " + name + "\n\n" + ("\n".join(items) if items else "- 本次无相关提交记录。")
            for name, items in groups.items()
        )
        origin += "迭代详情按提交标题自动归类。"
        if len(records) > 200:
            details += f"\n\n共 {len(records)} 条提交，展示最近 200 条，其余见完整变更记录。"
    return f"""{MARKER}
# DBPulse {version}

源提交为 `{head}`。{origin}

## 迭代详情

{details}

[查看完整变更记录]({comparison})

## 安装包与平台

| 安装包 | 适用平台 |
| --- | --- |
| `DBPulse-{version}-windows-x86_64.zip` | Windows x64 |
| `DBPulse-{version}-linux-x86_64.zip` | Linux x64，glibc 2.28 及以上 |
| `DBPulse-{version}-linux-arm64.zip` | Linux ARM64，glibc 2.28 及以上 |
| `DBPulse-{version}-Windows-Linux-Portable.zip` | Windows x64、Linux x64 / ARM64 共用便携目录 |

## 安装与升级说明

- Windows 双击 `start.bat`，Linux 执行 `bash start.sh`。
- 首次安装账号 `admin`，密码 `admin123`。
- 各平台包含 Java 运行时，共用 data、数据库密码密钥、台账、账号、任务、报告和自定义规则。
- 升级前停止服务并备份整个原目录，保留 `data/` 与其中的密码密钥。
- Windows 配置迁移至 Linux 时，停止软件后复制整个 Windows-Linux-Portable 目录，再执行 `bash start.sh`。
- 下载后使用 `SHA256SUMS.txt` 校验文件。

## 发布验证

Windows x64、Linux x64 和 Linux ARM64 的运行时检查与 Windows 配置迁移验证全部通过后发布。
"""


def publish(directory, version, sha):
    manifest = verify_assets(directory, version, sha)
    github = GitHub()
    releases = github.pages("releases")
    release = next(
        (value for value in releases if value["tag_name"] == version),
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
    base = previous_release(releases, version, sha)
    notes = release_notes(version, sha, base)
    (directory / "release-notes.md").write_text(notes, encoding="utf-8")
    if ref is None:
        github.request("git/refs", "POST", {"ref": "refs/tags/" + version, "sha": sha})
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




def staging_release(github, *, create=False):
    run_id = os.environ['GITHUB_RUN_ID']
    sha = os.environ['GITHUB_SHA']
    if not run_id.isdigit() or not re.fullmatch(r'[a-f0-9]{40}', sha):
        raise ValueError('Invalid staging run identity')
    tag = 'ci-build-' + run_id
    existing = next((item for item in github.pages('releases') if item['tag_name'] == tag), None)
    body = f'{MARKER}\nTemporary CI transport for run {run_id}.\n源提交为 `{sha}`。'
    if existing:
        if not existing['draft'] or existing.get('body') != body:
            raise ValueError('Staging release identity does not match this run')
        return existing
    if not create:
        return None
    return github.request('releases', 'POST', {'tag_name': tag, 'target_commitish': sha,
                          'name': 'CI staging ' + run_id, 'body': body, 'draft': True, 'prerelease': True})


def stage_upload(root, name, entries=None, github=None):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
        raise ValueError('Invalid staging asset name')
    github = github or GitHub()
    staging = staging_release(github)
    if not staging:
        raise ValueError('Private release transport was not initialized')
    root = root.resolve(strict=True)
    files = set()
    for entry in entries or ['.']:
        path = root / entry
        if not path.resolve().is_relative_to(root) or not path.exists():
            raise ValueError('Staging entry is missing or outside its root')
        files.update(path.rglob('*') if path.is_dir() else [path])
    files = sorted(path for path in files if path.is_file())
    if not files:
        raise ValueError('Staging asset has no files')
    with tempfile.TemporaryDirectory(prefix='dbpulse-stage-') as folder:
        archive = Path(folder) / (name + '.zip')
        with zipfile.ZipFile(archive, 'w') as package:
            for path in files:
                if not path.resolve().is_relative_to(root):
                    raise ValueError('Staging file points outside its root')
                package.write(path, path.relative_to(root), compress_type=(
                    zipfile.ZIP_STORED if path.suffix == '.zip' else zipfile.ZIP_DEFLATED))
        if archive.stat().st_size >= 2 * 1024 ** 3:
            raise ValueError('Staging asset exceeds the GitHub Release 2 GiB limit')
        for asset in github.pages(f"releases/{staging['id']}/assets"):
            if asset['name'] == archive.name:
                github.request('releases/assets/' + str(asset['id']), 'DELETE')
        github.upload(staging['id'], archive, label='sha256:' + digest(archive))
    print(json.dumps({'staged': name, 'files': len(files), 'draft': True}))


class _NoAssetRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_staged_asset(github, asset, destination):
    checksum = asset.get('digest') or asset.get('label') or ''
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', checksum):
        raise ValueError('Staging asset has no SHA-256 receipt')
    url = f"https://api.github.com/repos/{github.repository}/releases/assets/{asset['id']}"
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + github.token,
                                     'Accept': 'application/octet-stream', 'User-Agent': 'DBPulse-release-pipeline'})
    try:
        response = urllib.request.build_opener(_NoAssetRedirect).open(request, timeout=90)
    except urllib.error.HTTPError as error:
        if error.code not in (301, 302, 303, 307, 308):
            raise
        location = error.headers.get('Location', '')
        if urllib.parse.urlsplit(location).scheme != 'https':
            raise ValueError('Staging download requires HTTPS') from error
        # The signed asset URL is fetched without forwarding the GitHub token.
        response = urllib.request.urlopen(urllib.request.Request(location, headers={
            'User-Agent': 'DBPulse-release-pipeline'}), timeout=90)
    size = 0
    with response, destination.open('wb') as stream:
        for chunk in iter(lambda: response.read(1024 * 1024), b''):
            size += len(chunk)
            if size > asset['size']:
                raise ValueError('Staging download exceeds its declared size')
            stream.write(chunk)
    if size != asset['size'] or digest(destination) != checksum.removeprefix('sha256:'):
        raise ValueError('Staging download SHA-256 or size mismatch')


def stage_download(output, name, github=None):
    github = github or GitHub()
    staging = staging_release(github)
    if not staging:
        raise ValueError('Private release transport is missing')
    asset = next((item for item in github.pages(f"releases/{staging['id']}/assets")
                  if item['name'] == name + '.zip' and item.get('state') == 'uploaded'), None)
    if not asset:
        raise ValueError('Required staging asset is missing: ' + name)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='dbpulse-stage-') as folder:
        archive = Path(folder) / 'input.zip'
        download_staged_asset(github, asset, archive)
        with zipfile.ZipFile(archive) as package:
            for info in package.infolist():
                path = (output / info.filename).resolve()
                if not path.is_relative_to(output.resolve()) or '\\' in info.filename or ':' in info.filename:
                    raise ValueError('Unsafe staging archive path')
            package.extractall(output)
    print(json.dumps({'restored': name, 'sha256': asset.get('digest') or asset.get('label')}))


def stage_cleanup(version, sha, github=None):
    github = github or GitHub()
    published = next((item for item in github.pages('releases') if item['tag_name'] == version), None)
    if not published or published['draft'] or MARKER not in published.get('body', '') or f'源提交为 `{sha}`' not in published.get('body', ''):
        raise ValueError('Staging cleanup requires a published release for this source')
    expected = {f'DBPulse-{version}-{platform}.zip' for platform in (*TARGETS, 'Windows-Linux-Portable')}
    expected.update({'release-manifest.json', 'SHA256SUMS.txt'})
    uploaded = {item['name'] for item in published.get('assets', [])
                if item.get('state') == 'uploaded' and item.get('size', 0) > 0}
    if not expected <= uploaded:
        raise ValueError('Published assets are incomplete; preserve the staging release')
    staging = staging_release(github)
    if not staging:
        return
    github.request('releases/' + str(staging['id']), 'DELETE')
    try:
        reference = github.request('git/ref/tags/' + staging['tag_name'])
        if reference['object']['sha'] == sha:
            github.request('git/refs/tags/' + staging['tag_name'], 'DELETE')
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    print(json.dumps({'staging_removed': staging['tag_name']}))


def cleanup_artifacts(github=None, *, apply=False, current_run=None):
    """Reclaim temporary artifacts only after their exact source has a verified release."""
    github = github or GitHub()
    releases = {}
    for release in github.pages('releases'):
        tag = release.get('tag_name', '')
        body = release.get('body') or ''
        source = re.search(r'源提交为 `([0-9a-f]{40})`', body)
        if release.get('draft') or release.get('prerelease') or not TRAIN.fullmatch(tag) or MARKER not in body or not source:
            continue
        assets = {asset['name'] for asset in release.get('assets', [])
                  if asset.get('state') == 'uploaded' and asset.get('size', 0) > 0}
        expected = {f'DBPulse-{tag}-{platform}.zip' for platform in (*TARGETS, 'Windows-Linux-Portable')}
        expected.update({'release-manifest.json', 'SHA256SUMS.txt'})
        if not expected <= assets:
            continue
        try:
            reference = github.request('git/ref/tags/' + tag)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                continue
            raise
        if reference['object'].get('type') != 'commit' or reference['object']['sha'] != source[1]:
            continue
        releases[source[1]] = tag
    artifacts = []
    page = 1
    while True:
        batch = github.request(f'actions/artifacts?per_page=100&page={page}')['artifacts']
        artifacts.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    runs = {}
    selected = []
    for artifact in artifacts:
        run = artifact.get('workflow_run') or {}
        tag = releases.get(run.get('head_sha'))
        if not tag or artifact.get('expired'):
            continue
        names = {f'portable-component-{platform}' for platform in TARGETS}
        names |= {f'transfer-verification-{platform}' for platform in TARGETS}
        names |= {'windows-transfer-fixture', 'release-candidate-' + tag}
        if artifact['name'] not in names:
            continue
        run_id = run['id']
        if run_id not in runs:
            runs[run_id] = github.request('actions/runs/' + str(run_id))
        details = runs[run_id]
        if details.get('path', '').split('@')[0] != '.github/workflows/build-release.yml' or details.get('head_branch') != 'main':
            continue
        finished = details.get('status') == 'completed' and details.get('conclusion') == 'success'
        publishing = (str(run_id) == str(current_run) == os.environ.get('GITHUB_RUN_ID')
                      and run['head_sha'] == os.environ.get('GITHUB_SHA')
                      and details.get('status') == 'in_progress')
        if details.get('head_sha') != run['head_sha'] or not (finished or publishing):
            continue
        selected.append(artifact)
    removed = 0
    for artifact in selected if apply else []:
        try:
            github.request('actions/artifacts/' + str(artifact['id']), 'DELETE')
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        removed += 1
    result = {'apply': apply, 'selected': len(selected), 'removed': removed,
              'bytes': sum(artifact['size_in_bytes'] for artifact in selected)}
    print(json.dumps(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")
    commands.add_parser('stage-init')
    stage_put = commands.add_parser('stage-upload')
    stage_put.add_argument('--root', type=Path, required=True)
    stage_put.add_argument('--name', required=True)
    stage_put.add_argument('--entry', action='append')
    stage_get = commands.add_parser('stage-download')
    stage_get.add_argument('--output', type=Path, required=True)
    stage_get.add_argument('--name', required=True)
    stage_remove = commands.add_parser('stage-cleanup')
    stage_remove.add_argument('--version', required=True)
    stage_remove.add_argument('--sha', required=True)
    cleanup = commands.add_parser('cleanup-artifacts')
    cleanup.add_argument('--apply', action='store_true')
    cleanup.add_argument('--current-run', default=None)
    notes_command = commands.add_parser("notes", help="Preview release notes without publishing")
    notes_command.add_argument("--version", required=True)
    notes_command.add_argument("--sha", default="HEAD")
    notes_command.add_argument("--base", default="")
    notes_command.add_argument("--output", type=Path, required=True)
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
    elif args.command == 'stage-init':
        value = staging_release(GitHub(), create=True)
        print(json.dumps({"staging": value["tag_name"], "draft": True, "url": value.get("html_url")}))
    elif args.command == 'stage-upload':
        stage_upload(args.root, args.name, args.entry)
    elif args.command == 'stage-download':
        stage_download(args.output, args.name)
    elif args.command == 'stage-cleanup':
        stage_cleanup(args.version, args.sha)
    elif args.command == 'cleanup-artifacts':
        cleanup_artifacts(apply=args.apply, current_run=args.current_run)
    elif args.command == "notes":
        args.output.write_text(
            release_notes(args.version, args.sha, args.base), encoding="utf-8"
        )
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
