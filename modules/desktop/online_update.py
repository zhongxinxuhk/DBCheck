# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Fetch official Windows updates and pass verified ZIPs to the existing installer."""

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from modules.core.brand import PROJECT_URL
from modules.desktop.offline_update import (
    MAX_TOTAL_BYTES, PLATFORM, OfflineUpdateError, PreparedUpdate, _read_json, _version_key,
)


REPOSITORY = urlsplit(PROJECT_URL).path.strip("/")
RELEASES_URL = PROJECT_URL + "/releases"
_API = "https://api.github.com/repos/" + REPOSITORY + "/releases"
_DOWNLOAD_HOSTS = {
    "api.github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
}
_RELEASE_METADATA_LIMIT = 2 * 1024 * 1024
_CHECKSUM_LIMIT = 64 * 1024
_VERSION_TAG = re.compile(r"v?[0-9]+(?:\.[0-9]+){2,3}", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)


class GitHubAuthenticationRequired(OfflineUpdateError):
    """GitHub requires a token to read the release."""


class _GitHubRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urlsplit(newurl)
        if (url.scheme != "https" or url.hostname not in _DOWNLOAD_HOSTS
                or url.username is not None or url.password is not None
                or url.port not in (None, 443)):
            raise OfflineUpdateError("GitHub 更新下载的重定向地址不受支持")
        # Authorization is deliberately an unredirected header on the original request.
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class ReleaseUpdate:
    version: str
    name: str
    asset_id: int
    size: int
    sha256: str | None
    checksum_asset_id: int | None


def _asset_id(asset):
    value = asset.get("id")
    if type(value) is not int or value <= 0:
        raise OfflineUpdateError("GitHub 发行附件编号无效")
    return value


