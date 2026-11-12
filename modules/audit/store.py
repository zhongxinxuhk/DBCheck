# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""统一审计事件存储（append-only + 哈希链）。

- 默认落 ``data/user_db/audit.db``，可用 ``DBPULSE_AUDIT_DB`` 覆盖（测试/容器）。
- 表上挂 BEFORE UPDATE / BEFORE DELETE 触发器，数据库层面拒绝改写历史。
- 每条记录带 ``prev_hash`` 与前一条的 ``hash``，``verify_chain`` 可发现断链。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from modules.core import paths

_WRITE_LOCK = threading.Lock()

_FIELDS = (
    "ts", "actor", "actor_id", "action", "method", "path", "target",
    "status_code", "result", "ip", "ua", "client", "request_id", "detail",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT,
    actor_id    TEXT,
    action      TEXT NOT NULL,
    method      TEXT,
    path        TEXT,
    target      TEXT,
    status_code INTEGER,
    result      TEXT,
    ip          TEXT,
    ua          TEXT,
    client      TEXT,
    request_id  TEXT,
    detail      TEXT,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_event(ts);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_event(actor, ts);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_event(action, ts);
CREATE INDEX IF NOT EXISTS idx_audit_result ON audit_event(result, ts);
CREATE TRIGGER IF NOT EXISTS audit_event_no_update
BEFORE UPDATE ON audit_event
BEGIN
    SELECT RAISE(ABORT, 'audit_event is append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_event_no_delete
BEFORE DELETE ON audit_event
BEGIN
    SELECT RAISE(ABORT, 'audit_event is append-only');
END;
"""


def db_path() -> Path:
    override = os.environ.get("DBPULSE_AUDIT_DB")
    if override:
        return Path(override)
    return Path(paths.USER_DB_DIR) / "audit.db"


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _canonical(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(prev_hash: str, payload: Dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + "|" + _canonical(payload)).encode("utf-8")).hexdigest()


def append_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """追加一条事件，返回完整记录（含 id / hashes）。"""
    record = {key: event.get(key) for key in _FIELDS}
    record["ts"] = record["ts"] or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    record["actor"] = record["actor"] or "anonymous"
    record["action"] = record["action"] or "unknown"
    record["result"] = record["result"] or "success"
    record["request_id"] = record["request_id"] or uuid.uuid4().hex[:16]

    with _WRITE_LOCK:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT hash FROM audit_event ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev_hash = row["hash"] if row else "GENESIS"
            payload = {key: record.get(key) for key in _FIELDS}
            digest = _hash(prev_hash, payload)
            columns = ", ".join(list(_FIELDS) + ["prev_hash", "hash"])
            marks = ", ".join(["?"] * (len(_FIELDS) + 2))
            values = [record.get(key) for key in _FIELDS] + [prev_hash, digest]
            cur = conn.execute(
                f"INSERT INTO audit_event ({columns}) VALUES ({marks})", values
            )
            conn.commit()
            record["id"] = cur.lastrowid
            record["prev_hash"] = prev_hash
            record["hash"] = digest
            return record
        finally:
            conn.close()


def _where(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    mapping = {
        "actor": "actor = ?",
        "action": "action LIKE ?",
        "method": "method = ?",
        "result": "result = ?",
        "client": "client = ?",
    }
    for key, clause in mapping.items():
        value = filters.get(key)
        if value:
            params.append(f"%{value}%" if key == "action" else value)
            clauses.append(clause)
    if filters.get("path"):
        clauses.append("path LIKE ?")
        params.append(f"%{filters['path']}%")
    if filters.get("since"):
        clauses.append("ts >= ?")
        params.append(filters["since"])
    if filters.get("until"):
        clauses.append("ts <= ?")
        params.append(filters["until"])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def query_events(filters: Optional[Dict[str, Any]] = None,
                 limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    filters = filters or {}
    where, params = _where(filters)
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    conn = connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM audit_event{where}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT * FROM audit_event{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return {"total": total, "events": [dict(r) for r in rows]}
    finally:
        conn.close()


def iter_events(filters: Optional[Dict[str, Any]] = None,
                limit: int = 10000) -> List[Dict[str, Any]]:
    filters = filters or {}
    where, params = _where(filters)
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM audit_event{where} ORDER BY id ASC LIMIT ?",
            params + [max(1, min(int(limit), 100000))],
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def verify_chain(limit: int = 100000) -> Dict[str, Any]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_event ORDER BY id ASC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    prev = "GENESIS"
    for row in rows:
        payload = {key: row[key] for key in _FIELDS}
        expected = _hash(prev, payload)
        if row["prev_hash"] != prev or row["hash"] != expected:
            return {
                "ok": False,
                "checked": len(rows),
                "broken_id": row["id"],
                "reason": "哈希链在事件 %s 处断开" % row["id"],
            }
        prev = row["hash"]
    return {"ok": True, "checked": len(rows), "broken_id": None, "head": prev}


def stats() -> Dict[str, Any]:
    conn = connect()
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM audit_event").fetchone()["n"]
        since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        recent = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_event WHERE ts >= ?", (since,)
        ).fetchone()["n"]
        by_result = [dict(r) for r in conn.execute(
            "SELECT result, COUNT(*) AS n FROM audit_event GROUP BY result ORDER BY n DESC"
        ).fetchall()]
        top_actions = [dict(r) for r in conn.execute(
            "SELECT action, COUNT(*) AS n FROM audit_event GROUP BY action ORDER BY n DESC LIMIT 10"
        ).fetchall()]
        top_actors = [dict(r) for r in conn.execute(
            "SELECT actor, COUNT(*) AS n FROM audit_event GROUP BY actor ORDER BY n DESC LIMIT 10"
        ).fetchall()]
        daily = [dict(r) for r in conn.execute(
            "SELECT substr(ts, 1, 10) AS day, COUNT(*) AS n FROM audit_event "
            "GROUP BY day ORDER BY day DESC LIMIT 7"
        ).fetchall()]
        return {
            "total": total,
            "last_24h": recent,
            "by_result": by_result,
            "top_actions": top_actions,
            "top_actors": top_actors,
            "daily": list(reversed(daily)),
        }
    finally:
        conn.close()
