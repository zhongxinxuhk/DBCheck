# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""数据库运维中台的资产台账与多数据源巡检。"""


import copy
import json
import sqlite3
import uuid
from contextlib import contextmanager, closing
from datetime import datetime, timezone

from modules.db_types.connection_profile import BOOLEAN_FIELDS, normalize_source_fields
from modules.pro.instance_manager import (
    DatabaseInstance,
    InstanceManager,
    _decrypt_pwd,
    _decrypt_jdbc_url,
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
# 人工确认补录时允许映射的台账字段。只做“补缺”，不负责覆盖。
SPECIAL_APPLY_FIELDS = {
    "tablespaces": "name",
    "accounts": "username",
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
    "gbase8a_allow_plaintext": "GBase 8a 允许明文直连",
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
# jdbc_url 可能内嵌账号口令，落盘加密；但它不是密码字段，解密函数不同。
ENCRYPTED_FIELDS = SECRETS | {"jdbc_url"}


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
                CREATE TABLE IF NOT EXISTS operations_inventory_observed (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, instance_id TEXT NOT NULL,
                    tenant INTEGER NOT NULL, task_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL, report_path TEXT NOT NULL DEFAULT '',
                    payload BLOB NOT NULL,
                    UNIQUE(tenant, instance_id, task_id)
                );
                CREATE INDEX IF NOT EXISTS idx_inventory_observed_latest
                    ON operations_inventory_observed(instance_id, observed_at DESC, id DESC);
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
            source["jdbc_url"] = _decrypt_jdbc_url(source.get("jdbc_url") or "")
        else:
            for key in ENCRYPTED_FIELDS:
                source.pop(key, None)
        observation = None
        try:
            obs = db.execute(
                "SELECT * FROM operations_inventory_observed "
                "WHERE instance_id=? ORDER BY observed_at DESC, id DESC LIMIT 1",
                (iid,),
            ).fetchone()
            if obs:
                observation = {
                    **self.open(obs["payload"]),
                    "task_id": obs["task_id"],
                    "observed_at": obs["observed_at"],
                    "report_path": obs["report_path"] or "",
                }
        except sqlite3.Error:
            observation = None
        return {
            "id": iid,
            "revision": source["updated_at"] + "/" + str(version),
            "source": source,
            "metadata": metadata,
            "configuration_id": metadata.get("configuration_id") or "CI-" + iid,
            "version": version,
            "observation": observation,
        }

    def get(self, iid, decrypt=False):
        with self.connection() as db:
            return self._record(db, iid, decrypt)

    def list(self, ids, decrypt=False):
        with self.connection() as db:
            return [self._record(db, iid, decrypt) for iid in ids]

    def save(self, actor, data, iid=None, new_id=None):
        with self.connection(write=True) as db:
            record = self._save(actor, data, iid, new_id, db)
        return record

    def record_inspection(self, actor, iid, task_id, report_path, auto_fields,
                          observed_fields, basic_info=None, observed_at=None,
                          observed_tables=None):
        """保存巡检实测快照，并仅自动应用服务端白名单字段。

        该方法供巡检完成钩子调用，不经过 Web 路由；仍按台账租户和
        revision 做隔离/并发校验。task_id 唯一，重复回调不会重复改台账。
        """
        auto_fields = dict(auto_fields or {})
        observed_fields = dict(observed_fields or {})
        invalid = (set(auto_fields) | set(observed_fields)) - ASSET_FIELDS.keys()
        if invalid:
            raise ValueError("巡检台账字段无效: " + ",".join(sorted(invalid)))
        if not task_id:
            raise ValueError("巡检任务编号不能为空")
        observed_tables = dict(observed_tables or {})
        if set(observed_tables) - {"tablespaces", "accounts"}:
            raise ValueError("专项实测快照字段无效")
        for section in observed_tables.values():
            if not isinstance(section, dict):
                raise ValueError("专项实测快照格式无效")
            rows = section.get("rows", [])
            if not isinstance(rows, list) or len(rows) > 500:
                raise ValueError("专项实测快照最多500行")
            if any(not isinstance(row, dict) or any(
                    isinstance(value, (dict, list)) or len(str(value)) > 512
                    for value in row.values()) for row in rows):
                raise ValueError("专项实测快照单元格格式无效")
        try:
            encoded_snapshot = json.dumps(observed_tables, ensure_ascii=False).encode()
        except (TypeError, ValueError) as exc:
            raise ValueError("专项实测快照格式无效") from exc
        if len(encoded_snapshot) > 262144:
            raise ValueError("专项实测快照过大")
        observed_at = observed_at or now()
        with self.connection(write=True) as db:
            owner = db.execute(
                "SELECT tenant FROM operations_inventory WHERE instance_id=?",
                (iid,),
            ).fetchone()
            if owner is None:
                # 数据源可能早于台账模块存在；首次巡检时补建空台账，
                # 后续自动同步即可正常留痕。若实例本身不存在则 _record 抛错。
                current = self._record(db, iid)
                self._save(
                    actor,
                    {"revision": current["revision"], "metadata": {}},
                    iid=iid,
                    db=db,
                )
                owner = db.execute(
                    "SELECT tenant FROM operations_inventory WHERE instance_id=?",
                    (iid,),
                ).fetchone()
            if owner["tenant"] != actor.tenant_id:
                raise PermissionError("无权更新该台账")
            existing = db.execute(
                "SELECT payload FROM operations_inventory_observed "
                "WHERE tenant=? AND instance_id=? AND task_id=?",
                (actor.tenant_id, iid, task_id),
            ).fetchone()
            if existing:
                payload = self.open(existing["payload"])
                return {
                    "status": "already_applied",
                    "task_id": task_id,
                    "changed": payload.get("applied", []),
                    "observed_at": payload.get("observed_at", observed_at),
                }
            current = self._record(db, iid)
            changes = {}
            for key, value in auto_fields.items():
                if str(current["metadata"].get(key, "") or "") != str(value or ""):
                    changes[key] = value
            audit_extra = {
                "source": "inspection",
                "task_id": task_id,
                "report_path": report_path or "",
                "observed_at": observed_at,
                "auto_fields": sorted(changes),
            }
            if changes:
                self._save(
                    actor,
                    {"revision": current["revision"], "metadata": changes},
                    iid=iid,
                    db=db,
                    action="inspection_auto_update",
                    audit_extra=audit_extra,
                )
            payload = {
                "observed_at": observed_at,
                "auto_fields": auto_fields,
                "observed_fields": observed_fields,
                "basic_info": basic_info or {},
                "specialized": observed_tables,
                "applied": sorted(changes),
                "unchanged": sorted(set(auto_fields) - set(changes)),
            }
            db.execute(
                "INSERT INTO operations_inventory_observed "
                "(instance_id,tenant,task_id,observed_at,report_path,payload) "
                "VALUES(?,?,?,?,?,?)",
                (iid, actor.tenant_id, task_id, observed_at, report_path or "",
                 self.seal(payload)),
            )
            self._record(db, iid)
        return {
            "status": "applied" if changes else "unchanged",
            "task_id": task_id,
            "changed": sorted(changes),
            "observed_at": observed_at,
        }

    def apply_observation(self, actor, iid, data):
        """用户确认后把专项实测补录到台账，绝不覆盖已有手工值。

        补录规则：
        - 未登记的表空间/账号：新增行，只写名称和可安全映射的字段；
        - 已登记表空间且 size 为空：仅补全 size；
        - 已有手工值、账号已有行：一律不覆盖；
        - task_id 必须是当前最新实测，revision 必须匹配，防止过期页面回填。
        """
        if not (actor.is_admin or "operator" in (actor.roles or [])):
            raise PermissionError("维护台账需要管理员或运维角色")
        data = data or {}
        task_id = str(data.get("task_id") or "").strip()
        revision = data.get("revision")
        sections = data.get("sections")
        if not task_id:
            raise ValueError("缺少巡检任务编号")
        if not revision:
            raise ValueError("缺少台账版本，请刷新后重试")
        if not isinstance(sections, dict) or not sections:
            raise ValueError("请选择要补录的专项台账")
        if set(sections) - set(SPECIAL_APPLY_FIELDS):
            raise ValueError("专项台账类型无效")
        selected = {}
        for key, names in sections.items():
            if isinstance(names, str):
                names = [names]
            if not isinstance(names, list) or not names or len(names) > 500:
                raise ValueError("请选择要补录的台账行")
            clean = []
            for name in names:
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("台账行名称无效")
                clean.append(name.strip())
            selected[key] = list(dict.fromkeys(clean))

        changed = False
        counts = {}
        with self.connection(write=True) as db:
            owner = db.execute(
                "SELECT tenant FROM operations_inventory WHERE instance_id=?", (iid,)
            ).fetchone()
            if owner is None:
                raise LookupError("台账不存在")
            if owner["tenant"] != actor.tenant_id:
                raise PermissionError("无权更新该台账")
            observation = db.execute(
                "SELECT * FROM operations_inventory_observed "
                "WHERE instance_id=? ORDER BY observed_at DESC, id DESC LIMIT 1",
                (iid,),
            ).fetchone()
            if observation is None:
                raise LookupError("该数据源还没有巡检实测快照")
            if observation["task_id"] != task_id:
                raise Conflict("巡检实测快照已更新，请刷新后重试")
            payload = self.open(observation["payload"])
            available = payload.get("specialized") or {}
            current = self._record(db, iid)
            if revision != current["revision"]:
                raise Conflict("台账已被修改，请刷新后重试")
            tables = copy.deepcopy(current["metadata"].get("tables") or {})
            if not isinstance(tables, dict):
                raise ValueError("专项台账格式无效")

            for key, names in selected.items():
                name_field = SPECIAL_APPLY_FIELDS[key]
                section = available.get(key) or {}
                if not isinstance(section, dict) or section.get("status") != "collected":
                    raise ValueError("该专项没有可补录的实测结果")
                observed_rows = {}
                for row in section.get("rows") or []:
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get(name_field) or "").strip()
                    if name:
                        observed_rows.setdefault(name.casefold(), (name, row))
                rows = tables.get(key)
                if rows is None:
                    rows = []
                    tables[key] = rows
                if not isinstance(rows, list) or len(rows) > 10000:
                    raise ValueError("专项台账行数超出限制")
                existing = {}
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get(name_field) or "").strip()
                    if name:
                        existing.setdefault(name.casefold(), row)
                stats = {
                    "selected": len(names), "added": 0, "filled": 0,
                    "unchanged": 0, "protected": 0, "missing": 0,
                }
                for requested in names:
                    matched = observed_rows.get(requested.casefold())
                    if not matched:
                        stats["missing"] += 1
                        continue
                    name, observed = matched
                    row = existing.get(requested.casefold())
                    if row is not None:
                        if key == "tablespaces":
                            observed_size = str(observed.get("size") or "").strip()
                            current_size = str(row.get("size") or "").strip()
                            if observed_size and not current_size:
                                row["size"] = observed_size
                                stats["filled"] += 1
                                changed = True
                            elif observed_size and current_size != observed_size:
                                stats["protected"] += 1
                            else:
                                stats["unchanged"] += 1
                        else:
                            stats["unchanged"] += 1
                        continue
                    new_row = {name_field: name}
                    if key == "tablespaces":
                        observed_size = str(observed.get("size") or "").strip()
                        if observed_size:
                            new_row["size"] = observed_size
                    rows.append(new_row)
                    existing[requested.casefold()] = new_row
                    stats["added"] += 1
                    changed = True
                counts[key] = stats

            if changed:
                self._save(
                    actor,
                    {"revision": revision, "metadata": {"tables": tables}},
                    iid=iid,
                    db=db,
                    action="inspection_observation_apply",
                    audit_extra={
                        "source": "inspection_observation",
                        "task_id": task_id,
                        "report_path": payload.get("report_path") or observation["report_path"] or "",
                        "sections": counts,
                    },
                )
            record = self._record(db, iid)
        return {
            "status": "applied" if changed else "unchanged",
            "task_id": task_id,
            "sections": counts,
            "record": record,
        }

    def _save(self, actor, data, iid=None, new_id=None, db=None, action=None,
              audit_extra=None):
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
                id=iid, name="", db_type="", host="", port=0, user="", connection_mode=""
            ).to_dict()
        for key, value in source_updates.items():
            if key not in BOOLEAN_FIELDS | {"tags", "port", "ssh_port"} and not isinstance(value, str):
                raise ValueError("连接文本字段格式无效")
            if len(str(value)) > 32767:
                raise ValueError("连接字段最多32767字符")
        source_patch = {key: value for key, value in source_updates.items()
                        if key not in SECRETS or value}
        stored.update(InstanceManager.prepare_source_write(source_patch))
        for key in ("name", "db_type", "host"):
            if not isinstance(stored.get(key), str) or not stored[key].strip():
                raise ValueError("资产名称、数据库类型和主机地址必填")
            stored[key] = stored[key].strip()
        stored = normalize_source_fields(stored)
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
        if not old or source_updates:
            stored["updated_at"] = now()
        configuration_id = str(metadata.get("configuration_id") or "CI-" + iid).strip()
        if not configuration_id:
            raise ValueError("配置项编号不能为空")
        metadata["configuration_id"] = configuration_id
        if not old or source_updates:
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
        audit_source = {k: v for k, v in stored.items() if k not in ENCRYPTED_FIELDS}
        audit = {
            "before": old,
            "after": {"source": audit_source, "metadata": metadata},
            "credential_fields_updated": sorted(
                ENCRYPTED_FIELDS & {k for k, v in source_updates.items() if v}
            ),
        }
        if audit_extra:
            audit.update(audit_extra)
        db.execute(
            "INSERT INTO operations_inventory_history(instance_id,tenant,actor,action,created,payload) VALUES(?,?,?,?,?,?)",
            (
                iid,
                actor.tenant_id,
                actor.id,
                action or ("update" if old else "create"),
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

    def observation_for_report(self, actor, filename):
        """Resolve an immutable observation by exact report basename, never by latest timestamp."""
        if not actor.is_admin:
            raise PermissionError('按历史文件读取原始观测需要管理员权限')
        with self.connection() as db:
            rows=db.execute('SELECT id,instance_id,task_id,observed_at,report_path FROM operations_inventory_observed WHERE tenant=?',
                            (actor.tenant_id,)).fetchall()
            matches=[row for row in rows if str(row['report_path']).replace('\\','/').split('/')[-1]==filename]
            if len(matches)!=1:
                return None
            row=matches[0]
            payload=db.execute('SELECT payload FROM operations_inventory_observed WHERE id=?',(row['id'],)).fetchone()
            return {**dict(row),'data':self.open(payload['payload'])}


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
