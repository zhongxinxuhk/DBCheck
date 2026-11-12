# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Inspection comparisons and encrypted remediation records with verification evidence."""


import hashlib
import json
import re
import time
import uuid
from datetime import date

from .inventory import Conflict

STATES = {
    "pending": "待处理",
    "working": "处理中",
    "review": "待复检",
    "closed": "已关闭",
}


def issue_fields(issue):
    if not isinstance(issue, dict):
        return {"description": str(issue), "level": "", "suggestion": ""}
    return {
        "description": str(issue.get("description", issue.get("col1", ""))),
        "level": str(issue.get("level", issue.get("col2", ""))),
        "suggestion": str(issue.get("suggestion", issue.get("col3", ""))),
    }


def issue_key(issue):
    fields = issue_fields(issue)
    identity = (
        str(issue.get("check_id") or issue.get("rule_id") or fields["description"])
        if isinstance(issue, dict)
        else fields["description"]
    )
    return hashlib.sha256(re.sub(r"\s+", " ", identity).strip().encode()).hexdigest()


def issues(member):
    return {
        issue_key(item): issue_fields(item)
        for item in member.get("result", {}).get("issues", [])
    }


def snapshot_metrics(context):
    """Reuse collected metadata and summaries without issuing database queries."""
    result = {}
    size = (context.get("basic_info_db") or {}).get("db_size")
    if (
        isinstance(size, (str, int, float))
        and not isinstance(size, bool)
        and str(size) not in {"", "未采集", "未登记"}
    ):
        result["数据库文件大小"] = size
    for key, column, label in (
        ("threads_connected", "Value", "当前连接数"),
        ("max_connections", "Value", "最大连接数"),
        ("pg_connections", "used_connections", "当前连接数"),
        ("pg_connections", "max_connections", "最大连接数"),
        ("connections", "total_connections", "当前连接数"),
        ("connection_count", "total_connections", "当前连接数"),
    ):
        rows = context.get(key)
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            value = rows[0].get(column)
            if (
                isinstance(value, (str, int, float))
                and not isinstance(value, bool)
                and str(value).strip()
            ):
                result[label] = value
    slow = context.get("slow_query_result") or {}
    summary = slow.get("summary", {}) if isinstance(slow, dict) else {}
    for key, label in (
        ("current_long_running", "当前长耗时查询数"),
        ("current_long_sql", "当前长耗时查询数"),
        ("total_slow_calls", "累计慢查询调用数"),
    ):
        value = summary.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[label] = value
    return result


def compare_previous(store, run, payload):
    with store.connection() as db:
        rows = db.execute(
            "SELECT id,payload FROM operations_batches WHERE tenant=? AND owner=? AND created<? AND status IN ('completed','partial') ORDER BY created DESC",
            (run["tenant"], run["owner"], run["created"]),
        ).fetchall()
    previous = {}
    wanted = {member["instance_id"]: member for member in payload["members"]}
    for row in rows:
        old_payload = json.loads(row["payload"])
        for member in old_payload["members"]:
            iid = member["instance_id"]
            if (
                iid in wanted
                and iid not in previous
                and member["status"] == "done"
                and not member.get("warnings")
            ):
                kind = member["db_type"]
                if member["db_type"] == wanted[iid]["db_type"] and old_payload.get(
                    "templates", {}
                ).get(kind) == payload.get("templates", {}).get(kind):
                    previous[iid] = (row["id"], member)
    result = []
    for member in payload["members"]:
        item = {
            "instance_id": member["instance_id"],
            "name": member["name"],
            "status": member["status"],
            "new": [],
            "persistent": [],
            "resolved": [],
            "unverified": [],
            "changes": {},
        }
        prior = previous.get(member["instance_id"])
        if not prior:
            item["reason"] = "暂无相同模板的成功巡检基线"
        else:
            item["previous_run_id"], old = prior
            before, after = issues(old), issues(member)
            if member["status"] != "done":
                item["reason"] = "本次未完成，问题恢复情况待核对"
                item["unverified"] = list(before.values())
            else:
                item["new"] = [after[key] for key in after.keys() - before.keys()]
                item["persistent"] = [
                    after[key] for key in after.keys() & before.keys()
                ]
                missing = [before[key] for key in before.keys() - after.keys()]
                item["unverified" if member.get("warnings") else "resolved"] = missing
                for key in ("health_score", "risk_count", "risk_level"):
                    left, right = old.get("result", {}).get(key), member.get(
                        "result", {}
                    ).get(key)
                    if left is not None and right is not None and left != right:
                        item["changes"][key] = {"before": left, "after": right}
                old_metrics = old.get("result", {}).get("metrics", {})
                new_metrics = member.get("result", {}).get("metrics", {})
                for key in old_metrics.keys() & new_metrics.keys():
                    if old_metrics[key] != new_metrics[key]:
                        item["changes"][key] = {
                            "before": old_metrics[key],
                            "after": new_metrics[key],
                        }
                if member.get("warnings"):
                    item["reason"] = "本次存在采集缺项，未出现的问题保留待核对"
        result.append(item)
    return result


