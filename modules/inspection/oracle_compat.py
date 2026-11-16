# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Oracle 10g-23ai 巡检 SQL 的版本适配层。

Oracle 巡检模板是一份静态 SQL，但不同大版本之间存在明确的语法/视图差异：

* ``FETCH FIRST n ROWS ONLY`` 是 12c 才支持的行限制语法，10g/11g 必须改写为
  ``ROWNUM``；
* ``DBA_REGISTRY_SQLPATCH`` 是 12c 的补丁视图，10g/11g 使用
  ``REGISTRY$HISTORY`` / ``DBA_REGISTRY_HISTORY``；
* 10g/11g 的 ``DBA_USERS`` 没有 ``AUTHENTICATION_TYPE`` 列，需要用
  ``EXTERNAL_NAME`` 推导；
* 10g 的 ``DBA_HIST_BASELINE`` 没有 ``CREATION_TIME``，用
  ``START_SNAP_TIME`` 替代；
* 12c+ 的 ``V$DATABASE`` 没有 ``PLUGGABLE_DB`` 列，旧模板/旧检查项若拼出该
  列会直接 ORA-00904。

本模块只做“把模板 SQL 变成目标版本可执行 SQL”这一件事，不改变查询语义，
也不依赖数据库连接。调用方可在执行失败时使用 ``oracle_query_candidates()``
取得候选链，在 ORA-00904/00942/00933 等结构类错误时安全回退。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

# 支持的 Oracle 巡检版本族
ORACLE_FAMILIES = ('10g', '11g', '12c', '18c', '19c', '21c', '23ai')

# FETCH FIRST/OFFSET 只在 12c+ 可用
_FETCH_RE = re.compile(
    r'(?is)\s+FETCH\s+(?:FIRST|NEXT)\s+(\d+)\s+ROWS?\s+ONLY\s*$')
_OFFSET_RE = re.compile(r'(?i)\bOFFSET\b')

# 结构类错误：可以安全尝试同义 SQL；权限/连接错误不在此列
_COMPATIBILITY_ERROR_MARKERS = (
    'ora-00904',  # invalid identifier
    'ora-00942',  # table or view does not exist
    'ora-00933',  # SQL command not properly ended
    'ora-00911',  # invalid character
    'ora-00920',  # invalid relational operator
    'ora-00923',  # FROM keyword not found
    'ora-00936',  # missing expression
    'ora-00918',  # column ambiguously defined
    'ora-00932',  # inconsistent datatypes
    'ora-00979',  # not a GROUP BY expression
)

# REGISTRY$HISTORY / DBA_REGISTRY_HISTORY 的列映射
_PATCH_COLUMN_MAP = {
    'patch_id': 'id AS patch_id',
    'patch_uid': 'NULL AS patch_uid',
    'patch_type': 'NULL AS patch_type',
    'description': 'comments AS description',
    'status': 'NULL AS status',
    'action': 'action',
    'action_time': 'action_time',
    'version': 'version',
    'comments': 'comments',
    'id': 'id',
}


@dataclass(frozen=True)
class OracleVersion:
    """解析后的 Oracle 版本。"""

    raw: str
    major: int
    minor: int = 0

    @property
    def family(self) -> str:
        if self.major >= 23:
            return '23ai'
        if self.major >= 21:
            return '21c'
        if self.major >= 19:
            return '19c'
        if self.major >= 18:
            return '18c'
        if self.major >= 12:
            return '12c'
        if self.major == 11:
            return '11g'
        if self.major == 10:
            return '10g'
        return 'unknown'

    @property
    def supports_fetch_first(self) -> bool:
        # 版本未知时按保守模式处理：ROWNUM 在所有版本都可用。
        return self.major >= 12

    @property
    def supports_registry_sqlpatch(self) -> bool:
        return self.major >= 12


def parse_version(version_text: object) -> OracleVersion:
    """解析 ``v$instance.version`` / ``v$version.banner`` 中的版本号。

    支持 ``19.0.0.0.0``、``Oracle Database 19c ...``、``23ai``、``11`` 等
    常见形式。无法解析时返回 major=0，调用方按最保守的兼容语法处理。
    """
    raw = str(version_text or '').strip()
    match = re.search(
        r'(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(\d{1,2}))?', raw)
    if match:
        return OracleVersion(
            raw=raw,
            major=int(match.group(1)),
            minor=int(match.group(2)),
        )
    match = re.search(r'(?i)\b(\d{1,2})\s*(?:c|g|ai)\b', raw)
    if match:
        return OracleVersion(raw=raw, major=int(match.group(1)))
    match = re.match(r'^\s*(\d{1,2})\s*$', raw)
    if match:
        return OracleVersion(raw=raw, major=int(match.group(1)))
    return OracleVersion(raw=raw, major=0)


