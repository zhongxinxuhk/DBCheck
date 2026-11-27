# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""数据库类型元数据注册表（真插件化基础设施）。

把"内置类型"与"插件类型"统一为 :class:`DBTypeMeta`，供 ``web_ui`` /
``config_baseline`` 等主程序模块进行"读元数据 + 查表"，从而消除 ``elif`` 链与
前端硬编码字典。本模块不依赖 ``web_ui``（避免循环导入），仅按需 ``import plugin_loader``。
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import json
import os
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from modules.core.paths import BUILTIN_TYPES_JSON
_BUILTIN_TYPES_JSON = str(BUILTIN_TYPES_JSON)

# 连接测试注册表：db_type -> handler(data, flavor) -> dict
#   flavor: 'regular' 对应 /api/test_db，'pro' 对应 /api/pro/datasources/test-connection
CONNECTION_TESTERS: Dict[str, Callable] = {}
# 基线检查注册表：db_type -> handler(conn) -> report | None
BASELINE_CHECKERS: Dict[str, Callable] = {}

# 简单的进程内缓存（仅在插件启用/禁用时失效）。避免每次请求都扫盘。
_ALL_CACHE: Optional[List["DBTypeMeta"]] = None


@dataclass
class DBTypeMeta:
    """统一的数据库类型元数据。内置类型与插件类型共用同一份形状。"""

    db_type: str
    label: str = ""
    port: int = 3306
    user: str = "root"
    default_database: str = ""
    icon: str = ""
    emoji: str = "📊"
    description: str = ""
    compat_tag: str = ""
    protocol: str = "other"
    sql_editor: bool = False
    show_database_field: bool = True
    is_plugin: bool = False
    connect_test: Optional[str] = None
    baseline_reuse: Optional[str] = None
    baseline_check: Optional[str] = None
    path: str = ""

    @classmethod
    def from_builtin(cls, d: dict) -> "DBTypeMeta":
        bl = d.get("baseline") or {}
        return cls(
            db_type=d["db_type"],
            label=d.get("label", d["db_type"]),
            port=d.get("port", 3306),
            user=d.get("user", "root"),
            default_database=d.get("default_database", ""),
            icon=d.get("icon", ""),
            emoji=d.get("emoji", "📊"),
            description=d.get("description", ""),
            compat_tag=d.get("compat_tag", ""),
            protocol=d.get("protocol", "other"),
            sql_editor=bool(d.get("sql_editor", False)),
            show_database_field=bool(d.get("show_database_field", True)),
            is_plugin=False,
            connect_test=d.get("connect_test"),
            baseline_reuse=bl.get("reuse") if bl else None,
            baseline_check=bl.get("check") if bl else None,
        )

    @classmethod
    def from_plugin(cls, d: dict) -> "DBTypeMeta":
        bl = d.get("baseline") or {}
        _db_type = d.get("db_type") or d.get("id")
        return cls(
            db_type=_db_type,
            label=d.get("name", _db_type or ""),
            port=d.get("default_port", 3306),
            user=d.get("default_user", "root"),
            default_database=d.get("default_database", ""),
            icon=d.get("icon") or ("/plugin-logo/" + str(_db_type)),
            emoji=d.get("emoji", "🧩"),
            description=d.get("description", ""),
            compat_tag=d.get("compat_tag", ""),
            protocol=d.get("protocol", "other"),
            sql_editor=bool(d.get("sql_editor", False)),
            show_database_field=bool(d.get("show_database_field", True)),
            is_plugin=True,
            connect_test=d.get("connect_test"),
            baseline_reuse=bl.get("reuse") if bl else None,
            baseline_check=bl.get("check") if bl else None,
            path=d.get("path", ""),
        )


def register_connection_tester(db_type: str, fn: Callable) -> None:
    """注册某 db_type 的连接测试处理器（flavor 感知）。"""
    CONNECTION_TESTERS[db_type] = fn


def register_baseline_checker(db_type: str, fn: Callable) -> None:
    """注册某 db_type 的基线检查函数。"""
    BASELINE_CHECKERS[db_type] = fn


def load_builtin_types() -> List[DBTypeMeta]:
    try:
        with open(_BUILTIN_TYPES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # pragma: no cover - 防御性
        print(f"[DBTypeRegistry] 读取 builtin_types.json 失败: {e}")
        return []
    return [DBTypeMeta.from_builtin(item) for item in data.get("types", [])]


def load_plugin_types() -> List[DBTypeMeta]:
    out: List[DBTypeMeta] = []
    try:
        from modules.pluginkit.loader import discover_plugins

        for p in discover_plugins():
            if p.get("enabled") and p.get("db_type"):
                out.append(DBTypeMeta.from_plugin(p))
    except Exception as e:  # pragma: no cover - 防御性
        print(f"[DBTypeRegistry] 加载插件类型失败: {e}")
    return out


def load_all_db_types(force: bool = False) -> List[DBTypeMeta]:
    """合并内置类型 + 插件类型。带进程内缓存（force=True 时失效）。"""
    global _ALL_CACHE
    if _ALL_CACHE is not None and not force:
        return _ALL_CACHE
    _ALL_CACHE = load_builtin_types() + load_plugin_types()
    return _ALL_CACHE


def invalidate_cache() -> None:
    """在插件启用/禁用后调用，使缓存失效。"""
    global _ALL_CACHE
    _ALL_CACHE = None


def inspection_type(source):
    """Resolve the inspection driver from a saved or manually entered connection."""
    from modules.core import paths

    kind = (source.get('db_type') or 'mysql').strip().lower()
    kind = {'dm8': 'dm', 'oracle_full': 'oracle', 'mssql': 'sqlserver'}.get(kind, kind)
    if kind == 'oracle' and (
            paths.PORTABLE_ROOT is not None
            or str(source.get('jdbc_url') or '').strip().lower().startswith('jdbc:')):
        return 'oracle_jdbc'
    if kind == 'sqlserver':
        from modules.db_types.connection_profile import sqlserver_mode

        if sqlserver_mode(dict(source, db_type=kind)) in ('jdbc', 'auto'):
            return 'sqlserver_jdbc'
    return kind


def get_db_meta(db_type: str) -> Optional[DBTypeMeta]:
    """按 db_type 查统一元数据。内置类型优先（排在插件之前）。"""
    for m in load_all_db_types():
        if m.db_type == db_type:
            return m
    return None


def resolve_connection_tester(meta: Optional[DBTypeMeta]):
    """根据插件 meta 的 connect_test 字段解析出注册表中的 tester。

    支持字符串形式（如 ``"mysql"`` / ``"pg"``），复用内置协议测试器。
    """
    if not meta or not meta.connect_test:
        return None
    ct = meta.connect_test
    if isinstance(ct, str):
        return CONNECTION_TESTERS.get(ct) or CONNECTION_TESTERS.get(meta.db_type)
    return None


def resolve_baseline_checker(meta: Optional[DBTypeMeta]) -> Optional[Tuple[str, object]]:
    """解析基线检查器。

    返回 ``("plugin", func_name)`` 或 ``("builtin", fn)``；均无法解析返回 ``None``。
    """
    if not meta:
        return None
    if meta.baseline_check:
        return ("plugin", meta.baseline_check)
    if meta.baseline_reuse and meta.baseline_reuse in BASELINE_CHECKERS:
        return ("builtin", BASELINE_CHECKERS[meta.baseline_reuse])
    if meta.db_type in BASELINE_CHECKERS:
        return ("builtin", BASELINE_CHECKERS[meta.db_type])
    return None
