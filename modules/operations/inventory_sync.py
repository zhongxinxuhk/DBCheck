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
                                 inventory=None):
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
        if not candidates["auto_fields"] and not candidates["observed_fields"]:
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
        )
    except LookupError as exc:
        # 数据源可能只是连接实例，尚未建立台账；这不是同步故障。
        return {"status": "skipped", "reason": str(exc)[:300]}
    except PermissionError as exc:
        return {"status": "skipped", "reason": str(exc)[:300]}
    except Exception as exc:  # 同步失败不能把已完成的巡检改成失败
        return {"status": "failed", "reason": str(exc)[:300]}
