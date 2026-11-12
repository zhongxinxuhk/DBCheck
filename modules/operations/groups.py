# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""Visible inventory facets and reusable dynamic inspection groups."""


import time
import uuid
from .inventory import Conflict

FIELDS = {
    "system": "业务系统",
    "environment": "运行环境",
    "owner": "运维责任人",
    "source_group": "数据源分组",
    "db_type": "数据库类型",
    "cluster_id": "集群",
}


def normalize_filters(value):
    if not isinstance(value, dict) or set(value) - FIELDS.keys():
        raise ValueError("分组筛选字段无效")
    result = {}
    for key, values in value.items():
        if (
            not isinstance(values, list)
            or len(values) > 1000
            or any(not isinstance(item, str) or len(item) > 200 for item in values)
        ):
            raise ValueError("分组条件须为文本列表，每项最多200字符")
        if values:
            result[key] = sorted(set(item.strip() for item in values))
    return result


def cluster_map(clusters):
    result = {}
    for cluster in clusters:
        spec = cluster.get("spec", cluster)
        ids = [
            item["instance_id"]
            for item in spec.get("members", [])
            if item.get("enabled", True)
        ]
        if spec.get("service_instance_id"):
            ids.append(spec["service_instance_id"])
        if "instance_ids" in cluster:
            ids.extend(cluster["instance_ids"])
        for iid in ids:
            result.setdefault(iid, set()).add(cluster["id"])
    return result


def values_for(record, key, memberships):
    if key == "source_group":
        return {str(record["source"].get("group") or "").strip()}
    if key == "db_type":
        return {record["source"]["db_type"]}
    if key == "cluster_id":
        return memberships.get(record["id"], set()) | {
            str(record["metadata"].get("cluster_id") or "")
        }
    return {str(record["metadata"].get(key) or "").strip()}


def matches(record, filters, memberships):
    return all(
        values_for(record, key, memberships) & set(choices)
        for key, choices in filters.items()
    )


def select(records, filters, clusters):
    filters = normalize_filters(filters)
    memberships = cluster_map(clusters)
    return [record for record in records if matches(record, filters, memberships)]


def facets(records, clusters):
    memberships = cluster_map(clusters)
    cluster_names = {row["id"]: row["name"] for row in clusters}
    result = {}
    for key in FIELDS:
        values = sorted(
            set().union(*(values_for(record, key, memberships) for record in records))
        )
        result[key] = [
            {
                "value": value,
                "label": cluster_names.get(value, value) or "未登记",
                "count": sum(
                    value in values_for(record, key, memberships) for record in records
                ),
            }
            for value in values
        ]
    return result


