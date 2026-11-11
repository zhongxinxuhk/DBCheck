# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""数据库运维中台的资产台账与多数据源巡检。"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import copy
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import psutil
from flask import g, session
from modules.core.data_security import redact
from .inventory import Conflict

TERMINAL = {"completed", "partial", "failed", "interrupted", "cancelled"}


class BatchStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS operations_batches (
                    id TEXT PRIMARY KEY, tenant INTEGER NOT NULL, owner INTEGER NOT NULL,
                    request_key TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                    pid INTEGER NOT NULL, process_started REAL NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(tenant,owner,request_key)
                );
                CREATE TABLE IF NOT EXISTS operations_batch_leases (
                    instance_id TEXT PRIMARY KEY, run_id TEXT NOT NULL
                );
            """)
        with self.connection() as db:
            if "cancel_requested" not in {
                r["name"] for r in db.execute("PRAGMA table_info(operations_batches)")
            }:
                db.execute(
                    "ALTER TABLE operations_batches ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
                )

    def request_cancel(self, run_id):
        with self.connection() as db:
            db.execute(
                "UPDATE operations_batches SET cancel_requested=1 WHERE id=?", (run_id,)
            )

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def record(row):
        if row is None:
            raise LookupError("批量巡检任务不存在")
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value

    def create(self, actor, key, spec, members):
        fingerprint = hashlib.sha256(
            json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM operations_batches WHERE tenant=? AND owner=? AND request_key=?",
                (actor.tenant_id, actor.id, key),
            ).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise Conflict("请求标识已用于不同的巡检范围")
                return self.record(row), False
            run_id = str(uuid.uuid4())
            payload = dict(spec, members=members, finished_at=None)
            ts = time.time()
            db.execute(
                "INSERT INTO operations_batches(id,tenant,owner,request_key,fingerprint,status,pid,process_started,created,updated,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    actor.tenant_id,
                    actor.id,
                    key,
                    fingerprint,
                    "queued",
                    os.getpid(),
                    psutil.Process().create_time(),
                    ts,
                    ts,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            try:
                db.executemany(
                    "INSERT INTO operations_batch_leases VALUES(?,?)",
                    [(m["instance_id"], run_id) for m in members],
                )
            except sqlite3.IntegrityError as error:
                raise Conflict(
                    "所选数据源已有批量巡检任务，请等待当前任务结束"
                ) from error
        return self.get(run_id), True

    def get(self, run_id):
        with self.connection() as db:
            run = self.record(
                db.execute(
                    "SELECT * FROM operations_batches WHERE id=?", (run_id,)
                ).fetchone()
            )
        if run["status"] not in TERMINAL:
            try:
                alive = (
                    abs(
                        psutil.Process(run["pid"]).create_time()
                        - run["process_started"]
                    )
                    < 0.01
                )
            except (psutil.Error, ValueError):
                alive = False
            if not alive:
                for member in run["payload"]["members"]:
                    if member["status"] not in {"done", "error", "interrupted"}:
                        member.update(
                            status="interrupted",
                            error="执行进程已退出，须复核原任务结果后重新巡检",
                        )
                run["payload"]["finished_at"] = time.time()
                self.update(run_id, run["payload"], "interrupted")
                run["status"] = "interrupted"
        return run

    def update(self, run_id, payload, status):
        with self.connection() as db:
            db.execute(
                "UPDATE operations_batches SET payload=?,status=?,updated=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), status, time.time(), run_id),
            )
            if status in TERMINAL:
                db.execute(
                    "DELETE FROM operations_batch_leases WHERE run_id=?", (run_id,)
                )

    def list(self, actor):
        with self.connection() as db:
            rows = db.execute(
                "SELECT id FROM operations_batches WHERE tenant=? AND (owner=? OR ?=1) "
                "ORDER BY created DESC LIMIT 100",
                (actor.tenant_id, actor.id, int(actor.is_admin)),
            ).fetchall()
        return [self.get(row["id"]) for row in rows]


class BatchService:
    def __init__(self, app, store, reports_dir, inventory):
        self.app = app
        self.store = store
        self.reports_dir = Path(reports_dir)
        self.inventory = inventory
        self.report_lock = threading.Lock()

    def launch(self, run):
        threading.Thread(
            target=self.run,
            args=(run,),
            name="operations-batch-" + run["id"][:8],
            daemon=True,
        ).start()

    def dispatch(self, run, member):
        from modules.access import get_principal

        actor = get_principal(run["owner"])
        if actor is None or actor.tenant_id != run["tenant"]:
            raise PermissionError("任务所属用户已失效")
        if (
            self.inventory.get(member["instance_id"])["revision"]
            != member["source_revision"]
        ):
            raise Conflict(
                "台账或连接配置已在任务创建后变化，本次跳过该数据源，请核对后重新巡检"
            )
        payload = {
            "db_type": member["db_type"],
            "datasource_id": member["instance_id"],
            "inspector_name": run["payload"]["inspector"],
            "desensitize": False,
            "isolate_process": True,
            "template_id": run["payload"].get("templates", {}).get(member["db_type"]),
        }
        with self.app.test_request_context(
            "/api/start_inspection", method="POST", json=payload
        ):
            g.security_principal = actor
            session.update(
                user_id=actor.id,
                username=actor.username,
                user_roles=list(actor.roles),
                role=next(iter(actor.roles), "user"),
                is_admin=actor.is_admin,
            )
            response = self.app.make_response(
                self.app.view_functions["api_start_inspection"]()
            )
            result = response.get_json()
        if response.status_code >= 400 or not result or not result.get("ok"):
            raise ValueError(
                (result or {}).get("msg")
                or (result or {}).get("error")
                or "数据源巡检启动失败"
            )
        return result["task_id"]

    def task(self, task_id):
        from modules.web.app import tasks

        return tasks.get(task_id)

    def valid_report(self, value):
        path = Path(value).resolve() if value else None
        return (
            path
            if path
            and path.is_relative_to(self.reports_dir.resolve())
            and path.is_file()
            else None
        )

    def run(self, run):
        payload = copy.deepcopy(run["payload"])
        try:
            with self.app.app_context():
                while True:
                    if self.store.get(run["id"]).get("cancel_requested"):
                        for member in payload["members"]:
                            if member["status"] == "queued":
                                member.update(
                                    status="cancelled",
                                    finished_at=time.time(),
                                    error="任务已停止，未再启动该数据源",
                                )
                    active = [
                        m
                        for m in payload["members"]
                        if m["status"] in {"running", "unconfirmed"}
                    ]
                    for member in active:
                        task = self.task(member["task_id"])
                        if task is None:
                            member.update(
                                status="interrupted",
                                error="子任务状态丢失，须复核执行结果",
                            )
                        elif task.get("status") in {
                            "done",
                            "error",
                            "failed",
                        } and task.get("execution_complete", True):
                            result = copy.deepcopy(task.get("result") or {})
                            report = self.valid_report(
                                task.get("report_file") or task.get("report_path")
                            )
                            success = (
                                task.get("status") == "done" and report is not None
                            )
                            logs = task.get("log") or []
                            warnings = [
                                str(line)[:500]
                                for line in logs
                                if any(
                                    word in str(line)
                                    for word in (
                                        "查询失败",
                                        "已跳过",
                                        "未采集",
                                        "采集不完整",
                                    )
                                )
                            ][-100:]
                            member.update(
                                status="done" if success else "error",
                                finished_at=time.time(),
                                result=result,
                                warnings=warnings,
                                report_file=str(report) if report else "",
                            )
                            if not success:
                                member["error"] = str(
                                    redact(
                                        str(
                                            task.get("error_msg")
                                            or task.get("error")
                                            or "巡检失败或明细报告未生成"
                                        )
                                    )
                                )[:500]
                        elif (
                            time.time() - member["started_at"]
                            >= payload["timeout_seconds"]
                        ):
                            member.update(
                                status="unconfirmed",
                                error="等待子任务超时，保留执行槽位并继续核对原任务，避免重复执行",
                            )
                    active = [
                        m
                        for m in payload["members"]
                        if m["status"] in {"running", "unconfirmed"}
                    ]
                    queued = [m for m in payload["members"] if m["status"] == "queued"]
                    busy = {m.get("endpoint_key", m["instance_id"]) for m in active}
                    available = []
                    for candidate in queued:
                        key = candidate.get("endpoint_key", candidate["instance_id"])
                        if key not in busy and len(available) < max(
                            0, payload["concurrency"] - len(active)
                        ):
                            available.append(candidate)
                            busy.add(key)
                    for member in available:
                        if self.store.get(run["id"]).get("cancel_requested"):
                            break
                        try:
                            task_id = self.dispatch(run, member)
                            member.update(
                                status="running",
                                task_id=task_id,
                                started_at=time.time(),
                            )
                        except Exception as error:
                            member.update(
                                status="error",
                                finished_at=time.time(),
                                error=str(redact(str(error)))[:500],
                            )
                    unfinished = any(
                        m["status"] in {"queued", "running", "unconfirmed"}
                        for m in payload["members"]
                    )
                    self.store.update(run["id"], payload, "running")
                    if not unfinished:
                        break
                    time.sleep(0.5)
                completed = sum(m["status"] == "done" for m in payload["members"])
                status = (
                    "completed"
                    if completed == len(payload["members"])
                    else "partial" if completed else "failed"
                )
                if self.store.get(run["id"]).get("cancel_requested"):
                    status = "cancelled"
                payload["finished_at"] = time.time()
                self.store.update(run["id"], payload, status)
                try:
                    self.ensure_report(self.store.get(run["id"]))
                except Exception as error:
                    payload["report_error"] = str(redact(str(error)))[:500]
                    self.store.update(run["id"], payload, status)
        except Exception as error:
            for member in payload["members"]:
                if member["status"] in {"queued", "running", "unconfirmed"}:
                    member.update(
                        status="interrupted", error="调度异常，请复核原子任务状态"
                    )
            payload.update(finished_at=time.time(), error=str(redact(str(error)))[:500])
            self.store.update(run["id"], payload, "interrupted")

    def ensure_report(self, run):
        from .exports import batch_docx

        self.reports_dir.mkdir(parents=True, exist_ok=True)
        output = self.reports_dir / ("整体巡检报告_" + run["id"] + ".docx")
        with self.report_lock:
            if not output.is_file():
                temporary = output.with_suffix(".tmp")
                try:
                    batch_docx(run, self.reports_dir).save(temporary)
                    os.replace(temporary, output)
                finally:
                    temporary.unlink(missing_ok=True)
        if run["payload"].get("report_error"):
            run["payload"].pop("report_error")
            self.store.update(run["id"], run["payload"], run["status"])
        return output


def summary(run):
    members = run["payload"]["members"]
    return {
        "total": len(members),
        "done": sum(m["status"] == "done" for m in members),
        "failed": sum(m["status"] in {"error", "interrupted"} for m in members),
        "active": sum(m["status"] in {"running", "unconfirmed"} for m in members),
        "queued": sum(m["status"] == "queued" for m in members),
        "incomplete": sum(bool(m.get("warnings")) for m in members),
        "cancelled": sum(m["status"] == "cancelled" for m in members),
    }
