# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""巡检结果到资产台账的自动同步。

只同步可由数据库/目标主机稳定探测的字段。备份策略、容灾状态、集群属性
等容易受查询口径影响的字段只写入实测快照，供台账页面比对，不直接覆盖
人工登记值。静态资产字段（责任人、位置、项目归属等）永远不自动修改。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

# 自动应用白名单。顺序仅用于展示和稳定的测试断言。
AUTO_FIELDS = {
    "inventory_date",
    "instance_name",
    "charset",
    "file_size",
    "archive_path",
    "archive_mode",
    "storage_mode",
    "version",
    "patch_level",
    "os_version",
    "host_model",
}

# 仅记录实测值的字段，避免把展示型汇总文本直接写进人工台账。
OBSERVED_ONLY_FIELDS = {
    "backup_policy",
    "disaster_recovery",
    "cluster_attributes",
}

_PLACEHOLDERS = {
    "",
    "-",
    "—",
    "…",
    "未采集",
    "未登记",
    "未读取",
    "未知",
    "unknown",
    "n/a",
    "none",
    "null",
}

# 专项实测只做对照，不写回人工台账；限制单次载荷大小，避免巡检结果撑爆台账库。
_SPECIAL_LIMIT = 300
_TS_NAME_KEYS = (
    "tablespace_name", "tablespace", "ts_name", "space_name", "logical_name", "name",
)
_TS_SIZE_KEYS = (
    "total_mb", "size_mb", "total_size_mb", "allocated_mb", "total_gb",
    "size_gb", "size", "total_size",
)
_TS_FREE_KEYS = ("free_mb", "free_size_mb", "free_gb", "free_size", "free")
_TS_USED_KEYS = (
    "used_pct", "usage_pct", "used_percent", "used_percentage",
    "percent_used", "usage_percent",
)
_TS_STATUS_KEYS = ("status", "state", "status$", "tablespace_status")
_ACC_NAME_KEYS = (
    "username", "user_name", "login_name", "usename", "account", "user", "name",
)
_ACC_EXPIRY_KEYS = (
    "expiry_date", "expiration_date", "password_expiry_date", "valuntil", "expires",
)
_ACC_CREATED_KEYS = ("created_date", "create_date", "created", "ctime")
_ACC_TS_KEYS = ("default_tablespace", "default_tablespace_name", "tablespace")
_ACC_PROFILE_KEYS = ("profile", "user_profile")
_ACC_TYPE_KEYS = ("type_desc", "account_type", "user_type")


def _lookup(row):
    return {str(key).strip().lower(): value for key, value in row.items()}


def _pick_text(data, keys):
    for key in keys:
        if key in data:
            value = _text(data[key])
            if value:
                return value
    return ""


def _measure(data, keys):
    for key in keys:
        if key not in data:
            continue
        value = _text(data[key])
        if not value:
            continue
        suffix = " GB" if key.endswith("_gb") else " MB" if key.endswith("_mb") else ""
        if suffix and not re.search(r"[A-Za-z%]", value):
            value += suffix
        return value
    return ""


def _percent(data):
    value = _pick_text(data, _TS_USED_KEYS)
    if value and not value.endswith("%"):
        value += "%"
    return value


def _truthy(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "t"}


def _normalize_tablespace(row):
    data = _lookup(row)
    name = _pick_text(data, _TS_NAME_KEYS)
    if not name:
        return None
    item = {"name": name}
    size = _measure(data, _TS_SIZE_KEYS)
    free = _measure(data, _TS_FREE_KEYS)
    used = _percent(data)
    status = _pick_text(data, _TS_STATUS_KEYS)
    for key, value in (("size", size), ("free", free),
                       ("used_pct", used), ("status", status)):
        if value:
            item[key] = value
    if not any(key in item for key in ("size", "free", "used_pct", "status")):
        return None
    return item


