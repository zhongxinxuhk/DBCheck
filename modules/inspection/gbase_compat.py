# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""GBase 8a / GBase 8s 巡检 SQL 的版本适配层。

GBase 8a（MPP，MySQL 协议）与 GBase 8s（Informix 血统）是两套不同的
SQL 方言，不能共用一套模板适配规则。本模块分别处理：

* 版本识别：8a 从 ``VERSION()`` / ``@@VERSION_COMMENT`` 读取；
  8s 从 ``DBINFO('version','full')`` 读取；
* 8s 方言：系统视图补 ``sysmaster:`` 前缀，``LIMIT`` 改为 Informix
  的 ``SELECT FIRST n``；
* 8a 可选监控视图：集群监控、资源池、节点磁盘等视图在旧版本/非 MPP
  部署中可能不存在，缺失时回退或标记“不适用”；
* 只在无效列/表/语法错误时尝试候选 SQL，权限、连接、认证错误不掩盖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple


# ── 版本模型 ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GbaseVersion:
    """解析后的 GBase 版本。

    ``product`` 为 ``gbase8a`` 或 ``gbase8s``；``build`` 是版本号最后一段
    （8a 的 9.5.3.28 中的 28，8s 的 12.10.FC12 中的 12）。
    """

    raw: str
    product: str
    major: int = 0
    minor: int = 0
    patch: int = 0
    build: int = 0

    @property
    def family(self) -> str:
        if not self.major:
            return 'unknown'
        if self.product == 'gbase8a':
            if self.major == 8 and self.minor in (5, 6):
                return 'gbase8a-%d.%d' % (self.major, self.minor)
            if self.major == 9 and self.minor == 5:
                if self.patch >= 3:
                    return 'gbase8a-9.5.3+'
                return 'gbase8a-9.5.%d' % max(self.patch, 0)
            return 'gbase8a-%d.%d' % (self.major, self.minor)
        if self.product == 'gbase8s':
            if self.major == 12 and self.minor == 10:
                return 'gbase8s-12.10'
            return 'gbase8s-%d.%d' % (self.major, self.minor)
        return 'unknown'

    @property
    def display(self) -> str:
        if not self.major:
            return self.raw
        parts = [str(self.major), str(self.minor), str(self.patch)]
        if self.build:
            parts.append(str(self.build))
        prefix = 'GBase 8a' if self.product == 'gbase8a' else 'GBase 8s'
        return '%s %s' % (prefix, '.'.join(parts))


_VERSION_RE = re.compile(
    r'(?i)(?:^|[^0-9])V?(\d{1,2})\.(\d{1,2})'
    r'(?:\.(\d{1,3}))?(?:\.(\d{1,5}))?')
_GBASE8S_MAJOR_RE = re.compile(r'(?i)\bGBase\s*8s\b')


def _parsed(raw: str, product: str) -> GbaseVersion:
    text = str(raw or '').strip()
    if not text:
        return GbaseVersion(raw='', product=product)
    match = _VERSION_RE.search(text)
    if match:
        major = int(match.group(1))
        minor = int(match.group(2))
        patch = int(match.group(3) or 0)
        build = int(match.group(4) or 0)
        # 排除日期片段（如 2023.12.26）被误当作版本号。
        if 6 <= major <= 20:
            return GbaseVersion(raw=text, product=product, major=major,
                                minor=minor, patch=patch, build=build)
    if product == 'gbase8s' and _GBASE8S_MAJOR_RE.search(text):
        return GbaseVersion(raw=text, product=product, major=8)
    return GbaseVersion(raw=text, product=product)


def parse_gbase8a_version(version_text: object) -> GbaseVersion:
    """解析 GBase 8a 版本（8.5 / 8.6 / 9.5.x 及仅显示 V9 的形式）。"""
    return _parsed(str(version_text or ''), 'gbase8a')


def parse_gbase8s_version(version_text: object) -> GbaseVersion:
    """解析 GBase 8s 版本（GBase 8s V8.8 / Informix 12.10.FC 等）。"""
    return _parsed(str(version_text or ''), 'gbase8s')


def gbase_family(version_text: object, product: str = 'gbase8a') -> str:
    parser = parse_gbase8a_version if product == 'gbase8a' else parse_gbase8s_version
    return parser(version_text).family


# ── 版本探测 SQL ────────────────────────────────────────────────────────

