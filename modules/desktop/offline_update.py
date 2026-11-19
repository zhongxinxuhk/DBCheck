# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Safe offline updates from a locally supplied DBPulse Windows ZIP package.

The updater deliberately accepts only complete Windows distributions:

* a standalone Windows package containing ``dbcheck.exe``, ``DBPulse.exe`` and
  ``_internal/``; or
* a portable multi-platform package containing ``dbpulse-portable.json`` and
  ``bin/windows-x86_64/``.

Runtime data (``data/``) is never part of an update package and is never
replaced.  Program files are moved aside before the new files are installed, so
a failed copy, failed start or failed health check can be rolled back to the
exact previous installation.  A running ``DBPulse.exe`` cannot overwrite
itself on Windows.  The package's new ``dbcheck.exe`` is therefore launched
from the staging directory as a detached helper after the launcher exits; it
replaces the files and restarts the new launcher.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

FORMAT = "dbpulse-offline-update"
PLATFORM = "windows-x86_64"
MAX_MEMBERS = 60000
MAX_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 5000
RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
PROTECTED_NAMES = {"data", "backups", ".dbcheck.lock", "desktop_process.json"}
FORBIDDEN_ROOT_FILES = {
    ".db_key",
    ".dbcheck.lock",
    "bootstrap_admin_password",
    "DBPulse.exe.new",
    "desktop_process.json",
    "instances.db",
    "inspection.db",
    "um_rbac.db",
}
_STAGING_PREFIX = ".staging-"
_RUNTIME_DATA_PREFIXES = (
    ("data",),
    ("backups",),
    ("_internal", "data"),
    ("bin", PLATFORM, "data"),
    ("bin", PLATFORM, "_internal", "data"),
    ("runtime", PLATFORM, "data"),
)


class OfflineUpdateError(RuntimeError):
    """An update was rejected or failed and the previous install was restored."""


class OfflineUpdateRecoveryRequired(OfflineUpdateError):
    """Rollback could not complete; the previous files remain in the backup."""


