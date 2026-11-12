# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""数据库运维中台的资产台账与多数据源巡检。"""


import copy
import json
import sqlite3
import uuid
from contextlib import contextmanager, closing
from datetime import datetime, timezone

from modules.pro.instance_manager import (
    DatabaseInstance,
    _encrypt_pwd,
    _decrypt_pwd,
    _get_fernet,
)


class Conflict(ValueError):
    pass


# 服务要求 C3:C9 中的资产、集群、基础属性及各专项台账字段。
ASSET_FIELDS = {
    "configuration_id": "配置项编号",
    "project_id": "项目编号",
    "system": "系统归属",
    "business": "业务用途",
    "environment": "运行环境",
    "owner": "运维责任人",
    "online_date": "上线日期",
    "lifecycle": "运行状态",
    "deployment": "部署方式",
    "hardware": "硬件配置",
    "software_version": "软件版本",
    "version": "数据库版本",
    "cluster_id": "集群编号",
    "cluster_type": "集群类型",
    "node_count": "节点数量",
    "node_names": "节点名称",
    "architecture": "集群架构",
    "cluster_network": "集群网络配置",
    "instance_name": "实例名",
    "charset": "字符集",
    "file_size": "数据库文件大小",
    "archive_path": "归档路径",
    "archive_mode": "归档模式",
    "host_model": "主机型号",
    "location": "设备位置",
    "os_version": "OS版本",
    "storage_platform": "存储平台",
    "patch_level": "补丁号",
    "storage_mode": "存储模式",
    "backup_policy": "备份策略",
    "disaster_recovery": "容灾状态",
    "cluster_attributes": "集群属性",
    "baseline_version": "配置基线版本",
    "inventory_date": "盘点日期",
    "change_reference": "变更记录引用",
    "remarks": "备注",
}
TABLES = {
    "cluster_parameters": (
        "集群参数表",
        {
            "name": "参数名称",
            "value": "参数值",
            "node": "节点",
            "version": "参数版本",
            "change_reference": "变更记录",
        },
    ),
    "database_parameters": (
        "数据库参数表",
        {
            "name": "参数名称",
            "value": "参数值",
            "version": "参数版本",
            "effective_at": "生效时间",
            "change_reference": "变更记录",
        },
    ),
    "tablespaces": (
        "逻辑存储空间属性表",
        {
            "name": "逻辑存储空间名称",
            "type": "类型",
            "size": "存储大小",
            "file_path": "文件路径",
            "autoextend": "自动扩展模式",
            "usage_threshold": "使用率阈值",
        },
    ),
    "accounts": (
        "用户及权限表",
        {
            "username": "用户名",
            "purpose": "业务用途",
            "privileges": "具体权限",
            "owner": "责任归属人",
            "change_reference": "权限变更记录",
        },
    ),
    "backup_policies": (
        "备份策略表",
        {
            "data_type": "备份数据类型",
            "content": "备份内容",
            "window": "执行时间窗口",
            "method": "备份方式",
            "frequency": "执行频率",
            "retention": "保存周期",
            "owner": "运维责任人",
            "version": "策略版本",
            "change_reference": "策略变更记录",
        },
    ),
}
SOURCE_FIELDS = {
    "name": "资产/数据源名称",
    "db_type": "数据库类型",
    "host": "数据库IP/主机",
    "port": "数据库端口",
    "user": "数据库账号",
    "password": "数据库密码",
    "database": "数据库名称",
    "service_name": "服务名",
    "group": "数据源分组",
    "description": "连接说明",
    "tenant": "数据库租户",
    "gbase_server_name": "GBase服务器实例名",
    "sysdba": "SYSDBA连接",
    "jdbc_url": "JDBC连接串",
    "connection_mode": "驱动连接模式",
    "driver_version": "驱动版本",
    "use_sid": "使用SID",
    "seed_nodes": "集群种子节点",
    "connect_mode": "MongoDB连接模式",
    "auth_source": "认证源",
    "auth_mechanism": "认证机制",
    "replica_set": "副本集",
    "tls": "启用TLS",
    "tls_ca_file": "TLS CA证书路径",
    "tls_cert_key_file": "TLS客户端证书路径",
    "tls_allow_invalid_certs": "允许无效TLS证书",
    "encrypt": "JDBC加密",
    "trust_server_certificate": "信任服务器证书",
    "ssh_enabled": "启用SSH",
    "ssh_tunnel": "SSH数据库隧道",
    "ssh_host": "SSH主机",
    "ssh_port": "SSH端口",
    "ssh_user": "SSH账号",
    "ssh_password": "SSH密码",
    "ssh_key_file": "SSH私钥路径",
    "ssh_key_password": "SSH私钥密码",
    "enabled": "启用数据源",
    "tags": "标签",
}
SECRETS = {"password", "ssh_password", "ssh_key_password"}
BOOLEAN_FIELDS = {
    "sysdba",
    "use_sid",
    "tls",
    "tls_allow_invalid_certs",
    "encrypt",
    "trust_server_certificate",
    "ssh_enabled",
    "ssh_tunnel",
    "enabled",
}