def gbase8a_version_query_candidates() -> List[str]:
    return [
        'SELECT VERSION()',
        'SELECT VERSION() AS version, @@VERSION_COMMENT AS comment',
        'SELECT @@VERSION_COMMENT',
        "SHOW VARIABLES LIKE 'version'",
        "SHOW VARIABLES LIKE 'version_comment'",
    ]


def gbase8s_version_query_candidates() -> List[str]:
    return [
        "SELECT DBINFO('version', 'full') FROM systables WHERE tabid = 1",
        "SELECT DBINFO('version', 'server') FROM systables WHERE tabid = 1",
        "SELECT DBINFO('version', 'short') FROM systables WHERE tabid = 1",
    ]


def _rows_text(rows: Any) -> str:
    parts: List[str] = []
    for row in rows or []:
        if isinstance(row, dict):
            values = row.values()
        elif isinstance(row, (list, tuple)):
            values = row
        else:
            values = (row,)
        for value in values:
            text = str(value).strip()
            if text and text not in parts:
                parts.append(text)
    return ' | '.join(parts)


def fetch_gbase8a_version(cursor: Any) -> Tuple[str, GbaseVersion]:
    """读取 GBase 8a 版本；所有探测失败时返回 ``('', 空版本)``。"""
    best_text = ''
    best = parse_gbase8a_version('')
    for sql in gbase8a_version_query_candidates():
        try:
            cursor.execute(sql)
            rows = cursor.fetchall() or []
        except Exception:
            continue
        text = _rows_text(rows)
        if not text:
            continue
        parsed = parse_gbase8a_version(text)
        if not parsed.major:
            continue
        if 'gbase' in text.lower():
            return text, parsed
        if not best_text:
            best_text, best = text, parsed
    return best_text, best


def fetch_gbase8s_version(cursor: Any) -> Tuple[str, GbaseVersion]:
    """读取 GBase 8s 版本；所有探测失败时返回 ``('', 空版本)``。"""
    best_text = ''
    best = parse_gbase8s_version('')
    for sql in gbase8s_version_query_candidates():
        try:
            cursor.execute(sql)
            rows = cursor.fetchall() or []
        except Exception:
            continue
        text = _rows_text(rows)
        if not text:
            continue
        parsed = parse_gbase8s_version(text)
        if not parsed.major:
            continue
        if 'gbase' in text.lower() or (parsed.major == 12 and parsed.minor == 10):
            return text, parsed
        if not best_text:
            best_text, best = text, parsed
    return best_text, best


# ── GBase 8s 方言适配 ──────────────────────────────────────────────────

# 这些视图属于 sysmaster 库；直接连业务库时必须写跨库前缀。
_GBASE8S_SYSMASTER_VIEWS = (
    'syssessions', 'sysconfig', 'sysdbspaces', 'syschunks', 'syslogs',
    'sysprofile', 'syslocks', 'sysdatabases', 'syschkio', 'syssqexplain',
)

_GBASE8S_DBSPACE_USAGE_SQL = (
    'SELECT d.name AS dbspace_name, d.pagesize, d.nchunks, '
    'SUM(c.chksize) AS total_pages, SUM(c.nfree) AS free_pages, '
    'ROUND(SUM(c.chksize) * d.pagesize / 1024 / 1024, 2) AS total_mb, '
    'ROUND(SUM(c.nfree) * d.pagesize / 1024 / 1024, 2) AS free_mb, '
    'ROUND((SUM(c.chksize) - SUM(c.nfree)) * 100.0 / '
    'NULLIF(SUM(c.chksize), 0), 2) AS used_pct '
    'FROM sysmaster:sysdbspaces d '
    'JOIN sysmaster:syschunks c ON d.dbsnum = c.dbsnum '
    'GROUP BY d.name, d.pagesize, d.nchunks ORDER BY d.name'
)


def _prefix_sysmaster(sql: str) -> str:
    text = sql
    for view in sorted(_GBASE8S_SYSMASTER_VIEWS, key=len, reverse=True):
        text = re.sub(
            r'(?i)\b(FROM|JOIN)\s+' + re.escape(view) + r'\b',
            r'\1 sysmaster:' + view,
            text,
        )
    return text


