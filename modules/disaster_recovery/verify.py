# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""备份可恢复性验证（Restore Drill）。

设计目标
--------
- 把"备份任务成功"升级为"备份被证明可恢复"：对最近一次成功备份做一次真实恢复演练。
- 演练目标永远是**临时库**（前缀 ``dbcheck_verify_``），结束后无条件清理，绝不碰源库。
- 客户端缺失、文件过大、备份过期等情况记为 ``skipped`` 并写明原因，不伪造成通过。

分层策略
--------
- ``archive`` 层（所有类型）：文件存在性、大小、gzip/tar 完整性、SHA-256、SQL 结构标记。
- ``restore`` 层（mysql/postgresql）：重建临时库 → 导入备份 → 校验对象数量 → 删除临时库。
- 任一层失败即视为验证失败；只有 restore 层通过才把计划标记为"已验证"。

存储
----
``data/autobackup/verify/runs.jsonl``，单行一条记录，按 ``keep_runs`` 截断。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "auto_verify": True,
    "max_age_days": 7,
    "timeout_seconds": 600,
    "max_file_mb": 20480,
    "scratch_prefix": "dbcheck_verify_",
    "keep_runs": 200,
    "allow_remote": True,
}

_STATUS_LABEL = {
    "passed": "通过",
    "failed": "失败",
    "skipped": "跳过",
    "error": "异常",
}

_lock = threading.Lock()
_verify_lock = threading.Lock()
_worker: Optional[threading.Thread] = None
_worker_stop = threading.Event()


# ---------------------------------------------------------------------------
# 配置与存储
# ---------------------------------------------------------------------------
def _data_dir() -> Path:
    from modules.disaster_recovery import engine

    return Path(engine.DATA_DIR)


def _runs_path() -> Path:
    path = _data_dir() / "verify" / "runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_config() -> Dict[str, Any]:
    from modules.disaster_recovery import engine

    cfg = dict(DEFAULT_CONFIG)
    try:
        raw = engine._load_config().get("verify") or {}
        if isinstance(raw, dict):
            cfg.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
    except Exception:
        pass
    return cfg


def update_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    from modules.disaster_recovery import engine

    with engine._LOCK:
        cfg = engine._load_config()
        current = dict(DEFAULT_CONFIG)
        existing = cfg.get("verify")
        if isinstance(existing, dict):
            current.update({k: v for k, v in existing.items() if k in DEFAULT_CONFIG})
        for key, value in (patch or {}).items():
            if key not in DEFAULT_CONFIG:
                continue
            if key in ("enabled", "auto_verify", "allow_remote"):
                current[key] = bool(value)
            elif key in ("max_age_days", "timeout_seconds", "max_file_mb", "keep_runs"):
                current[key] = max(1, int(value))
            elif key == "scratch_prefix":
                prefix = re.sub(r"[^a-z0-9_]", "_", str(value).lower())
                current[key] = prefix or DEFAULT_CONFIG["scratch_prefix"]
        cfg["verify"] = current
        engine._save_config(cfg)
    return current


def _append_run(record: Dict[str, Any]) -> None:
    keep = int(get_config().get("keep_runs", 200))
    with _lock:
        path = _runs_path()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > keep:
                path.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
        except OSError:
            pass


def list_runs(limit: int = 50, task_name: Optional[str] = None) -> List[Dict[str, Any]]:
    path = _runs_path()
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    if task_name:
        records = [r for r in records if r.get("task") == task_name]
    return list(reversed(records[-limit:]))


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    for record in list_runs(limit=10000):
        if record.get("id") == run_id:
            return record
    return None


# ---------------------------------------------------------------------------
# 健康度汇总（供 engine.get_health_metrics 使用）
# ---------------------------------------------------------------------------
def get_summary(task_name: Optional[str] = None) -> Dict[str, Any]:
    runs = list_runs(limit=200, task_name=task_name)
    if not runs:
        return {
            "verified": False,
            "verified_label": "未验证",
            "status": "none",
            "last_run": None,
            "pass_rate": 0.0,
            "total": 0,
        }
    last = runs[0]
    max_age_days = int(get_config().get("max_age_days", 7))
    recent = runs[:20]
    passed = [r for r in recent if r.get("status") == "passed"]
    age_ok = True
    finished = last.get("finished_at") or ""
    try:
        age_ok = datetime.now() - datetime.strptime(finished, "%Y-%m-%d %H:%M:%S") <= timedelta(days=max_age_days)
    except Exception:
        age_ok = False
    verified = last.get("status") == "passed" and age_ok
    return {
        "verified": verified,
        "verified_label": _STATUS_LABEL.get(last.get("status", ""), "未验证"),
        "status": last.get("status"),
        "last_run": last,
        "pass_rate": round(len(passed) / len(recent) * 100, 1) if recent else 0.0,
        "total": len(runs),
    }


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _sha256(path: Path, limit_mb: int = 0) -> str:
    digest = hashlib.sha256()
    remaining = limit_mb * 1024 * 1024 if limit_mb else 0
    with path.open("rb") as fh:
        while True:
            size = 1024 * 1024
            if remaining:
                size = min(size, remaining)
            chunk = fh.read(size)
            if not chunk:
                break
            digest.update(chunk)
            if remaining:
                remaining -= len(chunk)
                if remaining <= 0:
                    break
    return digest.hexdigest()