class Groups:
    def __init__(self, store, inventory):
        self.store, self.inventory = store, inventory
        with store.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS operations_groups (id TEXT PRIMARY KEY, tenant INTEGER, owner INTEGER, shared INTEGER NOT NULL, version INTEGER NOT NULL, updated REAL, deleted INTEGER NOT NULL DEFAULT 0, payload BLOB)"
            )

    def record(self, row, actor):
        if row is None:
            raise LookupError("巡检分组不存在或无权访问")
        value = self.inventory.open(row["payload"])
        return {
            "id": row["id"],
            "version": row["version"],
            "updated": row["updated"],
            "shared": bool(row["shared"]),
            "can_edit": actor.is_admin or row["owner"] == actor.id,
            **value,
        }

    def get(self, actor, gid, write=False, db=None):
        if db is None:
            with self.store.connection() as connection:
                return self.get(actor, gid, write, connection)
        row = db.execute(
            "SELECT * FROM operations_groups WHERE id=? AND tenant=? AND deleted=0 AND (owner=? OR shared=1 OR ?=1)",
            (gid, actor.tenant_id, actor.id, int(actor.is_admin)),
        ).fetchone()
        value = self.record(row, actor)
        if write and not value["can_edit"]:
            raise PermissionError("仅分组创建人或管理员可维护此分组")
        return value

    def list(self, actor, records, clusters):
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT * FROM operations_groups WHERE tenant=? AND deleted=0 AND (owner=? OR shared=1 OR ?=1) ORDER BY updated DESC",
                (actor.tenant_id, actor.id, int(actor.is_admin)),
            ).fetchall()
        result = []
        for row in rows:
            value = self.record(row, actor)
            value["instance_ids"] = [
                record["id"]
                for record in select(records, value["filters"], clusters)
                if record["source"].get("enabled")
            ]
            value["count"] = len(value["instance_ids"])
            result.append(value)
        return result

    def save(self, actor, data, gid=None):
        if (
            set(data) - {"name", "filters", "shared", "version"}
            or not isinstance(data.get("name"), str)
            or not data["name"].strip()
            or len(data["name"]) > 100
        ):
            raise ValueError("请填写最多100字符的分组名称")
        if not isinstance(data.get("shared", False), bool):
            raise ValueError("共享范围格式无效")
        filters = normalize_filters(data.get("filters", {}))
        with self.store.connection() as db:
            old = self.get(actor, gid, True, db) if gid else None
            if old and data.get("version") != old["version"]:
                raise Conflict("分组条件已被修改，请刷新后保存")
            stamp = time.time()
            history = (old or {}).get("history", []) + [
                {
                    "at": stamp,
                    "actor": actor.username,
                    "name": data["name"].strip(),
                    "filters": filters,
                    "shared": data.get("shared", False),
                }
            ]
            payload = {
                "name": data["name"].strip(),
                "filters": filters,
                "history": history[-100:],
            }
            if gid:
                db.execute(
                    "UPDATE operations_groups SET shared=?,version=version+1,updated=?,payload=? WHERE id=?",
                    (
                        int(data.get("shared", False)),
                        stamp,
                        self.inventory.seal(payload),
                        gid,
                    ),
                )
            else:
                gid = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO operations_groups(id,tenant,owner,shared,version,updated,payload) VALUES(?,?,?,?,1,?,?)",
                    (
                        gid,
                        actor.tenant_id,
                        actor.id,
                        int(data.get("shared", False)),
                        stamp,
                        self.inventory.seal(payload),
                    ),
                )
            return self.get(actor, gid, False, db)

    def delete(self, actor, gid, version):
        with self.store.connection() as db:
            value = self.get(actor, gid, True, db)
            if value["version"] != version:
                raise Conflict("分组已更新，请刷新后删除")
            db.execute(
                "UPDATE operations_groups SET deleted=1,version=version+1,updated=? WHERE id=?",
                (time.time(), gid),
            )


def send_completion(run, report_file):
    """Use the installation's configured recipients and channels after explicit opt-in."""
    import html
    from modules.notify import EmailNotifier, WebhookNotifier, _load_config

    try:
        config = _load_config()
    except Exception:
        return {
            "status": "failed",
            "channels": [],
            "error": "通知配置读取失败，请检查通知设置",
        }
    payload = run["payload"]
    names = [scope["name"] for scope in payload.get("scope_snapshot", [])]
    label = (payload["name"] + (" · " + "、".join(names) if names else ""))[:500]
    counts = {
        "completed": "完成",
        "partial": "部分完成",
        "failed": "失败",
        "cancelled": "已取消",
        "interrupted": "已中断",
    }
    risk_counts = [
        member.get("result", {}).get("risk_count")
        for member in payload["members"]
        if member["status"] == "done"
    ]
    issue_count = (
        sum(risk_counts)
        if len(risk_counts) == len(payload["members"])
        and all(type(value) is int for value in risk_counts)
        else None
    )
    channels = []
    email = config.get("email", {})
    if email.get("recipients") and email.get("enabled", True) is not False:
        try:
            sent = EmailNotifier(email).send_report(
                html.escape(label),
                "整体巡检",
                str(report_file),
                issue_count=issue_count,
            )
            channels.append({"channel": "邮件", "status": "sent" if sent else "failed"})
        except Exception:
            channels.append(
                {
                    "channel": "邮件",
                    "status": "failed",
                    "error": "邮件发送失败，请检查通知设置与服务日志",
                }
            )
    webhook = config.get("webhook", {})
    if webhook.get("url") and webhook.get("enabled", True) is not False:
        try:
            sent = WebhookNotifier(webhook).send_alert(
                label,
                "整体巡检",
                counts.get(run["status"], run["status"]),
                issue_count=issue_count,
            )
            channels.append(
                {"channel": "Webhook", "status": "sent" if sent else "failed"}
            )
        except Exception:
            channels.append(
                {
                    "channel": "Webhook",
                    "status": "failed",
                    "error": "Webhook发送失败，请检查通知设置与服务日志",
                }
            )
    return {
        "status": (
            "sent"
            if channels and all(item["status"] == "sent" for item in channels)
            else "failed" if channels else "skipped"
        ),
        "channels": channels,
    }