def oracle_version_major(version_text: object) -> Optional[int]:
    """返回 Oracle 主版本号；无法解析时返回 ``None``。"""
    major = parse_version(version_text).major
    return major or None


def oracle_family(version_text: object) -> str:
    """返回版本族名称（10g/11g/12c/18c/19c/21c/23ai）。"""
    return parse_version(version_text).family


def _strip_sql(sql: object) -> str:
    text = str(sql or '').strip()
    if text.endswith(';'):
        text = text[:-1].rstrip()
    return text


def _split_select_list(text: str) -> List[str]:
    """按顶层逗号拆分 SELECT 列表，忽略括号内的逗号。"""
    parts: List[str] = []
    current: List[str] = []
    depth = 0
    for char in text:
        if char == '(':
            depth += 1
        elif char == ')':
            depth = max(depth - 1, 0)
        if char == ',' and depth == 0:
            parts.append(''.join(current).strip())
            current = []
            continue
        current.append(char)
    if current:
        parts.append(''.join(current).strip())
    return [part for part in parts if part]


def _patch_history_sql(sql: str, table: str) -> str:
    """把 ``DBA_REGISTRY_SQLPATCH`` 查询改写到历史视图。

    保留原查询的 WHERE/ORDER BY；仅替换表名和 12c 专属列名。
    """
    text = _strip_sql(sql)
    match = re.match(
        r'(?is)^\s*SELECT\s+(.*?)\s+FROM\s+dba_registry_sqlpatch\b(.*)$',
        text)
    if not match:
        text = re.sub(r'(?i)\bdba_registry_sqlpatch\b', table, text)
        return re.sub(r'(?i)\bregistry\$history\b', table, text)

    columns = []
    for column in _split_select_list(match.group(1)):
        key = column.strip().lower()
        columns.append(_PATCH_COLUMN_MAP.get(key, column))
    return 'SELECT %s FROM %s%s' % (', '.join(columns), table, match.group(2))


def _adapt_sqlpatch(sql: str, version: OracleVersion) -> str:
    if 'dba_registry_sqlpatch' not in sql.lower():
        return sql
    if version.supports_registry_sqlpatch:
        return sql
    return _patch_history_sql(sql, 'registry$history')


def _adapt_authentication_type(sql: str, version: OracleVersion) -> str:
    """10g/11g 的 DBA_USERS 没有 AUTHENTICATION_TYPE，用 EXTERNAL_NAME 推导。"""
    if version.major >= 12:
        return sql
    if not re.search(r'(?i)\bauthentication_type\b', sql):
        return sql

    # 只替换 SELECT 列表中的列，避免误改 WHERE/ORDER BY 中的同名列。
    select_match = re.match(r'(?is)^\s*SELECT\s+(.*?)\s+FROM\b', sql)
    if not select_match or not re.search(
            r'(?i)\bauthentication_type\b', select_match.group(1)):
        return sql

    expression = (
        "CASE WHEN external_name IS NULL THEN 'PASSWORD' "
        "ELSE 'EXTERNAL' END AS authentication_type")
    return re.sub(
        r'(?i)(?:\b[A-Za-z_]\w*\.)?\bauthentication_type\b',
        expression, sql, count=1)


def _adapt_awr_baseline(sql: str, version: OracleVersion) -> str:
    """10g 的 DBA_HIST_BASELINE 没有 CREATION_TIME，用 START_SNAP_TIME 替代。"""
    if version.major >= 11:
        return sql
    if 'dba_hist_baseline' not in sql.lower() or 'creation_time' not in sql.lower():
        return sql
    return re.sub(r'(?i)\bcreation_time\b', 'start_snap_time', sql)


def _adapt_pluggable_columns(sql: str) -> str:
    """移除 V$DATABASE 中不存在的 PLUGGABLE_DB/PLUGGABLE_NDB 列。"""
    return re.sub(
        r'(?i),\s*PLUGGABLE_(?:DB|NDB)\b(?:\s+AS\s+[A-Za-z_]\w*)?',
        '', sql)