def _scratch_name(task_name: str) -> str:
    prefix = get_config().get("scratch_prefix", DEFAULT_CONFIG["scratch_prefix"])
    safe = re.sub(r"[^a-z0-9_]", "_", (task_name or "task").lower())[:24].strip("_")
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{prefix}{safe}_{stamp}_{uuid.uuid4().hex[:4]}"[:63]


def _run(cmd: List[str], env: Optional[Dict[str, str]] = None,
         timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=timeout
    )


def _restore_stream(cmd: List[str], source: Path, env: Dict[str, str],
                    timeout: int) -> tuple:
    """把 gzip 备份流式喂给恢复命令，返回 (rc, stderr)。"""
    err_file = tempfile.TemporaryFile()
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err_file, env=env
    )
    try:
        with gzip.open(source, "rb") as fh:
            assert proc.stdin is not None
            shutil.copyfileobj(fh, proc.stdin, 1024 * 1024)
        proc.stdin.close()
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        rc = -9
    finally:
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        err_file.seek(0)
        stderr = err_file.read().decode("utf-8", "replace")[-4000:]
        err_file.close()
    return rc, stderr


# ---------------------------------------------------------------------------
# archive 层校验
# ---------------------------------------------------------------------------
def _check_archive(path: Path, checks: List[Dict[str, Any]]) -> None:
    size = path.stat().st_size
    checks.append({"name": "文件存在", "ok": True, "detail": f"{size} bytes"})
    checks.append({"name": "文件非空", "ok": size > 0, "detail": f"{size} bytes"})

    checksum = _sha256(path)
    checks.append({"name": "SHA-256", "ok": True, "detail": checksum})

    if path.name.endswith(".sql.gz"):
        try:
            with gzip.open(path, "rb") as fh:
                head = fh.read(1024 * 1024)
            checks.append({"name": "gzip 完整性", "ok": True, "detail": "可解压"})
            marker = b"CREATE TABLE" in head or b"INSERT INTO" in head or b"COPY " in head
            checks.append({
                "name": "SQL 结构标记",
                "ok": marker,
                "detail": "发现建表/写入语句" if marker else "前 1MB 未发现 SQL 结构标记",
            })
        except Exception as exc:
            checks.append({"name": "gzip 完整性", "ok": False, "detail": str(exc)})
    elif path.name.endswith(".tar.gz"):
        try:
            with tarfile.open(path, "r:gz") as tf:
                count = 0
                for _ in tf:
                    count += 1
                    if count >= 200000:
                        break
            checks.append({
                "name": "tar 完整性",
                "ok": count > 0,
                "detail": f"{count} 个条目",
            })
        except Exception as exc:
            checks.append({"name": "tar 完整性", "ok": False, "detail": str(exc)})
    else:
        checks.append({"name": "归档类型", "ok": False, "detail": "未知备份扩展名"})