def _limit_to_first(sql: str) -> str:
    """把尾部 ``LIMIT n`` 改为 Informix 的 ``SELECT FIRST n``。"""
    match = re.search(r'(?is)\s+LIMIT\s+(\d+)\s*$', sql)
    if not match:
        return sql
    limit = match.group(1)
    text = sql[:match.start()].rstrip()
    if re.match(r'(?is)^\s*SELECT\s+(?!FIRST\b|SKIP\b)', text):
        text = re.sub(
            r'(?is)^(\s*SELECT\s+)',
            r'\1FIRST ' + limit + ' ',
            text,
            count=1,
        )
    return text


def adapt_gbase8s_sql(sql: object, version_text: object = '', key: str = '') -> str:
    """把 GBase 8s 巡检 SQL 改成 Informix 可执行写法。

    目前只做两类已确认差异：sysmaster 跨库前缀、``LIMIT`` → ``FIRST``。
    旧模板把 ``sysdbspaces.create_size`` 等不存在的列写进查询，按 query key
    精确替换为基于 syschunks 的容量汇总。
    """
    text = str(sql or '').strip().rstrip(';').strip()
    if not text:
        return text
    if key == 'gbase_version' or 'DBINFO(\"VERSION\"' in text.upper():
        text = ("SELECT DBINFO('version', 'full') AS version "
                "FROM systables WHERE tabid = 1")
    elif key == 'gbase_instance_status' or 'TABTYPE = \"T\"' in text.upper():
        text = ("SELECT COUNT(*) AS table_count FROM systables "
                "WHERE tabtype = 'T'")
    if key == 'gbase_dbspace_usage' or (
            'CREATE_SIZE' in text.upper() and 'EXTEND_SIZE' in text.upper()):
        text = _GBASE8S_DBSPACE_USAGE_SQL
    text = _prefix_sysmaster(text)
    text = _limit_to_first(text)
    return text


# ── GBase 8a 候选 SQL ─────────────────────────────────────────────────

_GBASE8A_PROCESSLIST_LOCKS = (
    "SELECT ID, USER, HOST, DB, COMMAND, TIME, STATE, LEFT(INFO, 200) AS SQL_TEXT "
    "FROM information_schema.PROCESSLIST WHERE COMMAND <> 'Sleep' "
    "AND (STATE LIKE '%lock%' OR STATE LIKE '%wait%' OR STATE LIKE '%metadata%') "
    "ORDER BY TIME DESC"
)

_GBASE8A_OPTIONAL_VIEW_REASONS = {
    'CLUSTER_MONIT_INFO': '当前版本或部署未提供集群监控视图 CLUSTER_MONIT_INFO，已跳过',
    'GNODES_RESOURCE_POOL_USAGE': '当前版本或部署未提供节点资源池视图，已跳过',
    'GNODES_USER_DISKSPACE_USAGE': '当前版本或部署未提供节点磁盘配额视图，已跳过',
    'TABLE_LOCKS': '当前版本或部署未提供 TABLE_LOCKS，已跳过',
    'COORDINATORS_TASK_INFORMATION': '当前版本未提供协调节点任务视图，已跳过',
    'CLUSTER_DISK_USAGE_INFO': '当前版本未提供集群磁盘视图，已跳过',
    'DISK_USAGE_INFO': '当前版本未提供磁盘使用视图，已跳过',
    'EVENTS': '当前版本未提供 information_schema.EVENTS，已跳过',
}

_GBASE8A_OPTIONAL_FALLBACKS = {
    'CLUSTER_MONIT_INFO': 'SHOW GCLUSTER NODES',
    'GNODES_RESOURCE_POOL_USAGE': 'SHOW NODES',
    'GNODES_USER_DISKSPACE_USAGE': 'SHOW NODES',
    'TABLE_LOCKS': _GBASE8A_PROCESSLIST_LOCKS,
    'COORDINATORS_TASK_INFORMATION': 'SHOW PROCESSLIST',
    'CLUSTER_DISK_USAGE_INFO': 'SHOW NODES',
    'DISK_USAGE_INFO': 'SHOW NODES',
}


def _star_fallback(sql: str) -> Optional[str]:
    """简单单表查询列不兼容时退回 SELECT *，保留尾部 LIMIT。"""
    upper = sql.upper()
    if ' JOIN ' in upper or ' UNION ' in upper or ' FROM (' in upper:
        return None
    match = re.match(
        r'(?is)^\s*SELECT\s+(.*?)\s+FROM\s+([A-Za-z0-9_$#\.:]+)', sql)
    if not match:
        return None
    table = match.group(2)
    limit = re.search(r'(?i)\bLIMIT\s+(\d+)', sql)
    suffix = ' LIMIT %s' % limit.group(1) if limit else ''
    return 'SELECT * FROM %s%s' % (table, suffix)


