# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Desktop backup schedules, verification history and bounded crash recovery."""


import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .local_backup import LocalBackup

DEFAULTS = {
    "automatic_backup": False,
    "backup_frequency": "daily",
    "backup_time": "02:00",
    "retention": 7,
    "restart_enabled": False,
    "restart_limit": 3,
    "restart_delay": 10,
}


class Maintenance:
    def __init__(self, installation):
        self.root = Path(installation)
        self.path = self.root / "data/desktop_settings.json"
        self.lock = threading.RLock()
        self.last_attempt = 0
        self.state = {
            "last_success": "",
            "last_error": "",
            "last_verified": "",
            "history": [],
        }
        self.settings = dict(DEFAULTS)
        self.load()

    def load(self):
        with self.lock:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                self.settings = self.validate(value.get("settings", {}))
                self.state.update(value.get("state", {}))
            except FileNotFoundError:
                pass

    @staticmethod
    def validate(value):
        if not isinstance(value, dict) or set(value) - DEFAULTS.keys():
            raise ValueError("维护设置字段无效")
        settings = {**DEFAULTS, **value}
        for key in ("automatic_backup", "restart_enabled"):
            if not isinstance(settings[key], bool):
                raise ValueError("开关格式无效")
        for key, lower, upper in (
            ("retention", 1, 365),
            ("restart_limit", 1, 10),
            ("restart_delay", 5, 300),
        ):
            if type(settings[key]) is not int or not lower <= settings[key] <= upper:
                raise ValueError(f"{key}须为{lower}到{upper}的整数")
        if settings["backup_frequency"] not in {"daily", "weekly"} or not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", settings["backup_time"]
        ):
            raise ValueError("备份周期或时间无效")
        return settings

    def persist(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(
                self.path.name + "." + uuid.uuid4().hex + ".tmp"
            )
            try:
                temporary.write_text(
                    json.dumps(
                        {"settings": self.settings, "state": self.state},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def save(self, settings):
        with self.lock:
            self.settings = self.validate(settings)
            self.persist()

    def due(self, now=None):
        now = now or datetime.now().astimezone()
        if (
            not self.settings["automatic_backup"]
            or time.time() - self.last_attempt < 900
        ):
            return False
        hour, minute = map(int, self.settings["backup_time"].split(":"))
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < scheduled:
            scheduled -= timedelta(days=1)
        if self.settings["backup_frequency"] == "weekly":
            scheduled -= timedelta(days=scheduled.weekday())
        last = (
            datetime.fromisoformat(self.state["last_success"])
            if self.state["last_success"]
            else None
        )
        return last is None or last < scheduled

    def automatic_backup(self, progress=None):
        with self.lock:
            self.last_attempt = time.time()
            folder = self.root / "backups/automatic"
            folder.mkdir(parents=True, exist_ok=True)
            destination = folder / (
                "DBPulse-auto-"
                + datetime.now().strftime("%Y%m%d-%H%M%S")
                + "-"
                + uuid.uuid4().hex[:8]
                + ".zip"
            )
            backup = LocalBackup(self.root)
            try:
                backup.export(destination, progress)
                backup.verify(destination, progress)
                now = datetime.now().astimezone().isoformat()
                self.state.update(last_success=now, last_verified=now, last_error="")
                self.state["history"] = (
                    [{"at": now, "path": str(destination), "verified": True}]
                    + self.state["history"]
                )[:30]
                self.persist()
                candidates = sorted(
                    (
                        path
                        for path in folder.glob("DBPulse-auto-*.zip")
                        if re.fullmatch(
                            r"DBPulse-auto-\d{8}-\d{6}-[0-9a-f]{8}\.zip", path.name
                        )
                    ),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
                for old in candidates[self.settings["retention"] :]:
                    old.unlink()
                return str(destination)
            except Exception as error:
                destination.unlink(missing_ok=True)
                self.state["last_error"] = str(error)
                self.persist()
                raise

    def verification(self, source, progress=None):
        result = LocalBackup(self.root).verify(source, progress)
        with self.lock:
            now = datetime.now().astimezone().isoformat()
            self.state["last_verified"] = now
            self.state["history"] = (
                [{"at": now, "path": str(source), "verified": True}]
                + self.state["history"]
            )[:30]
            self.persist()
        return result


class CrashRecovery:
    def __init__(self):
        self.expected = False
        self.was_running = False
        self.attempts = 0
        self.healthy_since = None
        self.next_attempt = 0

    def intentional(self, running):
        self.expected = running
        self.was_running = running
        self.attempts = 0
        self.healthy_since = None
        self.next_attempt = 0

    def observe(self, owned, healthy, settings, now=None):
        now = time.monotonic() if now is None else now
        if healthy and owned:
            self.next_attempt = 0
            self.was_running = True
            self.healthy_since = self.healthy_since or now
            if now - self.healthy_since >= 300:
                self.attempts = 0
            return ""
        if owned:
            self.next_attempt = 0
            return ""
        self.healthy_since = None
        if not self.expected or not self.was_running:
            return ""
        if not settings["restart_enabled"]:
            self.expected = False
            return "后台服务异常停止，请查看运行日志后重新启动"
        if self.attempts >= settings["restart_limit"]:
            self.expected = False
            return "后台服务连续恢复失败，已达到自动重启次数限制"
        if not self.next_attempt:
            self.next_attempt = now + settings["restart_delay"]
            return "后台服务异常停止，准备自动重启"
        if now < self.next_attempt:
            return ""
        self.attempts += 1
        self.next_attempt = now + settings["restart_delay"]
        return "restart"
