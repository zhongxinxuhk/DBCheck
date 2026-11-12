# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Workflow 任务持久化（独立导航「工作流任务」）。

每个任务绑定一个已编排的工作流（``workflow_id``）与一个目标数据源（``instance_id``），
可配置 ``cron`` 周期运行。任务有独立生命周期：``stopped`` / ``running`` / ``error``。
执行产物（每次运行的发现 / 方案 / 输出）落库到 ``workflow_run_store``，本模块
只管任务元数据与状态机。

存储路径一律以 ``modules.core.paths.DATA_DIR`` 为准，与诊断历史库同目录。
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

from __future__ import annotations

import sqlite3
import json
import os
import psutil
from zoneinfo import ZoneInfo
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from modules.core import paths

_DB_PATH = str(paths.DATA_DIR / "intelligence_workflow_tasks.db")
_LOCK = threading.Lock()
_MIGRATED = False

STATUS_STOPPED = "stopped"
STATUS_RUNNING = "running"
STATUS_ERROR = "error"


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def _init_db() -> None:
    global _MIGRATED
    if _MIGRATED:
        return
    with _LOCK:
        if _MIGRATED:
            return
        try:
            conn = sqlite3.connect(_DB_PATH)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_tasks (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    name            TEXT NOT NULL,
                    workflow_id     INTEGER NOT NULL,
                    instance_id     TEXT NOT NULL,
                    goal            TEXT NOT NULL DEFAULT '',
                    cron            TEXT NOT NULL DEFAULT '',
                    status          TEXT NOT NULL DEFAULT 'stopped',
                    last_run_at     TEXT,
                    last_error      TEXT,
                    last_duration_ms INTEGER,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                )
                """)
            existing = {r[1] for r in conn.execute("PRAGMA table_info(workflow_tasks)")}
            additions = {
                "task_type": "TEXT NOT NULL DEFAULT 'workflow'",
                "spec_json": "TEXT NOT NULL DEFAULT '{}'",
                "schedule_enabled": "INTEGER NOT NULL DEFAULT 0",
                "owner_id": "INTEGER",
                "tenant_id": "INTEGER NOT NULL DEFAULT 1",
                "stop_requested": "INTEGER NOT NULL DEFAULT 0",
                "run_pid": "INTEGER",
                "run_process_started": "REAL",
            }
            for column, definition in additions.items():
                if column not in existing:
                    conn.execute(
                        f"ALTER TABLE workflow_tasks ADD COLUMN {column} {definition}"
                    )
            if "schedule_enabled" not in existing:
                conn.execute(
                    "UPDATE workflow_tasks SET schedule_enabled=1 WHERE trim(cron)<>''"
                )
            conn.commit()
            _MIGRATED = True
        finally:
            if "conn" in locals():
                conn.close()


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "task_type": row["task_type"] or "workflow",
        "batch_spec": json.loads(row["spec_json"] or "{}"),
        "schedule_enabled": bool(row["schedule_enabled"]),
        "owner_id": row["owner_id"],
        "tenant_id": row["tenant_id"],
        "stop_requested": bool(row["stop_requested"]),
        "run_pid": row["run_pid"],
        "run_process_started": row["run_process_started"],
        "workflow_id": row["workflow_id"],
        "instance_id": row["instance_id"],
        "goal": row["goal"] or "",
        "cron": row["cron"] or "",
        "status": row["status"] or STATUS_STOPPED,
        "last_run_at": row["last_run_at"],
        "last_error": row["last_error"],
        "last_duration_ms": row["last_duration_ms"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_tasks() -> List[Dict[str, Any]]:
    _init_db()
    with _LOCK:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM workflow_tasks ORDER BY updated_at DESC, id DESC"
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            conn.close()


def get_task(task_id: int) -> Optional[Dict[str, Any]]:
    _init_db()
    with _LOCK:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM workflow_tasks WHERE id=?", (task_id,)
            ).fetchone()
            return _row_to_dict(row) if row else None
        finally:
            conn.close()


def save_task(
    name: str,
    workflow_id: int,
    instance_id: str,
    goal: str = "",
    cron: str = "",
    task_id: Optional[int] = None,
    task_type: str = "workflow",
    batch_spec: Optional[Dict[str, Any]] = None,
    schedule_enabled: Optional[bool] = None,
    owner_id: Optional[int] = None,
    tenant_id: int = 1,
) -> Dict[str, Any]:
    """保存任务。``task_id`` 给定则幂等更新，否则新增。返回完整记录。"""
    if not name or not name.strip():
        raise ValueError("任务名称不能为空")
    if task_type not in {"workflow", "batch_inspection"}:
        raise ValueError("任务类型无效")
    if task_type == "workflow":
        if not workflow_id:
            raise ValueError("必须绑定一个工作流")
        if not instance_id:
            raise ValueError("必须选择一个数据源")
    else:
        workflow_id = 0
        instance_id = ""
        if not isinstance(batch_spec, dict) or not (
            batch_spec.get("instance_ids")
            or batch_spec.get("cluster_ids")
            or batch_spec.get("group_ids")
            or batch_spec.get("selection_filters")
        ):
            raise ValueError("整体巡检须选择数据源或集群")
        allowed = {
            "instance_ids",
            "cluster_ids",
            "group_ids",
            "selection_filters",
            "notify_on_completion",
            "concurrency",
            "timeout_seconds",
            "cluster_concurrency",
            "execution_window",
            "query_timeout",
            "row_limit",
            "templates",
            "inspector",
        }
        if set(batch_spec) - allowed:
            raise ValueError("整体巡检配置包含不支持的字段")
    name = name.strip()
    goal = goal or ""
    cron = (cron or "").strip()
    if cron:
        from croniter import croniter

        if len(cron.split()) != 5 or not croniter.is_valid(cron):
            raise ValueError("Cron须为有效的五段表达式（分 时 日 月 周）")
    if schedule_enabled is not None and not isinstance(schedule_enabled, bool):
        raise ValueError("定时开关须为布尔值")
    enabled = bool(cron) if schedule_enabled is None else schedule_enabled
    if enabled and not cron:
        raise ValueError("启用定时运行须填写Cron")
    spec_json = json.dumps(batch_spec or {}, ensure_ascii=False)
    now = _now()
    _init_db()
    with _LOCK:
        conn = sqlite3.connect(_DB_PATH)
        try:
            if task_id:
                cur = conn.execute(
                    "UPDATE workflow_tasks SET name=?, workflow_id=?, instance_id=?, "
                    "goal=?, cron=?, task_type=?,spec_json=?,schedule_enabled=?,"
                    "owner_id=COALESCE(owner_id,?),updated_at=? WHERE id=?",
                    (
                        name,
                        workflow_id,
                        instance_id,
                        goal,
                        cron,
                        task_type,
                        spec_json,
                        int(enabled),
                        owner_id,
                        now,
                        task_id,
                    ),
                )
                if cur.rowcount == 0:
                    task_id = None
            if not task_id:
                cur = conn.execute(
                    "INSERT INTO workflow_tasks "
                    "(name, workflow_id, instance_id, goal, cron, status, created_at, updated_at,task_type,spec_json,schedule_enabled,owner_id,tenant_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        name,
                        workflow_id,
                        instance_id,
                        goal,
                        cron,
                        STATUS_STOPPED,
                        now,
                        now,
                        task_type,
                        spec_json,
                        int(enabled),
                        owner_id,
                        tenant_id,
                    ),
                )
                task_id = cur.lastrowid
            conn.commit()
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM workflow_tasks WHERE id=?", (task_id,)
            ).fetchone()
            return _row_to_dict(row)
        finally:
            conn.close()


def delete_task(task_id: int) -> bool:
    _init_db()
    with _LOCK:
        conn = sqlite3.connect(_DB_PATH)
        try:
            cur = conn.execute("DELETE FROM workflow_tasks WHERE id=?", (task_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def update_status(
    task_id: int,
    status: str,
    last_error: Optional[str] = None,
    last_run_at: Optional[str] = None,
    last_duration_ms: Optional[int] = None,
) -> None:
    """更新任务运行状态（供 task_runner 在后台线程结束时回写）。"""
    _init_db()
    with _LOCK:
        conn = sqlite3.connect(_DB_PATH)
        try:
            sets = ["status=?", "updated_at=?"]
            if status != STATUS_RUNNING:
                sets.extend(["run_pid=NULL", "run_process_started=NULL"])
            params: List[Any] = [status, _now()]
            if last_error is not None:
                sets.append("last_error=?")
                params.append(last_error)
            if last_run_at is not None:
                sets.append("last_run_at=?")
                params.append(last_run_at)
            if last_duration_ms is not None:
                sets.append("last_duration_ms=?")
                params.append(last_duration_ms)
            params.append(task_id)
            conn.execute(
                "UPDATE workflow_tasks SET %s WHERE id=?" % ", ".join(sets), params
            )
            conn.commit()
        finally:
            conn.close()


def set_schedule_enabled(task_id: int, enabled: bool) -> None:
    _init_db()
    with _LOCK:
        conn = sqlite3.connect(_DB_PATH)
        try:
            conn.execute(
                "UPDATE workflow_tasks SET schedule_enabled=?,updated_at=? WHERE id=?",
                (int(enabled), _now(), task_id),
            )
            conn.commit()
        finally:
            conn.close()


def claim_task(task_id: int) -> bool:
    """跨进程领取任务，防止重复运行。"""
    _init_db()
    with _LOCK:
        conn = sqlite3.connect(_DB_PATH, timeout=15)
        try:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE workflow_tasks SET status='running',stop_requested=0,run_pid=?,run_process_started=?,updated_at=? WHERE id=? AND status<>'running'",
                (os.getpid(), psutil.Process().create_time(), _now(), task_id),
            ).rowcount
            conn.commit()
            return changed == 1
        finally:
            conn.close()


def runtime_alive(task: Dict[str, Any]) -> bool:
    if not task.get("run_pid") or not task.get("run_process_started"):
        return False
    try:
        return (
            abs(
                psutil.Process(task["run_pid"]).create_time()
                - task["run_process_started"]
            )
            < 0.01
        )
    except (psutil.Error, ValueError):
        return False


def request_stop(task_id: int) -> None:
    _init_db()
    with _LOCK:
        conn = sqlite3.connect(_DB_PATH)
        try:
            conn.execute(
                "UPDATE workflow_tasks SET schedule_enabled=0,stop_requested=1,updated_at=? WHERE id=?",
                (_now(), task_id),
            )
            conn.commit()
        finally:
            conn.close()
