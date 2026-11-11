# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""系统表/视图字段知识库 V2。

只记录系统视图/表的字段信息，根据用户问题动态生成SQL。
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple


_KNOWLEDGE_BASE: Optional[Dict[str, Any]] = None


def _load_knowledge() -> Dict[str, Any]:
    """加载知识库（延迟加载，单例）。"""
    global _KNOWLEDGE_BASE
    if _KNOWLEDGE_BASE is not None:
        return _KNOWLEDGE_BASE

    here = os.path.dirname(os.path.abspath(__file__))
    kb_path = os.path.join(here, "schema_knowledge.json")

    if os.path.exists(kb_path):
        try:
            with open(kb_path, "r", encoding="utf-8") as f:
                _KNOWLEDGE_BASE = json.load(f)
                return _KNOWLEDGE_BASE
        except Exception:
            pass

    _KNOWLEDGE_BASE = {"databases": {}}
    return _KNOWLEDGE_BASE


def get_knowledge() -> Dict[str, Any]:
    """返回当前完整知识库字典（含 databases）。"""
    return _load_knowledge()


def reload_knowledge() -> Dict[str, Any]:
    """强制重新从磁盘加载知识库（写入后调用以刷新缓存）。"""
    global _KNOWLEDGE_BASE
    _KNOWLEDGE_BASE = None
    return _load_knowledge()


