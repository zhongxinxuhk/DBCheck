# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""Local desktop data backups with SQLite snapshots and reversible restoration."""


import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from contextlib import closing, ExitStack
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

FORMAT = "dbpulse-desktop-backup"
TRANSIENT = {"desktop_process.json", "inspection_leases.db"}
SCOPE_NAMES = ("data", "custom_rules", "rule_overrides")


class RecoveryRequired(RuntimeError):
    """A restoration could not automatically return to the previous data."""


def _hash(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _linked(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


class LocalBackup:
    def __init__(self, installation, resources=None):
        self.installation = Path(installation).resolve()
        self.data = (self.installation / "data").resolve()
        if resources is None:
            resources = (
                self.installation / "_internal"
                if (self.installation / "_internal/web_templates").is_dir()
                else self.installation
            )
        rules = Path(resources) / "modules/pro/rules"
        self.scopes = {
            "data": self.data,
            "custom_rules": rules / "custom",
            "rule_overrides": rules / "overrides.yaml",
        }

    def _files(self, destination):
        for scope, base in self.scopes.items():
            if not base.exists():
                continue
            candidates = [base] if base.is_file() else sorted(base.rglob("*"))
            for path in candidates:
                if _linked(path):
                    raise ValueError(
                        "备份遇到文件链接，请先转换为实际文件：" + path.name
                    )
                if not path.is_file() or path.resolve() == destination:
                    continue
                relative = (
                    path.relative_to(base) if base.is_dir() else Path("overrides.yaml")
                )
                if scope == "data":
                    if relative.parts[0] == "backups" or relative.parts[:2] == (
                        "objstore",
                        "staging",
                    ):
                        continue
                    if path.name in TRANSIENT or path.name.endswith(
                        ("-wal", "-shm", ".lock", ".tmp")
                    ):
                        continue
                yield scope + "/" + relative.as_posix(), path

    def export(self, destination, progress=None):
        notify = progress or (lambda message: None)
        destination = Path(destination).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        entries = list(self._files(destination))
        if not entries:
            raise ValueError("当前尚无可备份的数据")
        temporary = destination.with_name(
            "." + destination.name + "." + uuid.uuid4().hex + ".tmp"
        )
        manifest = {
            "format": FORMAT,
            "version": 1,
            "created_at": datetime.now().astimezone().isoformat(),
            "scopes": list(SCOPE_NAMES),
            "files": [],
        }
        notify("正在导出数据备份…")
        try:
            with tempfile.TemporaryDirectory(
                prefix="dbpulse-backup-"
            ) as directory, zipfile.ZipFile(
                temporary, "w", zipfile.ZIP_DEFLATED, allowZip64=True
            ) as archive:
                for index, (name, path) in enumerate(entries, 1):
                    staged = Path(directory) / "file"
                    notify(f"备份 {index}/{len(entries)} · {name}")
                    with path.open("rb") as source:
                        header = source.read(16)
                        source.seek(0)
                        if header == b"SQLite format 3\x00":
                            with closing(
                                sqlite3.connect(
                                    path.resolve().as_uri() + "?mode=ro",
                                    uri=True,
                                    timeout=15,
                                )
                            ) as reader, closing(sqlite3.connect(staged)) as writer:
                                reader.backup(writer, pages=256, sleep=0.05)
                        else:
                            remaining = os.fstat(source.fileno()).st_size
                            with staged.open("wb") as target:
                                while remaining:
                                    block = source.read(min(1024 * 1024, remaining))
                                    if not block:
                                        raise ValueError(
                                            "备份期间文件被截断，请重试：" + name
                                        )
                                    target.write(block)
                                    remaining -= len(block)
                    manifest["files"].append(
                        {
                            "path": name,
                            "size": staged.stat().st_size,
                            "sha256": _hash(staged),
                        }
                    )
                    archive.write(staged, name)
                    staged.unlink()
                archive.writestr(
                    "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
                )
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        notify("备份导出完成 · " + str(destination))
        return {
            "path": str(destination),
            "files": len(entries),
            "created_at": manifest["created_at"],
        }

    @staticmethod
    def _manifest(archive):
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(name.casefold() for name in names)):
            raise ValueError("备份包含重复或大小写冲突的文件名")
        if (
            names.count("manifest.json") != 1
            or archive.getinfo("manifest.json").file_size > 128 * 1024 * 1024
        ):
            raise ValueError("备份缺少有效清单")
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("format") != FORMAT
            or manifest.get("version") != 1
        ):
            raise ValueError("请选择由 DBPulse 状态窗口导出的备份 ZIP")
        if manifest.get("scopes") != list(SCOPE_NAMES) or not isinstance(
            manifest.get("files"), list
        ):
            raise ValueError("备份清单的数据范围不正确")
        seen = set()
        for item in manifest["files"]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ValueError("备份文件清单不正确")
            name = item["path"]
            path = PurePosixPath(name)
            if (
                any(ord(char) < 32 for char in name)
                or any(
                    part.endswith((".", " ")) or PureWindowsPath(part).is_reserved()
                    for part in path.parts
                )
                or "\\" in name
                or ":" in name
                or path.is_absolute()
                or ".." in path.parts
                or path.as_posix() != name
                or len(path.parts) < 2
                or path.parts[0] not in SCOPE_NAMES
            ):
                raise ValueError("备份含有非法文件路径")
            if path.parts[0] == "rule_overrides" and path.parts != (
                "rule_overrides",
                "overrides.yaml",
            ):
                raise ValueError("备份的规则覆盖路径不正确")
            if (
                name.casefold() in seen
                or not isinstance(item.get("size"), int)
                or isinstance(item["size"], bool)
                or item["size"] < 0
                or not re.fullmatch("[0-9a-f]{64}", str(item.get("sha256")))
            ):
                raise ValueError("备份文件校验信息不正确")
            seen.add(name.casefold())
            info = archive.getinfo(name)
            if (
                info.is_dir()
                or info.file_size != item["size"]
                or stat.S_ISLNK(info.external_attr >> 16)
            ):
                raise ValueError("备份中的文件类型或长度不正确")
            if path.name in TRANSIENT or path.name.endswith(
                ("-wal", "-shm", ".lock", ".tmp")
            ):
                raise ValueError("备份包含运行中的临时状态文件")
        expected = {item["path"] for item in manifest["files"]} | {"manifest.json"}
        if set(names) != expected or not any(
            item["path"].startswith("data/") for item in manifest["files"]
        ):
            raise ValueError("备份文件与清单不一致或缺少系统数据")
        return manifest

    @staticmethod
    def _validate_data(stage):
        key = stage / "data/.db_key"
        if key.is_file():
            try:
                if (
                    len(
                        base64.b64decode(
                            key.read_bytes().strip(), altchars=b"-_", validate=True
                        )
                    )
                    != 32
                ):
                    raise ValueError()
            except Exception as error:
                raise ValueError("备份中的数据源加密密钥不正确") from error
        for path in (stage / "data").rglob("*"):
            if not path.is_file():
                continue
            with path.open("rb") as file:
                sqlite = file.read(16) == b"SQLite format 3\x00"
            if sqlite or path.suffix in {".db", ".sqlite", ".sqlite3"}:
                with closing(
                    sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                ) as database:
                    if database.execute("PRAGMA quick_check").fetchone() != ("ok",):
                        raise ValueError("备份数据库校验失败：" + path.name)
                    tables = {
                        row[0]
                        for row in database.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    encrypted_tables = tables & {
                        "operations_inventory",
                        "operations_inventory_history",
                        "operations_imports",
                        "operations_findings",
                        "operations_groups",
                        "service_records",
                    }
                    if encrypted_tables and not key.is_file():
                        raise ValueError("备份缺少台账和服务记录的配套加密密钥")
                    if key.is_file():
                        from cryptography.fernet import Fernet, InvalidToken

                        cipher = Fernet(key.read_bytes().strip())
                        try:
                            for table in sorted(encrypted_tables):
                                for (value,) in database.execute(
                                    'SELECT payload FROM "' + table + '"'
                                ):
                                    if value:
                                        cipher.decrypt(
                                            value.encode()
                                            if isinstance(value, str)
                                            else value
                                        )
                            if "instances" in tables:
                                columns = {
                                    row[1]
                                    for row in database.execute(
                                        "PRAGMA table_info(instances)"
                                    )
                                }
                                for field in sorted(
                                    columns
                                    & {"password", "ssh_password", "ssh_key_password"}
                                ):
                                    for (value,) in database.execute(
                                        'SELECT "' + field + '" FROM instances'
                                    ):
                                        if isinstance(value, str) and value.startswith(
                                            ("gAAAA", "Z0FBQUF")
                                        ):
                                            token = (
                                                base64.b64decode(value)
                                                if value.startswith("Z0FBQUF")
                                                else value.encode()
                                            )
                                            cipher.decrypt(token)
                        except (InvalidToken, ValueError, TypeError) as error:
                            raise ValueError("备份中的密码或台账密钥不匹配") from error
                    if path.name == "instances.db" and not key.is_file():
                        tables = {
                            row[0]
                            for row in database.execute(
                                "SELECT name FROM sqlite_master WHERE type='table'"
                            )
                        }
                        if (
                            "instances" in tables
                            and database.execute(
                                "SELECT COUNT(*) FROM instances"
                            ).fetchone()[0]
                        ):
                            raise ValueError("备份缺少数据库密码的配套加密密钥")

    def _extract_verified(self, source, stage, notify):
        with zipfile.ZipFile(source) as archive:
            manifest = self._manifest(archive)
            required = sum(item["size"] for item in manifest["files"])
            if required > shutil.disk_usage(stage).free:
                raise ValueError("磁盘可用空间不足，无法解压备份")
            for index, item in enumerate(manifest["files"], 1):
                notify(f'校验 {index}/{len(manifest["files"])} · {item["path"]}')
                path = stage / item["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item["path"]) as reader, path.open("wb") as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                if path.stat().st_size != item["size"] or _hash(path) != item["sha256"]:
                    raise ValueError("备份内容校验失败：" + item["path"])
        self._validate_data(stage)
        return manifest

    def verify(self, source, progress=None):
        notify = progress or (lambda message: None)
        notify("正在验证备份完整性、数据库和密码密钥…")
        with tempfile.TemporaryDirectory(prefix="dbpulse-backup-check-") as directory:
            manifest = self._extract_verified(source, Path(directory).resolve(), notify)
        notify("备份验证通过，可用于导入恢复")
        return {
            "files": len(manifest["files"]),
            "created_at": manifest["created_at"],
            "verified": True,
        }

    def restore(self, source, progress=None, before_apply=None):
        notify = progress or (lambda message: None)
        self.data.parent.mkdir(parents=True, exist_ok=True)
        recovery = (
            self.data.parent
            / "backups"
            / (
                "before-import-"
                + datetime.now().strftime("%Y%m%d-%H%M%S")
                + "-"
                + uuid.uuid4().hex[:8]
            )
        )
        moved = []
        candidates = []
        applied = []
        notify("正在检查备份文件…")
        with ExitStack() as temporary:
            directory = temporary.enter_context(
                tempfile.TemporaryDirectory(
                    prefix=".dbpulse-restore-",
                    dir=self.data.parent,
                    ignore_cleanup_errors=True,
                )
            )
            stage = Path(directory)
            manifest = self._extract_verified(source, stage, notify)
            # Resource replacements are prepared on their own filesystem.
            for scope in SCOPE_NAMES:
                target = self.scopes[scope]
                content = stage / scope
                if scope == "rule_overrides":
                    content = content / "overrides.yaml"
                if scope == "data":
                    candidate = content
                elif content.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    directory = temporary.enter_context(
                        tempfile.TemporaryDirectory(
                            prefix=".dbpulse-rule-restore-",
                            dir=target.parent,
                            ignore_cleanup_errors=True,
                        )
                    )
                    candidate = Path(directory) / target.name
                    if content.is_dir():
                        shutil.copytree(content, candidate)
                    else:
                        shutil.copy2(content, candidate)
                else:
                    candidate = None
                candidates.append((scope, target, candidate))
            try:
                if before_apply:
                    before_apply()
                recovery.mkdir(parents=True)
                notify("正在恢复数据，原数据将保留在 " + str(recovery))
                for scope, target, candidate in candidates:
                    if target.exists():
                        old_parent = (
                            recovery
                            if scope == "data"
                            else target.parent / (".before-import-" + recovery.name)
                        )
                        old_parent.mkdir(parents=True, exist_ok=True)
                        old = old_parent / target.name
                        target.rename(old)
                        moved.append((target, old))
                    if candidate is not None:
                        candidate.rename(target)
                        applied.append(target)
                receipt = {
                    "source": str(Path(source)),
                    "restored_at": datetime.now().astimezone().isoformat(),
                    "previous": [
                        {"target": str(target), "saved": str(old)}
                        for target, old in moved
                    ],
                }
                (recovery / "recovery.json").write_text(
                    json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception as error:
                failures = []
                for target in reversed(applied):
                    try:
                        if target.is_dir():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                    except OSError:
                        failures.append(str(target))
                for target, old in reversed(moved):
                    try:
                        old.rename(target)
                    except OSError:
                        failures.append(str(old))
                if failures:
                    raise RecoveryRequired(
                        "导入中断，自动回退未完成。请保持服务停止，原数据位置见 "
                        + str(recovery)
                    ) from error
                raise
        notify("备份导入完成，原数据副本位于 " + str(recovery))
        return {"files": len(manifest["files"]), "previous_data": str(recovery)}