def _normalize_account(row):
    data = _lookup(row)
    username = _pick_text(data, _ACC_NAME_KEYS)
    if not username:
        return None
    item = {"username": username}
    status_parts = []
    status = _pick_text(data, ("account_status", "status", "state"))
    if status:
        status_parts.append(status)
    for key, label in (("is_disabled", "禁用"), ("account_locked", "锁定"),
                       ("password_expired", "密码过期"), ("usesuper", "超级用户"),
                       ("usecreatedb", "可建库")):
        if key in data:
            if _truthy(data[key]):
                status_parts.append(label)
            elif key == "is_disabled":
                status_parts.append("启用")
    if status_parts:
        item["status"] = " / ".join(dict.fromkeys(status_parts))
    expiry = _pick_text(data, _ACC_EXPIRY_KEYS)
    created = _pick_text(data, _ACC_CREATED_KEYS)
    default_ts = _pick_text(data, _ACC_TS_KEYS)
    profile = _pick_text(data, _ACC_PROFILE_KEYS)
    account_type = _pick_text(data, _ACC_TYPE_KEYS)
    for key, value in (("expiry_date", expiry), ("created_date", created),
                       ("default_tablespace", default_ts), ("profile", profile),
                       ("account_type", account_type)):
        if value:
            item[key] = value
    return item


def _tablespace_score(key):
    # W11 的 DM 查询同时给出总容量、空闲空间和使用率，比只有容量的 W06 更适合对照。
    if "w11_tablespace_usage" in key:
        return 35
    if "w06_tablespace_usage" in key or "tablespace_usage" in key:
        return 30
    if "m06_tablespace_detail" in key:
        return 20
    if "tablespace" in key or "tablespaces" in key:
        return 12
    if "temp_space" in key:
        return 3
    return 0


def _account_score(key):
    if "w17" in key and ("user" in key or "login" in key):
        return 30
    if key in {"dba_users", "user_list", "mysql_users", "pg_users", "sql_logins"}:
        return 25
    if "users" in key or "logins" in key or "accounts" in key:
        return 12
    if "account" in key:
        return 8
    if any(word in key for word in ("session", "conn", "priv", "role")):
        return -20
    return 0


def _collect_section(context, normalizer, scorer, name_key):
    candidates = []
    query_status = context.get('_query_status') or {}
    for key, rows in (context or {}).items():
        if not isinstance(key, str) or not isinstance(rows, list):
            continue
        score = scorer(key.lower())
        if score <= 0:
            continue
        status = query_status.get(key) or {}
        if status.get('status') in {'error', 'not_applicable'}:
            continue
        if not rows and query_status and key not in query_status:
            continue
        total = len(rows)
        items = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = normalizer(row)
            if not item:
                continue
            marker = item.get(name_key, "").casefold()
            if marker and marker in seen:
                continue
            seen.add(marker)
            items.append(item)
        candidates.append((bool(items), score, key, items, total))
    if not candidates:
        return {
            "status": "not_collected", "source": "", "rows": [], "total": 0,
            "truncated": False,
        }
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    has_rows, _score, key, items, total = candidates[0]
    if total == 0:
        status = "not_detected"
    elif not has_rows:
        status = "unrecognized"
    else:
        status = "collected"
    return {
        "status": status,
        "source": key,
        "rows": items[:_SPECIAL_LIMIT],
        "total": total,
        "truncated": len(items) > _SPECIAL_LIMIT,
    }


def build_specialized_observation(context):
    """提取表空间、账号等专项实测快照，只用于台账差异对照。

    这里刻意采用列名归一化而不是直接把原始行写入台账库：
    - 只保留名称、容量、状态等展示字段，不保存口令散列、权限明细；
    - 查询结果为空与没有执行对应查询分别标记为 not_detected / not_collected；
    - 不修改 operations_inventory.metadata.tables。
    """
    if not isinstance(context, dict):
        return {}
    sections = {
        "tablespaces": _collect_section(
            context, _normalize_tablespace, _tablespace_score, "name"),
        "accounts": _collect_section(
            context, _normalize_account, _account_score, "username"),
    }
    if not any(section["status"] != "not_collected" for section in sections.values()):
        return {}
    return sections


def _text(value) -> str:
    if value is None:
        return ""
    value = str(value).strip()
    return "" if value.lower() in _PLACEHOLDERS else value