def save_knowledge(data: Dict[str, Any]) -> bool:
    """把知识库字典写回磁盘并刷新内存缓存。

    Args:
        data: 完整的 {"description":..., "version":..., "databases": {...}} 结构

    Returns:
        是否保存成功
    """
    global _KNOWLEDGE_BASE
    here = os.path.dirname(os.path.abspath(__file__))
    kb_path = os.path.join(here, "schema_knowledge.json")
    try:
        with open(kb_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _KNOWLEDGE_BASE = data
        return True
    except Exception as e:
        print(f"[schema_knowledge] save failed: {e}")
        return False


def find_relevant_views(db_type: str, goal: str) -> List[Dict[str, Any]]:
    """根据用户目标关键词找到相关的系统视图。

    Returns:
        [{"view": "视图名", "description": "...", "fields": {...}, "keywords": [...]}, ...]
    """
    kb = _load_knowledge()
    db_key = _normalize_db_type(db_type)
    db_info = kb.get("databases", {}).get(db_key, {})
    views = db_info.get("views", {})

    # 从用户问题中提取关键词（更细粒度）
    g = (goal or "").lower()
    # 提取英文单词和中文单字/词
    keywords = []
    # 英文部分
    keywords.extend(re.findall(r'[a-z0-9]+', g))
    # 中文部分：分成单字和双字词
    chinese_chars = re.findall(r'[\u4e00-\u9fff]', g)
    keywords.extend(chinese_chars)
    # 也提取常见的双字词
    chinese_words = re.findall(r'[\u4e00-\u9fff]{2,4}', g)
    keywords.extend(chinese_words)

    scored_views = []
    for view_name, view_info in views.items():
        view_keywords = [k.lower() for k in view_info.get("keywords", [])]
        # 计算匹配分数
        score = 0
        for kw in keywords:
            if kw in view_keywords:
                score += 10
            elif any(vk for vk in view_keywords if kw in vk):
                score += 5
            # 检查字段名是否匹配
            fields = view_info.get("fields", {})
            for fname in fields:
                if kw in fname.lower():
                    score += 2

        if score > 0:
            scored_views.append({
                "view": view_name,
                "description": view_info.get("description", ""),
                "fields": view_info.get("fields", {}),
                "keywords": view_keywords,
                "score": score
            })

    # 按分数排序
    scored_views.sort(key=lambda x: x["score"], reverse=True)
    return scored_views


def _normalize_db_type(db_type: str) -> str:
    """标准化数据库类型名称。"""
    t = (db_type or "").lower()
    t = t.replace("_jdbc", "").replace("_full", "")
    if t in ("oracle",):
        return "oracle"
    if t in ("dm", "dm8"):
        return "dm"
    if t in ("kingbase",):
        return "kingbase"
    if t in ("highgo",):
        return "highgo"
    if t in ("clickhouse",):
        return "clickhouse"
    if t in ("db2",):
        return "db2"
    if t in ("oceanbase",):
        return "oceanbase"
    if t in ("mysql", "tidb", "mariadb"):
        return "mysql"
    if t in ("postgresql", "pg", "ivorysql", "hgdb", "uxdb", "halodb", "vastbase", "kingbase"):
        return "postgresql"
    if t in ("sqlserver",):
        return "sqlserver"
    if t in ("yashandb", "yashan"):
        return "yashandb"
    if t in ("gbase", "gbase8a", "gbase8c"):
        return "gbase"
    if t in ("oceanbase",):
        return "oceanbase"
    if t in ("ivorysql",):
        return "postgresql"  # IvorySQL 兼容 PostgreSQL
    return t


def build_sql_from_fields(
    db_type: str,
    view_name: str,
    fields: Dict[str, Any],
    goal: str
) -> Tuple[str, str]:
    """根据用户目标和可用字段动态生成SQL。

    Args:
        db_type: 数据库类型
        view_name: 视图名
        fields: 可用字段字典 {"字段名": {"type": "...", "desc": "..."}}
        goal: 用户目标

    Returns:
        (sql, reason) 元组
    """
    g = (goal or "").lower()

    # 判断用户意图
    want_count = any(w in g for w in ["几个", "多少", "数量", "count", "how many", "number of"])
    want_list = any(w in g for w in ["列表", "有哪些", "列出", "list", "show"])
    want_exists = any(w in g for w in ["是否存在", "有没有", "exists", "有没有"])
    want_stats = any(w in g for w in ["统计", "分布", "占比", "statistics", "distribution"])

    # Oracle 特殊处理
    if _normalize_db_type(db_type) == "oracle":
        view_upper = view_name.upper()

        # 表空间相关
        if "tablespace" in view_upper or "TABLESPACE" in view_upper:
            # BIGFILE 相关
            if "bigfile" in g:
                if want_count:
                    # 统计 bigfile 表空间数量
                    if "BIGFILE" in fields:
                        sql = f"SELECT COUNT(*) AS BIGFILE_COUNT FROM {view_name} WHERE BIGFILE = 'YES'"
                        return sql, f"统计 {view_name} 中 BIGFILE='YES' 的表空间数量"
                    elif "CONTENTS" in fields:
                        sql = f"SELECT COUNT(*) AS CNT FROM {view_name} WHERE CONTENTS = 'PERMANENT'"
                        return sql, f"统计永久表空间数量"

            # 表空间列表
            if want_list:
                select_cols = []
                for col in ["TABLESPACE_NAME", "STATUS", "CONTENTS", "FILE_NAME"]:
                    if col in fields:
                        select_cols.append(col)
                if select_cols:
                    sql = f"SELECT {', '.join(select_cols)} FROM {view_name} ORDER BY TABLESPACE_NAME"
                    return sql, f"列出 {view_name} 中的表空间"

        # 用户相关
        if "user" in g:
            if want_count:
                if "USERNAME" in fields:
                    sql = f"SELECT COUNT(*) AS USER_COUNT FROM {view_name}"
                    return sql, f"统计用户数量"
            if want_list:
                select_cols = []
                for col in ["USERNAME", "ACCOUNT_STATUS", "CREATED"]:
                    if col in fields:
                        select_cols.append(col)
                if select_cols:
                    sql = f"SELECT {', '.join(select_cols)} FROM {view_name} ORDER BY CREATED DESC"
                    return sql, f"列出用户"

        # 表相关
        if "table" in g or "表" in g:
            if want_count:
                if "TABLE_NAME" in fields:
                    sql = f"SELECT COUNT(*) AS TABLE_COUNT FROM {view_name}"
                    return sql, f"统计表数量"
            if want_list:
                select_cols = []
                for col in ["OWNER", "TABLE_NAME", "TABLESPACE_NAME", "NUM_ROWS"]:
                    if col in fields:
                        select_cols.append(col)
                if select_cols:
                    sql = f"SELECT {', '.join(select_cols)} FROM {view_name} ORDER BY NUM_ROWS DESC NULLS LAST"
                    return sql, f"列出表"

    # MySQL/PG 通用处理
    if want_count:
        # 找合适的计数字段
        for cnt_field in ["TABLE_NAME", "USERNAME", "datname", "spcname", "TABLE_SCHEMA"]:
            if cnt_field in fields:
                sql = f"SELECT COUNT(*) AS CNT FROM {view_name}"
                return sql, f"统计数量"
        # 兜底
        sql = f"SELECT COUNT(*) AS CNT FROM {view_name}"
        return sql, "统计数量"

    if want_list:
        # 取前几个有语义的字段作为列表
        list_fields = []
        for f in ["TABLE_NAME", "USERNAME", "datname", "spcname", "TABLESPACE_NAME", "SEGMENT_NAME"]:
            if f in fields:
                list_fields.append(f)
                if len(list_fields) >= 3:
                    break
        if not list_fields:
            list_fields = list(fields.keys())[:3]
        sql = f"SELECT {', '.join(list_fields)} FROM {view_name} LIMIT 100"
        return sql, f"列出数据"

    # 默认：取前几行
    select_cols = list(fields.keys())[:5]
    sql = f"SELECT {', '.join(select_cols)} FROM {view_name} LIMIT 10"
    return sql, "查询数据"


def get_view_columns(db_type: str, view_name: str) -> List[str]:
    """获取视图的字段名列表。"""
    kb = _load_knowledge()
    db_key = _normalize_db_type(db_type)
    views = kb.get("databases", {}).get(db_key, {}).get("views", {})
    
    simple_name = view_name.upper()
    for full_name, view_info in views.items():
        if full_name.upper() == simple_name or full_name.split(".")[-1].upper() == simple_name:
            return list(view_info.get("fields", {}).keys())
    return []


def get_view_info(db_type: str, view_name: str) -> Optional[Dict[str, Any]]:
    """获取视图的完整信息。"""
    kb = _load_knowledge()
    db_key = _normalize_db_type(db_type)
    views = kb.get("databases", {}).get(db_key, {}).get("views", {})
    
    simple_name = view_name.upper()
    for full_name, view_info in views.items():
        if full_name.upper() == simple_name or full_name.split(".")[-1].upper() == simple_name:
            return view_info
    return None


def report_field_labels(db_type: str, sql: str, columns: List[str]) -> List[str]:
    """读取视图说明与跨库报告字典，为原始字段名追加中文说明。

    支持连接查询、别名及原生采集。未登记的新字段显式标记待维护。
    """
    kb = get_knowledge()
    common = kb.get('report_fields', {}).get('fields', {})
    db_key = _normalize_db_type(db_type)
    databases = kb.get('databases', {})
    views = dict(databases.get(db_key, {}).get('views', {}))
    views.update(databases.get((db_type or '').lower(), {}).get('views', {}))
    # 去掉字符串与注释，避免把日志文本中的 FROM 误识别为视图来源。
    source_sql = re.sub(r"'(?:''|[^'])*'", "''", sql or '')
    source_sql = re.sub(r'/\*.*?\*/|--[^\n]*', ' ', source_sql, flags=re.S)
    sources = re.findall(
        r'\b(?:FROM|JOIN)\s+([\w$."`]+)', source_sql, flags=re.I)
    source_names = {name.replace('"', '').replace('`', '').upper() for name in sources}
    scoped_fields = []
    for name, info in views.items():
        if name.upper() in source_names or name.upper() in {n.split('.')[-1] for n in source_names}:
            scoped_fields.append({str(k).upper(): v for k, v in info.get('fields', {}).items()})
    aliases = {
        alias.upper(): source.upper()
        for source, alias in re.findall(
            r'\b(?:[\w$]+\.)?([\w$]+)\s+AS\s+["`]?([\w$]+)', source_sql, flags=re.I)
    }
    labels = []
    for column in columns:
        text = str(column)
        if re.search(r'[\u4e00-\u9fff]', text) or not re.search(r'[A-Za-z]', text):
            labels.append(text)
            continue
        field = aliases.get(text.upper(), text.upper())
        descriptions = {str(fields[field].get('desc') or '').strip()
                        for fields in scoped_fields if field in fields}
        descriptions = {d for d in descriptions if re.search(r'[\u4e00-\u9fff]', d)}
        description = next(iter(descriptions)) if len(descriptions) == 1 else common.get(field, '')
        if not description:
            canonical = re.sub(r'\s+', '_', text.strip('`"[]').lstrip('@')).upper()
            description = common.get(canonical, '待维护中文说明')
        labels.append(f'{text}（{description}）')
    return labels