def _hash(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _version_key(value: str) -> tuple[int, ...]:
    parts = tuple(int(part) for part in re.findall(r"\d+", str(value or ""))[:4])
    return parts + (0,) * (4 - len(parts)) if parts else ()


def _find_portable_root(directory: Path) -> Path | None:
    configured = os.environ.get("DBPULSE_PORTABLE_HOME")
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if (candidate / "dbpulse-portable.json").is_file():
            return candidate
    directory = Path(directory).resolve()
    for parent in (directory, *directory.parents):
        if (parent / "dbpulse-portable.json").is_file():
            return parent
    return None


def _safe_member(name: str) -> PurePosixPath:
    """Validate a ZIP member path for extraction on Windows."""
    if not name or len(name) > 240:
        raise OfflineUpdateError("离线包包含过长或为空的文件路径")
    if "\\" in name or ":" in name or "\x00" in name:
        raise OfflineUpdateError("离线包包含非法的文件路径：" + name)
    if any(ord(character) < 32 for character in name):
        raise OfflineUpdateError("离线包包含控制字符的文件路径")
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name or not path.parts:
        raise OfflineUpdateError("离线包包含非法的绝对路径：" + name)
    for part in path.parts:
        if part in {"", ".", ".."}:
            raise OfflineUpdateError("离线包包含路径穿越项：" + name)
        if part.endswith((" ", ".")):
            raise OfflineUpdateError("离线包包含 Windows 不允许的文件名：" + name)
        if any(character in '<>"|?*' for character in part):
            raise OfflineUpdateError("离线包包含 Windows 不允许的文件名字符：" + name)
        if part.split(".")[0].upper() in RESERVED_NAMES:
            raise OfflineUpdateError("离线包包含 Windows 保留文件名：" + name)
    return path


def _validate_archive(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = archive.infolist()
    if not infos:
        raise OfflineUpdateError("离线包为空")
    if len(infos) > MAX_MEMBERS:
        raise OfflineUpdateError("离线包文件数量超出限制")
    total = 0
    files: list[zipfile.ZipInfo] = []
    names: set[str] = set()
    file_names: set[str] = set()
    for info in infos:
        if info.flag_bits & 0x1:
            raise OfflineUpdateError("离线包不能包含加密文件")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise OfflineUpdateError("离线包不能包含符号链接")
        directory = info.filename.endswith("/")
        name = info.filename[:-1] if directory else info.filename
        path = _safe_member(name)
        key = unicodedata.normalize("NFC", path.as_posix()).casefold()
        if key in names:
            raise OfflineUpdateError("离线包包含重复或大小写冲突的文件名：" + name)
        names.add(key)
        if directory:
            continue
        file_names.add(key)
        if info.file_size < 0 or info.file_size > MAX_FILE_BYTES:
            raise OfflineUpdateError("离线包单个文件过大：" + name)
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise OfflineUpdateError("离线包解压后体积超出限制")
        if (
            info.file_size > 64 * 1024 * 1024
            and info.compress_size
            and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
        ):
            raise OfflineUpdateError("离线包压缩比异常，已拒绝解压")
        files.append(info)
    if not files:
        raise OfflineUpdateError("离线包中没有可安装的文件")
    # A file and a directory cannot share the same path.
    for name in file_names:
        parts = PurePosixPath(name).parts
        for index in range(1, len(parts)):
            if "/".join(parts[:index]).casefold() in file_names:
                raise OfflineUpdateError("离线包文件路径相互冲突：" + name)
    return files


def _validate_no_runtime_data(root: Path) -> None:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if any(parts[: len(prefix)] == prefix for prefix in _RUNTIME_DATA_PREFIXES):
            raise OfflineUpdateError(
                "离线包不应包含运行时数据目录 data/ 或 backups/：" + path.name
            )
        if parts[0] in FORBIDDEN_ROOT_FILES:
            raise OfflineUpdateError(
                "离线包不应包含运行时状态文件：" + path.name
            )
    for path in root.rglob("*"):
        if path.is_symlink():
            raise OfflineUpdateError("离线包不能包含符号链接：" + path.name)


def _extract_archive(
    source: Path, destination: Path, progress=None
) -> tuple[int, int]:
    notify = progress or (lambda message: None)
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as error:
        raise OfflineUpdateError("无法读取离线包，请确认 ZIP 文件完整") from error
    with archive:
        files = _validate_archive(archive)
        required = sum(info.file_size for info in files)
        if required + 64 * 1024 * 1024 > shutil.disk_usage(destination).free:
            raise OfflineUpdateError("磁盘可用空间不足，无法解压离线更新包")
        raw = destination / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        notify(f"正在校验并解压离线包（{len(files)} 个文件）…")
        copied = 0
        for index, info in enumerate(files, 1):
            relative = _safe_member(info.filename)
            target = raw.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise OfflineUpdateError("离线包包含重复文件：" + info.filename)
            try:
                with archive.open(info) as reader, target.open("xb") as writer:
                    while True:
                        block = reader.read(1024 * 1024)
                        if not block:
                            break
                        writer.write(block)
                        copied += len(block)
            except (
                OSError,
                zipfile.BadZipFile,
                RuntimeError,
                NotImplementedError,
            ) as error:
                raise OfflineUpdateError(
                    "离线包解压或 CRC 校验失败：" + info.filename
                ) from error
            if target.stat().st_size != info.file_size:
                raise OfflineUpdateError("离线包文件长度不正确：" + info.filename)
            if index == len(files) or index % 250 == 0:
                notify(f"已解压 {index}/{len(files)} 个文件…")
    return len(files), copied


def _candidate_roots(base: Path) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    for current, directories, files in os.walk(base):
        current_path = Path(current)
        relative_parts = current_path.relative_to(base).parts
        depth = len(relative_parts)
        if depth >= 3:
            directories[:] = []
        if "dbpulse-portable.json" in files and not any(
            part in {"_internal", "data", "backups"} for part in relative_parts
        ):
            candidates.append(("portable", current_path))
        elif "dbcheck.exe" in files and "DBPulse.exe" in files:
            candidates.append(("standalone", current_path))
    return candidates


def _detect_layout(stage: Path) -> tuple[str, Path]:
    candidates = _candidate_roots(stage / "raw")
    if not candidates:
        raise OfflineUpdateError(
            "不是有效的 DBPulse Windows 离线包：未找到 dbpulse-portable.json，"
            "或 dbcheck.exe 与 DBPulse.exe"
        )
    portable = [item for item in candidates if item[0] == "portable"]
    selected = portable or candidates
    shallowest = min(len(path.relative_to(stage / "raw").parts) for _, path in selected)
    matches = [
        (mode, path)
        for mode, path in selected
        if len(path.relative_to(stage / "raw").parts) == shallowest
    ]
    if len(matches) != 1:
        raise OfflineUpdateError("离线包包含多个可安装目录，无法确定更新目标")
    return matches[0]


def _pe_amd64(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            header = stream.read(64)
            if len(header) < 64 or header[:2] != b"MZ":
                return False
            offset = int.from_bytes(header[60:64], "little")
            stream.seek(offset)
            signature = stream.read(6)
        return (
            len(signature) == 6
            and signature[:4] == b"PE\x00\x00"
            and int.from_bytes(signature[4:6], "little") == 0x8664
        )
    except OSError:
        return False


def _package_version(root: Path) -> str:
    for path in (
        root / "version.json",
        root / "_internal/modules/config/version.json",
        root / "modules/config/version.json",
    ):
        value = _read_json(path).get("version")
        if value:
            return str(value).strip()
    marker = _read_json(root / "dbpulse-portable.json")
    return str(marker.get("version") or "").strip()


def _validate_package(mode: str, root: Path) -> dict:
    _validate_no_runtime_data(root)
    capability = _read_json(root / "offline-update.json")
    if capability.get("schema") != 1 or capability.get("helper") != "--offline-update-helper":
        raise OfflineUpdateError(
            "离线包缺少自助更新能力标记 offline-update.json，请使用新版构建的完整离线包"
        )
    for name in ("dbcheck.exe", "DBPulse.exe"):
        path = root / name if mode == "standalone" else root / "bin" / PLATFORM / name
        if not path.is_file():
            raise OfflineUpdateError("离线包缺少 " + name)
        if not _pe_amd64(path):
            raise OfflineUpdateError(name + " 不是有效的 Windows x86_64 程序")
    details: dict = {}
    if mode == "standalone":
        if not (root / "_internal").is_dir():
            raise OfflineUpdateError("离线包缺少 _internal 程序目录")
        details["executable"] = "dbcheck.exe"
    else:
        marker_path = root / "dbpulse-portable.json"
        marker = _read_json(marker_path)
        if marker.get("layout") != "shared-multi-platform":
            raise OfflineUpdateError("离线包的便携版清单格式不正确")
        platforms = marker.get("platforms")
        entry = platforms.get(PLATFORM) if isinstance(platforms, dict) else None
        if not isinstance(entry, dict):
            raise OfflineUpdateError("离线包不包含 Windows x86_64 组件")
        executable = str(entry.get("executable") or "").replace("\\", "/")
        if executable and executable != f"bin/{PLATFORM}/dbcheck.exe":
            raise OfflineUpdateError("离线包的 Windows 程序路径不正确")
        for field, relative in (
            ("sha256", f"bin/{PLATFORM}/dbcheck.exe"),
            ("launcher_sha256", f"bin/{PLATFORM}/DBPulse.exe"),
        ):
            expected = str(entry.get(field) or "").strip().lower()
            if not expected:
                raise OfflineUpdateError("便携版清单缺少文件哈希：" + relative)
            if _hash(root / relative) != expected:
                raise OfflineUpdateError("离线包校验失败：" + relative)
        if not (root / "bin" / PLATFORM / "_internal").is_dir():
            raise OfflineUpdateError("便携包缺少 _internal 运行目录")
        runtime = root / "runtime" / PLATFORM
        if not runtime.is_dir() or not any(runtime.rglob("java.exe")):
            raise OfflineUpdateError("便携包缺少 Windows 内置 Java 运行时")
        details["marker"] = marker
        details["executable"] = f"bin/{PLATFORM}/dbcheck.exe"
    version = _package_version(root)
    if not version:
        raise OfflineUpdateError("离线包缺少 version.json 版本信息")
    details["version"] = version
    return details


def _prune_old_backups(backups: Path, keep: int = 5) -> None:
    try:
        candidates = sorted(
            (
                path
                for path in backups.iterdir()
                if path.is_dir()
                and path.name.startswith("update-")
                and (path / "transaction.json").is_file()
            ),
            key=lambda path: path.name,
            reverse=True,
        )
    except OSError:
        return
    for path in candidates[keep:]:
        shutil.rmtree(path, ignore_errors=True)


@dataclass
class PreparedUpdate:
    """A fully validated and extracted offline package ready to apply."""

    source: Path
    stage: Path
    root: Path
    mode: str
    version: str
    current_version: str
    files: int
    bytes: int
    backup: Path
    details: dict = field(default_factory=dict)

    @property
    def has_launcher(self) -> bool:
        return (self.root / "DBPulse.exe").is_file() or (
            self.root / "bin" / PLATFORM / "DBPulse.exe"
        ).is_file()

    @property
    def launcher_source(self) -> Path:
        if self.mode == "portable":
            return self.root / "bin" / PLATFORM / "DBPulse.exe"
        return self.root / "DBPulse.exe"

    @property
    def helper_executable(self) -> Path:
        if self.mode == "portable":
            return self.root / "bin" / PLATFORM / "dbcheck.exe"
        return self.root / "dbcheck.exe"

    def summary(self) -> dict:
        return {
            "source": str(self.source),
            "version": self.version,
            "current_version": self.current_version,
            "mode": self.mode,
            "mode_label": "Windows 便携版" if self.mode == "portable" else "Windows 安装版",
            "files": self.files,
            "bytes": self.bytes,
            "backup": str(self.backup),
            "has_launcher": self.has_launcher,
        }

    def cleanup(self) -> None:
        shutil.rmtree(self.stage, ignore_errors=True)


@dataclass
class _Target:
    relative: Path
    source: Path
    is_dir: bool


class OfflineUpdate:
    """Prepare, apply and roll back a local Windows offline update."""

    def __init__(self, installation, data_root=None):
        self.installation = Path(installation).resolve()
        self.portable_root = _find_portable_root(self.installation)
        self.root = self.portable_root or self.installation
        self.data_root = (
            Path(data_root).resolve() if data_root else self.root
        )
        self.mode = "portable" if self.portable_root else "standalone"
        self.backups = self.data_root / "backups" / "offline_update"

    def _current_version(self) -> str:
        for path in (
            self.root / "version.json",
            self.root / "_internal/modules/config/version.json",
            self.root / "modules/config/version.json",
        ):
            value = _read_json(path).get("version")
            if value:
                return str(value).strip()
        marker = _read_json(self.root / "dbpulse-portable.json")
        return str(marker.get("version") or "").strip()

    def prepare(self, source, progress=None) -> PreparedUpdate:
        notify = progress or (lambda message: None)
        source = Path(source).expanduser().resolve()
        if not source.is_file():
            raise OfflineUpdateError("未找到所选的离线更新包")
        if source.suffix.lower() != ".zip":
            raise OfflineUpdateError("离线更新包必须是 ZIP 文件")
        self.backups.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        stage = self.backups / (
            _STAGING_PREFIX + stamp + "-" + uuid.uuid4().hex[:8]
        )
        stage.mkdir(parents=True)
        self._prune_staging(keep=stage)
        try:
            files, size = _extract_archive(source, stage, notify)
            mode, root = _detect_layout(stage)
            if mode != self.mode:
                expected = (
                    "便携版离线包（包含 dbpulse-portable.json）"
                    if self.mode == "portable"
                    else "Windows 安装版离线包（包含 _internal 目录）"
                )
                raise OfflineUpdateError("当前安装类型与离线包不匹配，请使用" + expected)
            details = _validate_package(mode, root)
            package_version = details["version"]
            current_version = self._current_version()
            package_key = _version_key(package_version)
            current_key = _version_key(current_version)
            if package_key and current_key and package_key < current_key:
                raise OfflineUpdateError(
                    f"离线包版本 {package_version} 低于当前版本 {current_version}，"
                    "已拒绝降级更新"
                )
            backup = self.backups / (
                "update-" + stamp + "-" + uuid.uuid4().hex[:8]
            )
            notify(
                f"离线包校验通过：{package_version}，共 {files} 个文件"
            )
            return PreparedUpdate(
                source=source,
                stage=stage,
                root=root,
                mode=mode,
                version=package_version,
                current_version=current_version,
                files=files,
                bytes=size,
                backup=backup,
                details=details,
            )
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def _prune_staging(self, keep: Path | None = None, older_than: int = 86400) -> None:
        try:
            candidates = list(self.backups.glob(_STAGING_PREFIX + "*"))
        except OSError:
            return
        now = time.time()
        for path in candidates:
            if keep is not None and path.resolve() == keep.resolve():
                continue
            try:
                if now - path.stat().st_mtime > older_than:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                continue

    def handoff(
        self,
        prepared: PreparedUpdate,
        *,
        launcher_pid: int,
        was_running: bool,
        progress=None,
    ) -> Path:
        """Start a detached updater from the staged package.

        The staged ``dbcheck.exe`` runs from the extracted package, so it does
        not lock the installation's ``_internal`` directory while the running
        launcher and its Python runtime are being replaced.
        """
        notify = progress or (lambda message: None)
        if not isinstance(prepared, PreparedUpdate):
            raise OfflineUpdateError("离线更新包尚未完成校验")
        helper = prepared.helper_executable
        if not helper.is_file():
            raise OfflineUpdateError("离线包缺少更新助手程序")
        if not self._launcher_locked():
            raise OfflineUpdateError("当前进程不是正在运行的 DBPulse 启动器")
        prepared.backup.mkdir(parents=True, exist_ok=False)
        transaction_path = prepared.backup / "handoff.json"
        transaction = {
            "schema": 1,
            "installation": str(self.installation),
            "data_root": str(self.data_root),
            "stage": str(prepared.stage),
            "package_root": str(prepared.root),
            "source": str(prepared.source),
            "mode": prepared.mode,
            "version": prepared.version,
            "current_version": prepared.current_version,
            "files": prepared.files,
            "bytes": prepared.bytes,
            "backup": str(prepared.backup),
            "launcher_pid": int(launcher_pid),
            "launcher": str(self.installation / "DBPulse.exe"),
            "was_running": bool(was_running),
        }
        _write_json(transaction_path, transaction)
        log = prepared.backup / "helper.log"
        env = os.environ.copy()
        env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
        notify("正在启动独立更新助手…")
        try:
            with log.open("ab") as output:
                process = subprocess.Popen(
                    [str(helper), "--offline-update-helper", str(transaction_path)],
                    cwd=str(helper.parent),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    creationflags=flags,
                    close_fds=True,
                )
        except OSError as error:
            raise OfflineUpdateError("无法启动更新助手：" + str(error)) from error
        time.sleep(0.8)
        if process.poll() is not None:
            raise OfflineUpdateError(
                "更新助手启动失败，请查看日志：" + str(log)
            )
        notify("更新助手已启动，启动器即将退出并自动完成替换")
        return transaction_path

    def _launcher_locked(self) -> bool:
        if not getattr(sys, "frozen", False):
            return False
        try:
            return (
                Path(sys.executable).resolve()
                == (self.installation / "DBPulse.exe").resolve()
            )
        except OSError:
            return False

    def _plan(
        self, prepared: PreparedUpdate, launcher_locked: bool | None = None
    ) -> tuple[list[_Target], bool]:
        root = prepared.root
        plan: list[_Target] = []
        if launcher_locked is None:
            launcher_locked = self._launcher_locked()
        if prepared.mode == "portable":
            component = root / "bin" / PLATFORM
            plan.append(_Target(Path("bin") / PLATFORM, component, True))
            runtime = root / "runtime" / PLATFORM
            if runtime.is_dir():
                plan.append(_Target(Path("runtime") / PLATFORM, runtime, True))
            for entry in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
                name = entry.name
                if name in {"bin", "runtime", "data", "backups", "dbpulse-portable.json"}:
                    continue
                if name.startswith(_STAGING_PREFIX):
                    continue
                plan.append(_Target(Path(name), entry, entry.is_dir()))
        else:
            for entry in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
                name = entry.name
                if name in PROTECTED_NAMES or name.startswith(_STAGING_PREFIX):
                    continue
                if name == "DBPulse.exe" and launcher_locked:
                    continue
                plan.append(_Target(Path(name), entry, entry.is_dir()))
        if not plan:
            raise OfflineUpdateError("离线包没有可安装的程序文件")
        for target in plan:
            if target.relative.is_absolute() or ".." in target.relative.parts:
                raise OfflineUpdateError("离线包目标路径非法")
            if target.relative.parts[0] in PROTECTED_NAMES:
                raise OfflineUpdateError("离线包试图覆盖运行时数据目录")
        return plan, launcher_locked

    @staticmethod
    def _move(path: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        last_error = None
        for attempt in range(60):
            try:
                path.rename(destination)
                return
            except FileNotFoundError:
                raise
            except OSError as error:
                last_error = error
                time.sleep(0.5)
        # Cross-volume staging is slower but still correct.
        try:
            if path.is_dir():
                shutil.copytree(path, destination, symlinks=False)
                shutil.rmtree(path)
            else:
                shutil.copy2(path, destination)
                path.unlink()
        except OSError as error:
            raise OfflineUpdateError(
                "无法移动程序文件：" + path.name + "（" + str(error) + "）"
            ) from last_error or error

    @staticmethod
    def _install(target: _Target, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise OfflineUpdateError("安装目标已存在：" + str(destination))
        if target.is_dir:
            shutil.copytree(target.source, destination, symlinks=False)
        else:
            shutil.copy2(target.source, destination)

    @staticmethod
    def _manifest(root: Path) -> dict[str, tuple[int, str]]:
        result: dict[str, tuple[int, str]] = {}
        for current, _, files in os.walk(root):
            for name in files:
                path = Path(current) / name
                result[path.relative_to(root).as_posix()] = (
                    path.stat().st_size,
                    _hash(path),
                )
        return result

    def _validate_installed(
        self, plan: list[_Target], prepared: PreparedUpdate, launcher_new: Path | None
    ) -> None:
        for target in plan:
            installed = self.root / target.relative
            if target.is_dir:
                if not installed.is_dir():
                    raise OfflineUpdateError("安装后缺少目录：" + target.relative.as_posix())
                if self._manifest(installed) != self._manifest(target.source):
                    raise OfflineUpdateError(
                        "安装后文件校验失败：" + target.relative.as_posix()
                    )
            else:
                if not installed.is_file():
                    raise OfflineUpdateError("安装后缺少文件：" + target.relative.as_posix())
                if _hash(installed) != _hash(target.source):
                    raise OfflineUpdateError(
                        "安装后文件校验失败：" + target.relative.as_posix()
                    )
        if launcher_new is not None:
            if not launcher_new.is_file():
                raise OfflineUpdateError("启动器更新文件未生成")
            if _hash(launcher_new) != _hash(prepared.launcher_source):
                raise OfflineUpdateError("启动器更新文件校验失败")
        if prepared.mode == "standalone":
            executable = self.root / "dbcheck.exe"
        else:
            executable = self.root / "bin" / PLATFORM / "dbcheck.exe"
        if not _pe_amd64(executable):
            raise OfflineUpdateError("安装后的 dbcheck.exe 校验失败")

    def _snapshot_rules(self, backup: Path) -> dict:
        rules = (
            self.root / "modules/pro/rules"
            if self.mode == "portable"
            else self.root / "_internal/modules/pro/rules"
        )
        snapshot = {
            "rules": rules,
            "custom": None,
            "overrides": None,
        }
        if (rules / "custom").is_dir():
            snapshot["custom"] = backup / "user_rules/custom"
            shutil.copytree(rules / "custom", snapshot["custom"], symlinks=False)
        if (rules / "overrides.yaml").is_file():
            snapshot["overrides"] = backup / "user_rules/overrides.yaml"
            snapshot["overrides"].parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rules / "overrides.yaml", snapshot["overrides"])
        return snapshot

    def _restore_rules(self, snapshot: dict) -> None:
        rules = snapshot["rules"]
        if snapshot["custom"] is not None:
            shutil.copytree(
                snapshot["custom"], rules / "custom", symlinks=False, dirs_exist_ok=True
            )
        if snapshot["overrides"] is not None:
            (rules).mkdir(parents=True, exist_ok=True)
            shutil.copy2(snapshot["overrides"], rules / "overrides.yaml")

    def _merge_marker(self, prepared: PreparedUpdate, previous: dict) -> None:
        marker_path = self.root / "dbpulse-portable.json"
        package = _read_json(prepared.root / "dbpulse-portable.json")
        package.update(
            version=prepared.version,
            updated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        platforms = dict(previous.get("platforms") or {})
        platforms.update(package.get("platforms") or {})
        required = previous.get("required_platforms") or package.get("required_platforms")
        if required:
            package["required_platforms"] = list(required)
            package["platforms"] = platforms
            package["complete"] = all(item in platforms for item in required)
        else:
            package["platforms"] = platforms
        _write_json(marker_path, package)

    @staticmethod
    def _wait_healthy(healthy, timeout: float, interval: float) -> bool:
        if healthy is None:
            return True
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            try:
                if healthy():
                    return True
            except Exception:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(max(0.01, float(interval)))

    def _rollback(
        self,
        applied: list[Path],
        moved: list[tuple[Path, Path]],
        launcher_new: Path | None,
        marker_path: Path,
        marker_before: dict | None,
    ) -> list[str]:
        failures: list[str] = []
        if launcher_new is not None:
            try:
                launcher_new.unlink(missing_ok=True)
            except OSError:
                failures.append(str(launcher_new))
        for target in reversed(applied):
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
            except OSError:
                failures.append(str(target))
        for target, previous in reversed(moved):
            try:
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                self._move(previous, target)
            except Exception:
                failures.append(str(target))
        try:
            if marker_before is None:
                marker_path.unlink(missing_ok=True)
            else:
                _write_json(marker_path, marker_before)
        except OSError:
            failures.append(str(marker_path))
        return failures

    def apply(
        self,
        prepared: PreparedUpdate,
        *,
        stop=None,
        start=None,
        healthy=None,
        was_running: bool = False,
        progress=None,
        health_timeout: float = 120.0,
        health_interval: float = 1.0,
    ) -> dict:
        notify = progress or (lambda message: None)
        if not isinstance(prepared, PreparedUpdate):
            raise OfflineUpdateError("离线更新包尚未完成校验")
        if not prepared.stage.is_dir() or not prepared.root.is_dir():
            raise OfflineUpdateError("离线更新包临时目录已失效，请重新选择")
        if was_running and (stop is None or start is None):
            raise OfflineUpdateError("运行中的服务需要提供停止和启动回调")
        if not was_running and healthy is not None and healthy():
            raise OfflineUpdateError(
                "检测到其他启动方式运行的服务，请先停止该服务后再离线更新"
            )
        plan, launcher_locked = self._plan(prepared)
        if launcher_locked:
            raise OfflineUpdateError(
                "启动器正在运行，需使用离线更新助手完成自更新"
            )
        prepared.backup.mkdir(parents=True, exist_ok=False)
        previous_root = prepared.backup / "previous"
        transaction_path = prepared.backup / "transaction.json"
        transaction = {
            "format": FORMAT,
            "version": 1,
            "state": "preparing",
            "source": str(prepared.source),
            "from_version": prepared.current_version,
            "to_version": prepared.version,
            "mode": prepared.mode,
            "started_at": datetime.now().astimezone().isoformat(),
            "targets": [item.relative.as_posix() for item in plan],
        }
        _write_json(transaction_path, transaction)

        marker_path = self.root / "dbpulse-portable.json"
        marker_before = (
            _read_json(marker_path)
            if prepared.mode == "portable" and marker_path.is_file()
            else None
        )
        moved: list[tuple[Path, Path]] = []
        applied: list[Path] = []
        launcher_new: Path | None = None
        stopped = False
        started = False
        try:
            if was_running and stop is not None:
                notify("正在停止后台服务…")
                stop()
                stopped = True
                if healthy is not None and not self._wait_healthy(
                    lambda: not healthy(), 20.0, 0.5
                ):
                    raise OfflineUpdateError("后台服务未能停止，已取消更新")
            transaction["state"] = "backing_up"
            _write_json(transaction_path, transaction)
            notify("正在备份当前程序文件…")
            snapshot = self._snapshot_rules(prepared.backup)
            for target in plan:
                current = self.root / target.relative
                if not current.exists():
                    continue
                saved = previous_root / target.relative
                self._move(current, saved)
                moved.append((current, saved))
            transaction["state"] = "installing"
            _write_json(transaction_path, transaction)
            notify("正在安装新版本程序文件…")
            for target in plan:
                destination = self.root / target.relative
                self._install(target, destination)
                applied.append(destination)
            self._validate_installed(plan, prepared, launcher_new)
            self._restore_rules(snapshot)
            if prepared.mode == "portable":
                self._merge_marker(prepared, marker_before or {})
            transaction["state"] = "installed"
            _write_json(transaction_path, transaction)
            if was_running and start is not None:
                notify("正在启动更新后的后台服务…")
                start()
                started = True
                if not self._wait_healthy(healthy, health_timeout, health_interval):
                    raise OfflineUpdateError("更新后的后台服务未能通过健康检查")
            transaction.update(
                state="complete",
                finished_at=datetime.now().astimezone().isoformat(),
                backup=str(prepared.backup),
            )
            _write_json(transaction_path, transaction)
            _prune_old_backups(self.backups)
            notify(
                "离线更新完成："
                + (prepared.current_version or "未知")
                + " → "
                + prepared.version
            )
            return {
                "version": prepared.version,
                "from_version": prepared.current_version,
                "mode": prepared.mode,
                "backup": str(prepared.backup),
                "launcher_update": str(launcher_new) if launcher_new else "",
                "restarted": bool(was_running and start is not None and started),
                "files": prepared.files,
            }
        except Exception as error:
            transaction["state"] = "rolling_back"
            transaction["error"] = str(error)
            _write_json(transaction_path, transaction)
            notify("更新失败，正在回滚到更新前版本…")
            if started and stop is not None:
                try:
                    stop()
                except Exception:
                    pass
                started = False
            failures = self._rollback(
                applied, moved, launcher_new, marker_path, marker_before
            )
            if failures:
                transaction["state"] = "recovery_required"
                transaction["rollback_failures"] = failures
                _write_json(transaction_path, transaction)
                raise OfflineUpdateRecoveryRequired(
                    "离线更新失败且自动回滚未完成。请保持服务停止，旧程序文件位于 "
                    + str(prepared.backup)
                ) from error
            transaction.update(
                state="rolled_back",
                finished_at=datetime.now().astimezone().isoformat(),
            )
            _write_json(transaction_path, transaction)
            if was_running and start is not None:
                try:
                    start()
                    self._wait_healthy(
                        healthy, min(45.0, float(health_timeout)), health_interval
                    )
                except Exception:
                    pass
            raise OfflineUpdateError(
                "离线更新失败，已回滚到更新前版本：" + str(error)
            ) from error
        finally:
            prepared.cleanup()


def _helper_log(path: Path, message: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as output:
            output.write(datetime.now().astimezone().isoformat() + " " + message + "\n")
    except OSError:
        pass


def _process_running(process_id: int) -> bool:
    try:
        import psutil

        process = psutil.Process(int(process_id))
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def _wait_process_exit(process_id: int, timeout: float = 180.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_running(process_id):
            return True
        time.sleep(0.5)
    return not _process_running(process_id)


def _health_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.load(response)
            return (
                response.status == 200
                and isinstance(data, dict)
                and data.get("ok") is True
                and data.get("status") == "healthy"
            )
    except Exception:
        return False


def _wait_health(url: str, expected: bool, timeout: float, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if _health_ok(url) is expected:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _stop_backend_from_metadata(data_root: Path, log: Path) -> None:
    record = _read_json(data_root / "desktop_process.json")
    try:
        process_id = int(record.get("pid") or 0)
        started = float(record.get("started") or 0)
    except (TypeError, ValueError):
        return
    if not process_id:
        return
    try:
        import psutil

        process = psutil.Process(process_id)
        if abs(process.create_time() - started) > 0.01:
            return
        subprocess.run(
            ["taskkill", "/PID", str(process_id), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=20,
        )
        try:
            process.wait(timeout=10)
        except psutil.Error:
            pass
        _helper_log(log, "stopped updated backend pid=" + str(process_id))
    except Exception as error:
        _helper_log(log, "failed to stop updated backend: " + str(error))


def _start_launcher(launcher: Path) -> subprocess.Popen:
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
        subprocess, "DETACHED_PROCESS", 0
    )
    environment = os.environ.copy()
    environment.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return subprocess.Popen(
        [str(launcher)],
        cwd=str(launcher.parent),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )


def run_update_helper(transaction_path, progress=None) -> dict:
    """Apply a staged package after the launcher exits.

    This function runs inside the newly extracted package, so the old
    installation's files are not held open by the updating process.
    """
    transaction_path = Path(transaction_path).resolve()
    data = _read_json(transaction_path)
    if data.get("schema") != 1:
        raise OfflineUpdateError("更新助手事务文件格式不正确")
    installation = Path(str(data.get("installation") or "")).resolve()
    data_root = Path(str(data.get("data_root") or "")).resolve()
    stage = Path(str(data.get("stage") or "")).resolve()
    package_root = Path(str(data.get("package_root") or "")).resolve()
    backup = Path(str(data.get("backup") or "")).resolve()
    launcher = Path(str(data.get("launcher") or "")).resolve()
    mode = str(data.get("mode") or "")
    if mode not in {"portable", "standalone"}:
        raise OfflineUpdateError("更新助手安装类型不正确")
    if not stage.is_dir() or not package_root.is_dir():
        raise OfflineUpdateError("更新助手临时目录已失效")
    if not package_root.is_relative_to(stage):
        raise OfflineUpdateError("更新助手包路径不正确")
    if not backup.is_relative_to(data_root / "backups" / "offline_update"):
        raise OfflineUpdateError("更新助手备份路径不正确")
    if launcher != installation / "DBPulse.exe" or not launcher.is_file():
        raise OfflineUpdateError("更新助手启动器路径不正确")
    log = backup / "helper.log"
    notify = progress or (lambda message: _helper_log(log, message))
    backup.mkdir(parents=True, exist_ok=True)
    notify("更新助手已启动，等待启动器退出…")
    process_id = int(data.get("launcher_pid") or 0)
    if process_id and not _wait_process_exit(process_id, 180.0):
        raise OfflineUpdateError("等待启动器退出超时，未修改程序文件")
    health_url = "http://127.0.0.1:5003/api/v1/health"
    if not _wait_health(health_url, False, 30.0, 0.5):
        _helper_log(log, "service is still healthy; update cancelled")
        _start_launcher(launcher)
        raise OfflineUpdateError("后台服务仍在运行，已取消离线更新")

    updater = OfflineUpdate(installation, data_root=data_root)
    if updater.mode != mode:
        raise OfflineUpdateError("更新助手安装类型与当前目录不匹配")
    prepared = PreparedUpdate(
        source=Path(str(data.get("source") or "")),
        stage=stage,
        root=package_root,
        mode=mode,
        version=str(data.get("version") or ""),
        current_version=str(data.get("current_version") or ""),
        files=int(data.get("files") or 0),
        bytes=int(data.get("bytes") or 0),
        backup=backup,
        details={},
    )
    plan, _ = updater._plan(prepared, launcher_locked=False)
    transaction_file = backup / "transaction.json"
    transaction = {
        "format": FORMAT,
        "version": 1,
        "state": "preparing",
        "source": str(prepared.source),
        "from_version": prepared.current_version,
        "to_version": prepared.version,
        "mode": prepared.mode,
        "started_at": datetime.now().astimezone().isoformat(),
        "targets": [item.relative.as_posix() for item in plan],
        "helper": True,
    }
    _write_json(transaction_file, transaction)
    marker_path = updater.root / "dbpulse-portable.json"
    marker_before = (
        _read_json(marker_path)
        if prepared.mode == "portable" and marker_path.is_file()
        else None
    )
    previous_root = backup / "previous"
    moved: list[tuple[Path, Path]] = []
    applied: list[Path] = []
    launcher_process = None
    try:
        transaction["state"] = "backing_up"
        _write_json(transaction_file, transaction)
        notify("正在备份当前程序文件…")
        snapshot = updater._snapshot_rules(backup)
        for target in plan:
            current = updater.root / target.relative
            if not current.exists():
                continue
            saved = previous_root / target.relative
            updater._move(current, saved)
            moved.append((current, saved))
        transaction["state"] = "installing"
        _write_json(transaction_file, transaction)
        notify("正在安装新版本程序文件…")
        for target in plan:
            destination = updater.root / target.relative
            updater._install(target, destination)
            applied.append(destination)
        updater._validate_installed(plan, prepared, None)
        updater._restore_rules(snapshot)
        if prepared.mode == "portable":
            updater._merge_marker(prepared, marker_before or {})
        transaction["state"] = "installed"
        _write_json(transaction_file, transaction)
        notify("程序文件已替换，正在启动新版本…")
        launcher_process = _start_launcher(launcher)
        if not _wait_health(health_url, True, 180.0, 1.0):
            raise OfflineUpdateError("新版本后台服务未能通过健康检查")
        transaction.update(
            state="complete",
            finished_at=datetime.now().astimezone().isoformat(),
            backup=str(backup),
        )
        _write_json(transaction_file, transaction)
        _prune_old_backups(updater.backups)
        notify("离线更新完成：" + prepared.current_version + " → " + prepared.version)
        return {
            "version": prepared.version,
            "from_version": prepared.current_version,
            "backup": str(backup),
        }
    except Exception as error:
        transaction["state"] = "rolling_back"
        transaction["error"] = str(error)
        _write_json(transaction_file, transaction)
        notify("更新失败，正在回滚：" + str(error))
        if launcher_process is not None and launcher_process.poll() is None:
            try:
                launcher_process.kill()
                launcher_process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass
        _stop_backend_from_metadata(data_root, log)
        failures = updater._rollback(
            applied, moved, None, marker_path, marker_before
        )
        if failures:
            transaction["state"] = "recovery_required"
            transaction["rollback_failures"] = failures
            _write_json(transaction_file, transaction)
            raise OfflineUpdateRecoveryRequired(
                "离线更新失败且自动回滚未完成，旧程序文件位于 " + str(backup)
            ) from error
        transaction.update(
            state="rolled_back",
            finished_at=datetime.now().astimezone().isoformat(),
        )
        _write_json(transaction_file, transaction)
        notify("已回滚到更新前版本，正在重新启动旧版本…")
        try:
            _start_launcher(launcher)
            _wait_health(health_url, True, 60.0, 1.0)
        except Exception:
            pass
        raise OfflineUpdateError(
            "离线更新失败，已回滚到更新前版本：" + str(error)
        ) from error


def helper_main(arguments=None) -> int:
    """Frozen entry point for ``dbcheck.exe --offline-update-helper``."""
    arguments = list(sys.argv if arguments is None else arguments)
    flag = "--offline-update-helper"
    if flag not in arguments:
        return 2
    index = arguments.index(flag)
    if index + 1 >= len(arguments):
        return 2
    transaction = Path(arguments[index + 1])
    try:
        run_update_helper(transaction)
        return 0
    except Exception as error:
        data = _read_json(transaction)
        log = (
            Path(str(data["backup"])) / "helper.log"
            if data.get("backup")
            else transaction.parent / "helper.log"
        )
        _helper_log(log, "helper failed: " + str(error))
        state = str(data.get("state") or "")
        launcher = Path(str(data.get("launcher") or ""))
        if (
            state in {"", "preparing"}
            and launcher.is_file()
            and not _health_ok("http://127.0.0.1:5003/api/v1/health")
        ):
            try:
                launcher_pid = int(data.get("launcher_pid") or 0)
                if launcher_pid and not _wait_process_exit(launcher_pid, 30.0):
                    _helper_log(
                        log,
                        "previous launcher did not exit; not starting a duplicate",
                    )
                else:
                    _helper_log(
                        log, "restarting previous launcher after helper failure"
                    )
                    _start_launcher(launcher)
            except Exception as restart_error:
                _helper_log(
                    log,
                    "failed to restart previous launcher: " + str(restart_error),
                )
        return 1