def _version_fields(value) -> tuple[str, str]:
    value = _text(value)
    if not value:
        return "", ""
    # basic_info 在补丁级别不可读时会附中文说明；说明不是版本号。
    value = re.sub(r"[（(]\s*补丁级别未读取到\s*[）)]", "", value).strip()
    value = re.sub(r"[；;]\s*补丁[:：]\s*未读取到.*$", "", value).strip()
    value = re.sub(r"[（(]\s*patch level not (?:read|available)\s*[）)]", "", value,
                   flags=re.IGNORECASE).strip()
    parts = [part.strip() for part in re.split(r"\s*/\s*", value) if part.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return value, ""


def _target_server(system_info) -> bool:
    return bool(isinstance(system_info, dict) and system_info.get("_is_target_server"))


def build_candidates(basic_info, system_info=None, observed_at=None):
    """把 basic_info 拆成“可自动应用字段”和“仅实测字段”。

    - 目标主机信息只在 SSH 采集成功且标记为目标服务器时采信；
    - 所有“未采集/未登记”等占位值都不会进入台账；
    - 返回值中的 ``auto_fields`` 是唯一会被自动写入 metadata 的内容。
    """
    basic = dict(basic_info or {})
    system_info = system_info or {}
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    # 盘点日期按巡检所在机器的本地日期记，避免 UTC 时间跨日。
    inventory_date = date.today().isoformat()

    auto_fields = {}
    observed_fields = {}

    def put(key, value):
        value = _text(value)
        if value:
            observed_fields[key] = value
            if key in AUTO_FIELDS:
                auto_fields[key] = value

    put("inventory_date", inventory_date)
    put("instance_name", basic.get("instance_name"))
    put("charset", basic.get("charset"))
    put("file_size", basic.get("db_size"))
    put("archive_path", basic.get("archive_path"))
    put("archive_mode", basic.get("archive_mode"))
    put("storage_mode", basic.get("storage_mode"))

    version, patch_level = _version_fields(basic.get("version_patch"))
    put("version", version)
    put("patch_level", patch_level)

    # 未配置 SSH 时 basic_info 会回退到巡检工具本机平台信息，不能写进目标台账。
    if _target_server(system_info):
        put("os_version", basic.get("os_version") or system_info.get("os_version"))
        put("host_model", basic.get("host_model") or system_info.get("host_model"))

    # 这些字段允许展示实测差异，但不自动覆盖台账。
    put("backup_policy", basic.get("backup_policy"))
    put("disaster_recovery", basic.get("dr_status"))
    put("cluster_attributes", basic.get("cluster_attrs"))

    return {
        "auto_fields": auto_fields,
        "observed_fields": observed_fields,
        "basic_info": {str(k): _text(v) for k, v in basic.items() if _text(v)},
        "target_server": _target_server(system_info),
        "observed_at": observed_at,
    }


def apply_inspection_observation(*, instance_id, task_id, tenant_id, user_id,
                                 basic_info, system_info=None, report_path="",
                                 observed_tables=None, inventory=None):
    """巡检成功后调用；失败返回状态字典，不向上抛出业务异常。

    调用方负责记录日志。重复 task_id 不会重复写台账，也不会重复改版本。
    """
    if not instance_id or not task_id:
        return {"status": "skipped", "reason": "缺少数据源或巡检任务编号"}
    if user_id in (None, ""):
        return {"status": "skipped", "reason": "巡检任务没有归属用户"}
    try:
        from modules.access import get_principal
        actor = get_principal(user_id)
        if actor is None:
            return {"status": "skipped", "reason": "巡检任务归属用户已失效"}
        if tenant_id not in (None, "") and int(tenant_id) != int(actor.tenant_id):
            return {"status": "skipped", "reason": "巡检任务租户与台账不一致"}
        if not (actor.is_admin or "operator" in (actor.roles or [])):
            return {"status": "skipped", "reason": "任务归属用户没有台账写权限"}

        candidates = build_candidates(basic_info, system_info)
        observed_tables = observed_tables or {}
        if (not candidates["auto_fields"] and not candidates["observed_fields"]
                and not observed_tables):
            return {"status": "skipped", "reason": "本次巡检没有可记录的台账实测值"}

        if inventory is None:
            from modules.operations.inventory import Inventory
            from modules.pro import get_instance_manager
            inventory = Inventory(get_instance_manager())
        return inventory.record_inspection(
            actor,
            instance_id,
            task_id,
            report_path or "",
            candidates["auto_fields"],
            candidates["observed_fields"],
            basic_info=candidates["basic_info"],
            observed_at=candidates["observed_at"],
            observed_tables=observed_tables,
        )
    except LookupError as exc:
        # 数据源可能只是连接实例，尚未建立台账；这不是同步故障。
        return {"status": "skipped", "reason": str(exc)[:300]}
    except PermissionError as exc:
        return {"status": "skipped", "reason": str(exc)[:300]}
    except Exception as exc:  # 同步失败不能把已完成的巡检改成失败
        return {"status": "failed", "reason": str(exc)[:300]}