def gbase8a_query_candidates(
    sql: object,
    version_text: object = '',
    key: str = '',
    failed_sql: object = '',
) -> List[str]:
    """返回 GBase 8a 结构类错误的候选 SQL 链（去重）。"""
    adapted = adapt_gbase8a_sql(sql, version_text, key)
    upper = adapted.upper()
    candidates: List[str] = [adapted]
    for view, fallback in _GBASE8A_OPTIONAL_FALLBACKS.items():
        if view in upper:
            candidates.append(fallback)
    candidates.append(_star_fallback(adapted))
    return [item for item in candidates if item]


def gbase8s_query_candidates(
    sql: object,
    version_text: object = '',
    key: str = '',
    failed_sql: object = '',
) -> List[str]:
    """返回 GBase 8s 结构类错误的候选 SQL 链（去重）。"""
    adapted = adapt_gbase8s_sql(sql, version_text, key)
    candidates: List[str] = [adapted]
    if 'CREATE_SIZE' in adapted.upper() or 'EXTEND_SIZE' in adapted.upper():
        candidates.append(_GBASE8S_DBSPACE_USAGE_SQL)
    candidates.append(_star_fallback(adapted))
    return [item for item in candidates if item]


_GBASE8A_SYSTEM_SCHEMAS = "'information_schema','gbase','gclusterdb','performance_schema'"


def adapt_gbase8a_sql(sql: object, version_text: object = '', key: str = '') -> str:
    """GBase 8a 版本无关的轻量改写；保留入口供运行时统一调用。"""
    text = str(sql or '').strip().rstrip(';').strip()
    # W06 汇总各库数据量时如果不排除系统库，会把 gbase/gclusterdb 的
    # 内部表一起扫进来；老模板同样需要在运行时补上这个过滤条件。
    if key == 'W06_schema_space' and 'TABLE_SCHEMA NOT IN' not in text.upper():
        clause = 'TABLE_SCHEMA NOT IN (%s)' % _GBASE8A_SYSTEM_SCHEMAS
        if re.search(r'\bWHERE\b', text, re.I):
            text = re.sub(r'\bGROUP\s+BY\b', 'AND ' + clause + ' GROUP BY',
                          text, count=1, flags=re.I)
        else:
            text = re.sub(r'\bGROUP\s+BY\b', 'WHERE ' + clause + ' GROUP BY',
                          text, count=1, flags=re.I)
    return text


# ── 错误分类与“不适用”说明 ─────────────────────────────────────────────

_GBASE_COMPATIBILITY_MARKERS = (
    'unknown column', 'unknown table', 'doesn\'t exist', 'does not exist',
    'no such table', 'no such column', 'invalid column', 'invalid table',
    'syntax error', 'sql syntax', 'unknown system variable',
    'table or view does not exist', 'unknown view',
    "this version of gbase doesn't yet support",
    '无效的列', '无效列', '列名无效', '字段无效',
    '无效的表', '无效表', '表或视图不存在', '对象不存在', '语法错误',
)
_GBASE_NON_COMPATIBILITY_MARKERS = (
    'access denied', 'permission denied', 'not authorized', 'authentication',
    'invalid password', 'login failed', 'connection', 'timeout',
    '权限', '认证', '登录失败', '连接失败', '超时',
)


def is_gbase_compatibility_error(error: object) -> bool:
    low = str(error or '').lower()
    if not low:
        return False
    if any(marker in low for marker in _GBASE_NON_COMPATIBILITY_MARKERS):
        return False
    return any(marker in low for marker in _GBASE_COMPATIBILITY_MARKERS)


def optional_view_reason(sql: object, key: str = '') -> Optional[str]:
    """GBase 8a 可选视图缺失时返回“不适用”说明。"""
    upper = str(sql or '').upper()
    if re.match(r'\s*SHOW\s+(?:GCLUSTER\s+)?NODES\b', upper):
        return '当前 GBase 8a 版本或部署不提供集群节点查询，此项不适用'
    for view, reason in _GBASE8A_OPTIONAL_VIEW_REASONS.items():
        if view in upper:
            return reason
    return None