def _rewrite_fetch_first(sql: str) -> Optional[str]:
    """把简单的 ``FETCH FIRST n ROWS ONLY`` 改写为 ROWNUM 形式。

    仅处理模板中常见的“单层 SELECT + 可选 ORDER BY”形式；带 OFFSET 或
    复杂集合运算时返回 ``None``，由调用方保留原 SQL/使用候选链。
    """
    text = _strip_sql(sql)
    match = _FETCH_RE.search(text)
    if not match:
        return None
    body = text[:match.start()].rstrip()
    if not body or _OFFSET_RE.search(body):
        return None
    if not re.match(r'(?is)^(?:SELECT|WITH)\b', body):
        return None
    # WITH 子句在 10g/11g 的内联视图中限制较多，模板未使用，遇到时保守放弃。
    if re.match(r'(?is)^WITH\b', body):
        return None
    return 'SELECT * FROM (%s) WHERE ROWNUM <= %d' % (
        body, int(match.group(1)))


def adapt_oracle_sql(
    sql: object,
    version: object,
    query_key: str = '',
) -> str:
    """按 Oracle 版本返回可执行 SQL。

    Args:
        sql: 模板中的原始 SQL。
        version: 版本文本（``19.0.0.0.0`` / ``Oracle Database 19c`` 等）。
        query_key: 可选的模板查询键，保留给后续细粒度适配。
    """
    del query_key  # 当前适配规则均可从 SQL 与版本号判断
    text = _strip_sql(sql)
    if not text:
        return text

    parsed = parse_version(version)
    text = _adapt_sqlpatch(text, parsed)
    text = _adapt_authentication_type(text, parsed)
    text = _adapt_awr_baseline(text, parsed)
    text = _adapt_pluggable_columns(text)

    if not parsed.supports_fetch_first:
        rewritten = _rewrite_fetch_first(text)
        if rewritten:
            text = rewritten
    return text


def adapt_oracle_queries(
    queries: Dict[str, str],
    version: object,
) -> Dict[str, str]:
    """批量适配 ``{query_key: sql}``，返回新字典。"""
    return {
        str(key): adapt_oracle_sql(sql, version, str(key))
        for key, sql in (queries or {}).items()
    }


def oracle_patch_query_candidates(version: object) -> List[str]:
    """返回读取补丁历史的候选 SQL（按优先级排序）。"""
    parsed = parse_version(version)
    preferred = (
        "SELECT patch_id, patch_uid, action, action_time, description, status "
        "FROM dba_registry_sqlpatch ORDER BY patch_id DESC"
        if parsed.supports_registry_sqlpatch else
        "SELECT id AS patch_id, NULL AS patch_uid, action, action_time, "
        "comments AS description, NULL AS status "
        "FROM registry$history ORDER BY action_time DESC"
    )
    candidates = [preferred]
    if parsed.supports_registry_sqlpatch:
        candidates.append(
            "SELECT id AS patch_id, NULL AS patch_uid, action, action_time, "
            "comments AS description, NULL AS status "
            "FROM registry$history ORDER BY action_time DESC")
    else:
        candidates.append(
            "SELECT id AS patch_id, NULL AS patch_uid, action, action_time, "
            "comments AS description, NULL AS status "
            "FROM dba_registry_history ORDER BY action_time DESC")
        candidates.append(
            "SELECT * FROM v$system_patch WHERE patch_id IS NOT NULL")
    return candidates


def oracle_query_candidates(
    sql: object,
    version: object,
    query_key: str = '',
    failed_sql: str = '',
) -> List[str]:
    """返回查询候选链（首选版本 SQL + 兼容回退）。

    ``failed_sql`` 会从结果中排除，便于执行器在结构类错误后直接取下一个。
    """
    parsed = parse_version(version)
    original = _strip_sql(sql)
    preferred = adapt_oracle_sql(original, parsed, query_key)
    candidates: List[str] = [preferred]

    if original and original != preferred:
        candidates.append(original)

    lower = original.lower()
    if ('dba_registry_sqlpatch' in lower or 'registry$history' in lower
            or 'dba_registry_history' in lower
            or str(query_key) == 'registry_sqlpatch'):
        candidates.append(_patch_history_sql(original, 'registry$history'))
        candidates.append(_patch_history_sql(original, 'dba_registry_history'))
        candidates.append(
            'SELECT * FROM v$system_patch WHERE patch_id IS NOT NULL')

    if 'fetch first' in lower or 'fetch next' in lower:
        rewritten = _rewrite_fetch_first(original)
        if rewritten:
            candidates.append(rewritten)

    if 'pluggable_db' in lower or 'pluggable_ndb' in lower:
        candidates.append(_adapt_pluggable_columns(original))

    seen = {_strip_sql(failed_sql).lower()}
    result: List[str] = []
    for candidate in candidates:
        normalized = _strip_sql(candidate)
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def is_compatibility_error(exc: object) -> bool:
    """判断异常是否属于可安全回退的语法/对象结构错误。"""
    text = str(exc or '').lower()
    return any(marker in text for marker in _COMPATIBILITY_ERROR_MARKERS)