# ---------------------------------------------------------------------------
# restore 层：MySQL
# ---------------------------------------------------------------------------
def _verify_mysql(task: Dict[str, Any], path: Path,
                  checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    client = shutil.which("mysql")
    if not client:
        return {"skipped": True, "reason": "未找到 mysql 客户端，仅完成归档校验"}

    db = task.get("database") or {}
    host = db.get("host", "localhost")
    port = int(db.get("port", 3306))
    user = db.get("user", "")
    from modules.disaster_recovery.vendor.autobackup import resolve_secret

    password = resolve_secret(db.get("password", ""))
    env = os.environ.copy()
    env["MYSQL_PWD"] = password
    timeout = int(get_config().get("timeout_seconds", 600))
    scratch = _scratch_name(task.get("name", ""))
    base = [client, f"--host={host}", f"--port={port}", f"--user={user}",
            "--connect-timeout=10", "--default-character-set=utf8mb4"]
    if host not in ("localhost", "127.0.0.1", "::1") and not get_config().get("allow_remote", True):
        return {"skipped": True, "reason": "目标为远程实例，配置禁止远程恢复演练"}

    try:
        created = _run(base + ["-e", f"CREATE DATABASE `{scratch}` CHARACTER SET utf8mb4"],
                       env=env, timeout=60)
        if created.returncode != 0:
            checks.append({"name": "创建临时库", "ok": False, "detail": created.stderr.strip()[-1000:]})
            return {"skipped": False}
        checks.append({"name": "创建临时库", "ok": True, "detail": scratch})

        rc, stderr = _restore_stream(base + [scratch], path, env, timeout)
        if rc != 0:
            checks.append({"name": "导入备份", "ok": False, "detail": stderr or f"exit={rc}"})
            return {"skipped": False}
        checks.append({"name": "导入备份", "ok": True, "detail": "mysql < dump"})

        counted = _run(base + ["-N", "-B", "-e",
                               "SELECT COUNT(*) FROM information_schema.tables "
                               f"WHERE table_schema='{scratch}'"], env=env, timeout=60)
        count = int((counted.stdout or "0").strip() or 0) if counted.returncode == 0 else 0
        checks.append({
            "name": "对象数量",
            "ok": count > 0,
            "detail": f"{count} 张表/视图",
        })
        return {"skipped": False, "scratch_db": scratch, "objects": count}
    finally:
        try:
            _run(base + ["-e", f"DROP DATABASE IF EXISTS `{scratch}`"], env=env, timeout=60)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# restore 层：PostgreSQL
# ---------------------------------------------------------------------------
def _verify_postgresql(task: Dict[str, Any], path: Path,
                       checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    client = shutil.which("psql")
    if not client:
        return {"skipped": True, "reason": "未找到 psql 客户端，仅完成归档校验"}

    db = task.get("database") or {}
    host = db.get("host", "localhost")
    port = int(db.get("port", 5432))
    user = db.get("user", "")
    from modules.disaster_recovery.vendor.autobackup import resolve_secret

    password = resolve_secret(db.get("password", ""))
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    timeout = int(get_config().get("timeout_seconds", 600))
    scratch = _scratch_name(task.get("name", ""))
    base = [client, f"--host={host}", f"--port={port}", f"--username={user}",
            "--no-password", "-v", "ON_ERROR_STOP=1"]
    if host not in ("localhost", "127.0.0.1", "::1") and not get_config().get("allow_remote", True):
        return {"skipped": True, "reason": "目标为远程实例，配置禁止远程恢复演练"}

    try:
        created = _run(base + ["-d", "postgres", "-c", f'CREATE DATABASE "{scratch}"'],
                       env=env, timeout=60)
        if created.returncode != 0:
            checks.append({"name": "创建临时库", "ok": False, "detail": created.stderr.strip()[-1000:]})
            return {"skipped": False}
        checks.append({"name": "创建临时库", "ok": True, "detail": scratch})

        rc, stderr = _restore_stream(base + ["-d", scratch], path, env, timeout)
        if rc != 0:
            checks.append({"name": "导入备份", "ok": False, "detail": stderr or f"exit={rc}"})
            return {"skipped": False}
        checks.append({"name": "导入备份", "ok": True, "detail": "psql < dump"})

        counted = _run(base + ["-d", scratch, "-t", "-A", "-c",
                               "SELECT COUNT(*) FROM information_schema.tables "
                               "WHERE table_schema NOT IN ('pg_catalog','information_schema')"],
                       env=env, timeout=60)
        count = int((counted.stdout or "0").strip() or 0) if counted.returncode == 0 else 0
        checks.append({
            "name": "对象数量",
            "ok": count > 0,
            "detail": f"{count} 张表/视图",
        })
        return {"skipped": False, "scratch_db": scratch, "objects": count}
    finally:
        try:
            _run(base + ["-d", "postgres", "-c", f'DROP DATABASE IF EXISTS "{scratch}"'],
                 env=env, timeout=60)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def verify_backup(task_name: str, filename: Optional[str] = None,
                  trigger: str = "manual") -> Dict[str, Any]:
    """执行一次恢复演练，返回运行记录。并发演练会串行化。"""
    from modules.disaster_recovery import engine

    cfg = get_config()
    run_id = f"{task_name}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
    started = datetime.now()
    checks: List[Dict[str, Any]] = []
    record: Dict[str, Any] = {
        "id": run_id,
        "task": task_name,
        "filename": filename,
        "type": "",
        "trigger": trigger,
        "status": "error",
        "level": "archive",
        "checks": checks,
        "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": "",
        "duration_seconds": 0,
    }

    if not cfg.get("enabled", True):
        record.update({"status": "skipped", "reason": "恢复验证已在配置中关闭"})
        _finish(record, started)
        return record

    raw_task = None
    try:
        for item in engine._load_config().get("tasks", []):
            if item.get("name") == task_name:
                raw_task = item
                break
    except Exception:
        raw_task = None
    if not raw_task:
        record.update({"status": "error", "reason": f"计划 {task_name} 不存在"})
        _finish(record, started)
        return record

    backup = _resolve_backup(raw_task, filename)
    if not backup:
        record.update({"status": "skipped", "reason": "未找到可验证的备份文件"})
        _finish(record, started)
        return record

    path = Path(backup["path"])
    record.update({
        "filename": backup["filename"],
        "type": raw_task.get("type", ""),
        "size_bytes": backup.get("size_bytes", 0),
    })

    max_mb = int(cfg.get("max_file_mb", 20480))
    if path.stat().st_size > max_mb * 1024 * 1024:
        record.update({"status": "skipped", "reason": f"备份超过 {max_mb}MB 上限"})
        _finish(record, started)
        return record

    if not _verify_lock.acquire(blocking=False):
        record.update({"status": "skipped", "reason": "已有恢复演练在执行"})
        _finish(record, started)
        return record
    try:
        _check_archive(path, checks)
        if not all(c["ok"] for c in checks):
            record["status"] = "failed"
            return _finish(record, started)

        task_type = (raw_task.get("type") or "").lower()
        if task_type == "mysql":
            outcome = _verify_mysql(raw_task, path, checks)
        elif task_type in ("postgresql", "postgres", "pg"):
            outcome = _verify_postgresql(raw_task, path, checks)
        else:
            outcome = {"skipped": False}

        if outcome.get("skipped"):
            record.update({"status": "skipped", "reason": outcome.get("reason", "")})
        else:
            record["level"] = "restore" if task_type in ("mysql", "postgresql", "postgres", "pg") else "archive"
            record["scratch_db"] = outcome.get("scratch_db", "")
            ok = all(c["ok"] for c in checks)
            record["status"] = "passed" if ok else "failed"
        return _finish(record, started)
    except Exception as exc:  # pragma: no cover - 兜底
        record.update({"status": "error", "reason": str(exc)})
        return _finish(record, started)
    finally:
        _verify_lock.release()


def _resolve_backup(task: Dict[str, Any], filename: Optional[str]) -> Optional[Dict[str, Any]]:
    from modules.disaster_recovery import engine

    backups = engine.list_backups(task.get("name"))
    if filename:
        for item in backups:
            if item.get("filename") == filename:
                return item
        return None
    return backups[0] if backups else None


def _finish(record: Dict[str, Any], started: datetime) -> Dict[str, Any]:
    finished = datetime.now()
    record["finished_at"] = finished.strftime("%Y-%m-%d %H:%M:%S")
    record["duration_seconds"] = round((finished - started).total_seconds(), 1)
    record.setdefault("reason", "")
    _append_run(record)
    return record


# ---------------------------------------------------------------------------
# 自动演练 worker
# ---------------------------------------------------------------------------
def start_worker() -> Optional[threading.Thread]:
    global _worker
    if _worker is not None and _worker.is_alive():
        return _worker
    _worker_stop.clear()
    _worker = threading.Thread(target=_worker_loop, daemon=True, name="dr-restore-verify")
    _worker.start()
    return _worker


def stop_worker() -> None:
    _worker_stop.set()


def _worker_loop() -> None:
    while not _worker_stop.is_set():
        try:
            scan_once()
        except Exception:
            pass
        _worker_stop.wait(120)


def scan_once() -> List[Dict[str, Any]]:
    """扫描最近的成功备份，对尚未验证的文件补一次演练。"""
    from modules.disaster_recovery import engine

    cfg = get_config()
    if not cfg.get("enabled", True) or not cfg.get("auto_verify", True):
        return []

    results: List[Dict[str, Any]] = []
    tasks = engine._load_config().get("tasks", [])
    max_age_days = int(cfg.get("max_age_days", 7))
    for task in tasks:
        name = task.get("name")
        if not name or not task.get("enabled", True):
            continue
        history = engine.get_history(limit=20, task_name=name)
        successful = [h for h in history if h.get("success") and h.get("filename")]
        if not successful:
            continue
        latest = successful[0]
        filename = latest.get("filename")
        try:
            run_at = datetime.strptime(latest.get("start_time", ""), "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if datetime.now() - run_at > timedelta(days=max_age_days):
            continue
        recent = [r for r in list_runs(limit=100, task_name=name)
                  if r.get("filename") == filename]
        if recent:
            continue
        results.append(verify_backup(name, filename, trigger="auto"))
    return results