class GitHubUpdate:
    def __init__(self, installer, token=None):
        self.installer = installer
        self._token = (os.environ.get("DBPULSE_GITHUB_TOKEN", "") if token is None else token).strip()
        if self._token and not re.fullmatch(r"[A-Za-z0-9_]+", self._token):
            raise OfflineUpdateError("GitHub 访问令牌格式不正确")
        self._opener = build_opener(_GitHubRedirect())

    def _request(self, resource, accept):
        request = Request(
            _API + resource,
            headers={"Accept": accept, "User-Agent": "DBPulse-Online-Update"},
        )
        if self._token:
            request.add_unredirected_header("Authorization", "Bearer " + self._token)
        try:
            return self._opener.open(request, timeout=35)
        except HTTPError as error:
            code = error.code
            rate_limited = error.headers.get("X-RateLimit-Remaining") == "0" or code == 429
            error.close()
            if rate_limited:
                raise OfflineUpdateError("GitHub API 请求达到频率限制，请稍后重试") from None
            if code in {401, 403, 404}:
                if not self._token:
                    raise GitHubAuthenticationRequired("需要 GitHub 仓库的发行版读取权限") from None
                raise OfflineUpdateError(
                    "GitHub 无法读取本项目发行版，请核对令牌的仓库范围和 Contents 读取权限"
                ) from None
            raise OfflineUpdateError(f"GitHub 更新请求失败（HTTP {code}）") from None
        except (URLError, OSError):
            raise OfflineUpdateError("无法连接 GitHub 更新服务，请检查网络后重试") from None

    @staticmethod
    def _read(response, limit):
        data = response.read(limit + 1)
        if len(data) > limit:
            raise OfflineUpdateError("GitHub 返回的发行信息超过允许大小")
        return data

    def _release_suffix(self):
        if self.installer.mode == "portable":
            marker = _read_json(self.installer.root / "dbpulse-portable.json")
            platforms = marker.get("required_platforms") or marker.get("platforms")
            if isinstance(platforms, (list, dict)) and list(platforms) == [PLATFORM]:
                return "windows-x86_64"
            return "windows-linux-portable"
        return "windows-x86_64"

    def latest(self):
        """Return an update for this install mode, or None when already current."""
        with self._request("/latest", "application/vnd.github+json") as response:
            try:
                release = json.loads(self._read(response, _RELEASE_METADATA_LIMIT))
            except (UnicodeError, ValueError):
                raise OfflineUpdateError("GitHub 发行信息格式不正确") from None
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            raise OfflineUpdateError("GitHub 发行信息中没有可用的正式版本")
        version = release.get("tag_name", "")
        if not isinstance(version, str) or not _VERSION_TAG.fullmatch(version):
            raise OfflineUpdateError("GitHub 发行版的版本号格式不受支持")
        current = self.installer._current_version()
        if not _VERSION_TAG.fullmatch(current):
            raise OfflineUpdateError("无法识别当前安装版本，请使用完整更新包手动更新")
        if _version_key(version) <= _version_key(current):
            return None

        assets = release.get("assets")
        if not isinstance(assets, list):
            raise OfflineUpdateError("GitHub 发行版缺少附件清单")
        bare = version.removeprefix("v")
        stems = {bare, "v" + bare}
        suffix = self._release_suffix()
        names = {f"dbpulse-{tag}-{suffix}.zip" for tag in stems}
        if self.installer.mode == "standalone":
            names.update(f"dbpulse-windows-x86_64-{tag}.zip" for tag in stems)
        uploaded = [asset for asset in assets if isinstance(asset, dict)
                    and isinstance(asset.get("name"), str) and asset.get("state") == "uploaded"]
        matching = [asset for asset in uploaded if asset["name"].lower() in names]
        if len(matching) != 1:
            raise OfflineUpdateError(f"GitHub 发行版 {version} 没有唯一匹配的 {suffix} 正式更新包")
        package = matching[0]
        size = package.get("size")
        if type(size) is not int or not 0 < size <= MAX_TOTAL_BYTES:
            raise OfflineUpdateError("GitHub 更新包大小无效")
        raw_digest = package.get("digest")
        digest = None
        checksum_id = None
        if raw_digest:
            if not isinstance(raw_digest, str) or not raw_digest.startswith("sha256:"):
                raise OfflineUpdateError("GitHub 更新包摘要格式不受支持")
            digest = raw_digest.removeprefix("sha256:")
            if not _SHA256.fullmatch(digest):
                raise OfflineUpdateError("GitHub 更新包摘要格式不正确")
        else:
            files = [asset for asset in uploaded if asset["name"] == "SHA256SUMS.txt"]
            if len(files) != 1:
                raise OfflineUpdateError("GitHub 发行版缺少更新包 SHA-256 校验值")
            checksum_id = _asset_id(files[0])
        return ReleaseUpdate(version, package["name"], _asset_id(package), size, digest, checksum_id)

    def _checksum(self, release):
        if release.sha256:
            return release.sha256
        with self._request(f"/assets/{release.checksum_asset_id}", "application/octet-stream") as response:
            try:
                lines = self._read(response, _CHECKSUM_LIMIT).decode("utf-8-sig").splitlines()
            except UnicodeError:
                raise OfflineUpdateError("发行版 SHA256SUMS.txt 格式不正确") from None
        checksums = []
        for line in lines:
            parts = line.split(maxsplit=1)
            if len(parts) == 2 and parts[1].removeprefix("*") == release.name:
                checksums.append(parts[0].lower())
        if len(checksums) != 1 or not _SHA256.fullmatch(checksums[0]):
            raise OfflineUpdateError("发行版 SHA256SUMS.txt 中缺少唯一匹配的更新包校验值")
        return checksums[0]

    def prepare(self, release, progress=None) -> PreparedUpdate:
        """Download, verify and stage without stopping or modifying the installation."""
        notify = progress or (lambda _: None)
        self.installer.backups.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".download-", dir=self.installer.backups) as temp:
            folder = Path(temp)
            if shutil.disk_usage(folder).free < release.size:
                raise OfflineUpdateError("磁盘剩余空间不足，无法下载 GitHub 更新包")
            source = folder / release.name
            checksum = self._checksum(release)
            digest = hashlib.sha256()
            received = 0
            next_progress = 0
            notify(f"正在下载 GitHub 发行版 {release.version} · {release.name}")
            try:
                with self._request(f"/assets/{release.asset_id}", "application/octet-stream") as response, source.open("wb") as file:
                    while block := response.read(1024 * 1024):
                        received += len(block)
                        if received > release.size:
                            raise OfflineUpdateError("GitHub 更新包超出发行版声明的大小")
                        digest.update(block)
                        file.write(block)
                        percent = received * 100 // release.size
                        if percent >= next_progress:
                            notify(f"已下载 {percent}%")
                            next_progress = percent + 10
            except (URLError, OSError):
                raise OfflineUpdateError("下载 GitHub 更新包失败，请检查网络或磁盘后重试") from None
            if received != release.size or digest.hexdigest() != checksum:
                raise OfflineUpdateError("GitHub 更新包的大小或 SHA-256 校验不匹配，已取消更新")
            notify("GitHub 更新包 SHA-256 校验通过，正在校验包内文件…")
            prepared = self.installer.prepare(source, notify)
            if (_version_key(release.version) != _version_key(prepared.version)
                    or _version_key(prepared.version) <= _version_key(prepared.current_version)):
                prepared.cleanup()
                raise OfflineUpdateError("更新包版本和发行版不一致，或本机已是同一或更高版本")
            return prepared