def now():
    return datetime.now(timezone.utc).isoformat()


class Inventory:
    """台账元数据与权威数据源位于同一 SQLite 文件，单事务同步更新。"""

    def __init__(self, manager):
        self.manager = manager
        self.path = manager.instances_db
        self.cipher = _get_fernet()
        with closing(sqlite3.connect(self.path)) as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='instances'"
            ).fetchone()
        if not exists:
            manager._save_data()
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS operations_inventory (
                    instance_id TEXT PRIMARY KEY, tenant INTEGER NOT NULL,
                    configuration_id TEXT NOT NULL, version INTEGER NOT NULL,
                    payload BLOB NOT NULL, UNIQUE(tenant,configuration_id)
                );
                CREATE TABLE IF NOT EXISTS operations_inventory_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, instance_id TEXT NOT NULL,
                    tenant INTEGER NOT NULL, actor INTEGER NOT NULL, action TEXT NOT NULL,
                    created TEXT NOT NULL, payload BLOB NOT NULL
                );
            """)

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def seal(self, value):
        return self.cipher.encrypt(json.dumps(value, ensure_ascii=False).encode())

    def open(self, value):
        return json.loads(self.cipher.decrypt(value))

    def _record(self, db, iid, decrypt=False):
        row = db.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
        if row is None:
            raise LookupError("台账或数据源不存在")
        source = dict(row)
        source["db_type"] = {
            "postgresql": "pg",
            "dm8": "dm",
            "oracle_full": "oracle",
        }.get(source["db_type"], source["db_type"])
        source["tags"] = json.loads(source.get("tags") or "[]")
        stored = db.execute(
            "SELECT * FROM operations_inventory WHERE instance_id=?", (iid,)
        ).fetchone()
        metadata = self.open(stored["payload"]) if stored else {}
        version = stored["version"] if stored else 0
        if decrypt:
            for key in SECRETS:
                source[key] = _decrypt_pwd(source[key]) if source.get(key) else ""
        else:
            for key in SECRETS:
                source.pop(key, None)
        return {
            "id": iid,
            "revision": source["updated_at"] + "/" + str(version),
            "source": source,
            "metadata": metadata,
            "configuration_id": metadata.get("configuration_id") or "CI-" + iid,
            "version": version,
        }

    def get(self, iid, decrypt=False):
        with self.connection() as db:
            return self._record(db, iid, decrypt)

    def list(self, ids, decrypt=False):
        with self.connection() as db:
            return [self._record(db, iid, decrypt) for iid in ids]

    def _refresh(self):
        self.manager._bump_rev(self.path)
        self.manager._load_data()

    def save(self, actor, data, iid=None, new_id=None):
        with self.connection(write=True) as db:
            record = self._save(actor, data, iid, new_id, db)
        self._refresh()
        return record

    def _save(self, actor, data, iid=None, new_id=None, db=None):
        source_updates = data.get("source", {})
        metadata_updates = data.get("metadata", {})
        if not isinstance(source_updates, dict) or not isinstance(
            metadata_updates, dict
        ):
            raise ValueError("连接信息和台账内容须为对象")
        if set(source_updates) - SOURCE_FIELDS.keys() or set(metadata_updates) - (
            ASSET_FIELDS.keys() | {"tables"}
        ):
            raise ValueError("台账包含不支持的字段")
        for key, value in metadata_updates.items():
            if key != "tables" and (
                isinstance(value, (dict, list)) or len(str(value)) > 32767
            ):
                raise ValueError("台账属性须为文本或数值，单元格最多32767字符")
        old = self._record(db, iid) if iid else None
        if old and data.get("revision") != old["revision"]:
            raise Conflict("台账或数据源已被修改，请刷新后保存")
        metadata = {
            **(old["metadata"] if old else {}),
            **copy.deepcopy(metadata_updates),
        }
        if old and isinstance(metadata_updates.get("tables"), dict):
            metadata["tables"] = {
                **old["metadata"].get("tables", {}),
                **metadata_updates["tables"],
            }
        node_count = metadata.get("node_count")
        if node_count not in (None, ""):
            if (
                isinstance(node_count, bool)
                or not str(node_count).isdigit()
                or int(node_count) < 1
            ):
                raise ValueError("节点数量须为正整数")
            metadata["node_count"] = int(node_count)
        tables = metadata.get("tables", {})
        if not isinstance(tables, dict) or set(tables) - TABLES.keys():
            raise ValueError("专项台账格式无效")
        for key, rows in tables.items():
            if not isinstance(rows, list) or len(rows) > 10000:
                raise ValueError("专项台账须为列表，最多10000行")
            for row in rows:
                if not isinstance(row, dict) or set(row) - TABLES[key][1].keys():
                    raise ValueError("专项台账列名无效")
                if any(
                    isinstance(value, (dict, list)) or len(str(value)) > 32767
                    for value in row.values()
                ):
                    raise ValueError("专项台账单元格须为文本或数值，最多32767字符")
        iid = iid or new_id or uuid.uuid4().hex[:12]
        if old:
            stored = dict(
                db.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
            )
            stored["tags"] = json.loads(stored.get("tags") or "[]")
        else:
            stored = DatabaseInstance(
                id=iid, name="", db_type="", host="", port=0, user=""
            ).to_dict()
        for key, value in source_updates.items():
            if key not in BOOLEAN_FIELDS | {
                "tags",
                "port",
                "ssh_port",
            } and not isinstance(value, str):
                raise ValueError("连接文本字段格式无效")
            if len(str(value)) > 32767:
                raise ValueError("连接字段最多32767字符")
            if key in SECRETS:
                if value:
                    stored[key] = _encrypt_pwd(str(value))
            else:
                stored[key] = value
        for key in ("name", "db_type", "host"):
            if not isinstance(stored.get(key), str) or not stored[key].strip():
                raise ValueError("资产名称、数据库类型和主机地址必填")
            stored[key] = stored[key].strip()
        stored["db_type"] = {
            "postgresql": "pg",
            "dm8": "dm",
            "oracle_full": "oracle",
        }.get(stored["db_type"].lower(), stored["db_type"].lower())
        for key in ("port", "ssh_port"):
            try:
                value = stored.get(key, 22)
                if isinstance(value, bool) or (
                    isinstance(value, float) and not value.is_integer()
                ):
                    raise ValueError()
                stored[key] = int(value)
                if not 1 <= stored[key] <= 65535:
                    raise ValueError()
            except (TypeError, ValueError):
                raise ValueError("端口须为1到65535的整数")
        for key in BOOLEAN_FIELDS:
            if key not in source_updates:
                stored[key] = bool(stored.get(key))
            if stored.get(key) not in (True, False, 0, 1):
                raise ValueError("连接开关须为布尔值")
            stored[key] = bool(stored.get(key))
        if not isinstance(stored.get("tags"), list) or any(
            not isinstance(t, str) for t in stored["tags"]
        ):
            raise ValueError("标签须为列表")
        if not old:
            duplicate = db.execute(
                "SELECT id FROM instances WHERE db_type=? AND host=? AND port=? "
                'AND "user"=? AND "database"=? AND service_name=?',
                tuple(
                    stored.get(k, "")
                    for k in (
                        "db_type",
                        "host",
                        "port",
                        "user",
                        "database",
                        "service_name",
                    )
                ),
            ).fetchone()
            if duplicate:
                raise Conflict("该连接已有数据源，请在台账列表中补充已有记录")
        stored["connection_mode"] = stored.get("connection_mode") or (
            "jdbc" if stored["db_type"] == "sqlserver_jdbc" else "odbc"
        )
        stored["updated_at"] = now()
        configuration_id = str(metadata.get("configuration_id") or "CI-" + iid).strip()
        if not configuration_id:
            raise ValueError("配置项编号不能为空")
        metadata["configuration_id"] = configuration_id
        columns = {row["name"] for row in db.execute("PRAGMA table_info(instances)")}
        values = {key: value for key, value in stored.items() if key in columns}
        values["tags"] = json.dumps(values.get("tags", []), ensure_ascii=False)
        names = list(values)
        quoted = ",".join('"' + name + '"' for name in names)
        marks = ",".join("?" for _ in names)
        db.execute(
            f"INSERT {'OR REPLACE ' if old else ''}INTO instances ({quoted}) VALUES ({marks})",
            list(values.values()),
        )
        version = old["version"] + 1 if old else 1
        try:
            db.execute(
                "INSERT INTO operations_inventory VALUES(?,?,?,?,?) ON CONFLICT(instance_id) "
                "DO UPDATE SET configuration_id=excluded.configuration_id,version=excluded.version,payload=excluded.payload",
                (
                    iid,
                    actor.tenant_id,
                    configuration_id,
                    version,
                    self.seal(metadata),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise Conflict("配置项编号已存在") from error
        audit_source = {k: v for k, v in stored.items() if k not in SECRETS}
        audit = {
            "before": old,
            "after": {"source": audit_source, "metadata": metadata},
            "credential_fields_updated": sorted(
                SECRETS & {k for k, v in source_updates.items() if v}
            ),
        }
        db.execute(
            "INSERT INTO operations_inventory_history(instance_id,tenant,actor,action,created,payload) VALUES(?,?,?,?,?,?)",
            (
                iid,
                actor.tenant_id,
                actor.id,
                "update" if old else "create",
                now(),
                self.seal(audit),
            ),
        )
        return self._record(db, iid)

    def delete(self, actor, iid, revision):
        with self.connection(write=True) as db:
            old = self._record(db, iid)
            if revision != old["revision"]:
                raise Conflict("台账或数据源已被修改，请刷新后删除")
            db.execute("DELETE FROM instances WHERE id=?", (iid,))
            db.execute("DELETE FROM operations_inventory WHERE instance_id=?", (iid,))
            db.execute(
                "INSERT INTO operations_inventory_history(instance_id,tenant,actor,action,created,payload) VALUES(?,?,?,?,?,?)",
                (
                    iid,
                    actor.tenant_id,
                    actor.id,
                    "delete",
                    now(),
                    self.seal({"before": old}),
                ),
            )
        self._refresh()

    def history(self, actor, ids=None):
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM operations_inventory_history WHERE tenant=? ORDER BY id DESC",
                (actor.tenant_id,),
            ).fetchall()
            return [
                dict(
                    id=row["id"],
                    instance_id=row["instance_id"],
                    actor=row["actor"],
                    action=row["action"],
                    created=row["created"],
                    details=self.open(row["payload"]),
                )
                for row in rows
                if (
                    row["instance_id"] in ids
                    if ids is not None
                    else actor.is_admin or row["actor"] == actor.id
                )
            ]


def report_overrides(engine):
    """只读读取既有台账，供巡检报告合并核心属性。"""
    from modules.core import paths
    from modules.driver_registry import db_family

    path = paths.PRO_DATA_DIR / "instances.db"
    if not path.is_file() or not paths.DB_KEY_PATH.is_file():
        return {}
    iid = (getattr(engine, "context", {}) or {}).get("_instance_id")
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='operations_inventory'"
        ).fetchone():
            return {}
        if not iid:
            rows = db.execute(
                'SELECT id,db_type,"database" FROM instances WHERE host=? AND port=?',
                (getattr(engine, "host", ""), getattr(engine, "port", 0)),
            ).fetchall()
            matches = [
                r
                for r in rows
                if db_family(r["db_type"]) == db_family(getattr(engine, "db_type", ""))
                and (
                    not getattr(engine, "database", None)
                    or r["database"] == engine.database
                )
            ]
            if len(matches) != 1:
                return {}
            iid = matches[0]["id"]
        row = db.execute(
            "SELECT payload FROM operations_inventory WHERE instance_id=?", (iid,)
        ).fetchone()
    if row is None:
        return {}
    metadata = json.loads(_get_fernet().decrypt(row["payload"]))
    aliases = {
        "file_size": "db_size",
        "disaster_recovery": "dr_status",
        "cluster_attributes": "cluster_attrs",
    }
    result = {
        aliases.get(k, k): v
        for k, v in metadata.items()
        if k in ASSET_FIELDS and v not in (None, "")
    }
    if metadata.get("version") or metadata.get("patch_level"):
        result["version_patch"] = " / ".join(
            str(v) for v in (metadata.get("version"), metadata.get("patch_level")) if v
        )
    return result