class Findings:
    def __init__(self, store, inventory):
        self.store, self.inventory = store, inventory
        with store.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS operations_findings (id TEXT PRIMARY KEY, tenant INTEGER, owner INTEGER, instance_id TEXT, fingerprint TEXT, version INTEGER, updated REAL, payload BLOB, UNIQUE(tenant,owner,instance_id,fingerprint))"
            )

    def record(self, row):
        if row is None:
            raise LookupError("整改记录不存在")
        return {
            "id": row["id"],
            "instance_id": row["instance_id"],
            "version": row["version"],
            "updated": row["updated"],
            **self.inventory.open(row["payload"]),
        }

    def scoped(self, db, actor, fid):
        row = db.execute(
            "SELECT * FROM operations_findings WHERE id=? AND tenant=? AND (owner=? OR ?=1)",
            (fid, actor.tenant_id, actor.id, int(actor.is_admin)),
        ).fetchone()
        self.record(row)
        return row

    def list(self, actor, visible):
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT * FROM operations_findings WHERE tenant=? AND (owner=? OR ?=1) ORDER BY updated DESC",
                (actor.tenant_id, actor.id, int(actor.is_admin)),
            ).fetchall()
        return [self.record(row) for row in rows if row["instance_id"] in visible]

    def create(self, actor, run, iid, fingerprint):
        member = next(
            (item for item in run["payload"]["members"] if item["instance_id"] == iid),
            None,
        )
        found = issues(member) if member else {}
        if not member or member["status"] != "done" or fingerprint not in found:
            raise ValueError("请选择已完成巡检中的异常项")
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM operations_findings WHERE tenant=? AND owner=? AND instance_id=? AND fingerprint=?",
                (actor.tenant_id, actor.id, iid, fingerprint),
            ).fetchone()
            if row:
                existing = self.record(row)
                if existing["state"] == "closed" and run["created"] > row["updated"]:
                    payload = self.inventory.open(row["payload"])
                    payload.update(
                        state="pending",
                        source_run=run["id"],
                        source_created=run["created"],
                        verification_run="",
                    )
                    payload["history"].append(
                        {
                            "at": time.time(),
                            "actor": actor.username,
                            "state": "pending",
                            "note": "后续巡检再次发现该问题，重新登记处理",
                        }
                    )
                    db.execute(
                        "UPDATE operations_findings SET version=version+1,updated=?,payload=? WHERE id=?",
                        (time.time(), self.inventory.seal(payload), row["id"]),
                    )
                    return self.record(
                        db.execute(
                            "SELECT * FROM operations_findings WHERE id=?", (row["id"],)
                        ).fetchone()
                    )
                return existing
            fid = uuid.uuid4().hex
            payload = {
                **found[fingerprint],
                "fingerprint": fingerprint,
                "name": member["name"],
                "state": "pending",
                "assignee": "",
                "deadline": "",
                "action": "",
                "source_run": run["id"],
                "source_created": run["created"],
                "verification_run": "",
                "history": [
                    {
                        "at": time.time(),
                        "actor": actor.username,
                        "state": "pending",
                        "note": "从巡检异常创建",
                    }
                ],
            }
            db.execute(
                "INSERT INTO operations_findings VALUES(?,?,?,?,?,?,?,?)",
                (
                    fid,
                    actor.tenant_id,
                    actor.id,
                    iid,
                    fingerprint,
                    1,
                    time.time(),
                    self.inventory.seal(payload),
                ),
            )
            return self.record(
                db.execute(
                    "SELECT * FROM operations_findings WHERE id=?", (fid,)
                ).fetchone()
            )

    def update(self, actor, fid, data, visible):
        if set(data) - {
            "version",
            "state",
            "assignee",
            "deadline",
            "action",
            "verification_run",
            "note",
        }:
            raise ValueError("整改字段无效")
        with self.store.connection() as db:
            row = self.scoped(db, actor, fid)
            if row["instance_id"] not in visible:
                raise PermissionError("无权访问整改数据源")
            if data.get("version") != row["version"]:
                raise Conflict("整改记录已更新，请刷新")
            value = self.inventory.open(row["payload"])
            for key in ("state", "assignee", "deadline", "action", "verification_run"):
                if key in data:
                    if not isinstance(data[key], str) or len(data[key]) > 10000:
                        raise ValueError("整改字段须为文本且最多10000字符")
                    value[key] = data[key].strip()
            if value["state"] not in STATES:
                raise ValueError("整改状态无效")
            if value["deadline"]:
                date.fromisoformat(value["deadline"])
            if (
                value["state"] in {"working", "review", "closed"}
                and not value["assignee"]
            ):
                raise ValueError("请填写整改负责人")
            if value["state"] in {"review", "closed"} and not value["action"]:
                raise ValueError("请填写处理措施")
            previous = self.inventory.open(row["payload"])
            if value["action"] != previous.get("action"):
                value["remediated_at"] = time.time()
            if value["state"] == "closed":
                verification = self.store.record(
                    db.execute(
                        "SELECT * FROM operations_batches WHERE id=?",
                        (value["verification_run"],),
                    ).fetchone()
                )
                if verification["tenant"] != actor.tenant_id or (
                    verification["owner"] != actor.id and not actor.is_admin
                ):
                    raise PermissionError("无权使用此复检任务")
                member = next(
                    (
                        item
                        for item in verification["payload"]["members"]
                        if item["instance_id"] == row["instance_id"]
                    ),
                    None,
                )
                source = self.store.record(
                    db.execute(
                        "SELECT * FROM operations_batches WHERE id=?",
                        (value["source_run"],),
                    ).fetchone()
                )
                if (
                    not member
                    or member["status"] != "done"
                    or member.get("warnings")
                    or verification["created"]
                    <= max(value["source_created"], value.get("remediated_at", 0))
                ):
                    raise ValueError("请选择后续完整成功的复检任务")
                kind = member["db_type"]
                if verification["payload"].get("templates", {}).get(kind) != source[
                    "payload"
                ].get("templates", {}).get(kind):
                    raise ValueError("复检与原巡检模板须一致")
                if row["fingerprint"] in issues(member):
                    raise ValueError("复检中该问题仍然存在，请继续处理")
            note = data.get("note", "")
            if not isinstance(note, str) or len(note) > 10000:
                raise ValueError("处理备注最多10000字符")
            value["history"].append(
                {
                    "at": time.time(),
                    "actor": actor.username,
                    "state": value["state"],
                    "note": note or value["action"],
                }
            )
            db.execute(
                "UPDATE operations_findings SET version=version+1,updated=?,payload=? WHERE id=?",
                (time.time(), self.inventory.seal(value), fid),
            )
            return self.record(
                db.execute(
                    "SELECT * FROM operations_findings WHERE id=?", (fid,)
                ).fetchone()
            )
