# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""SQL Server 2005–2025 巡检 SQL 的版本适配层。

SQL Server 的 DMV 与系统目录视图在各版本间有增删和列名变化。这里统一处理：

* 版本识别：``SERVERPROPERTY('ProductVersion')`` / ``@@VERSION``；
* 版本代际：2005、2008、2008 R2、2012、2014、2016、2017、2019、2022、2025；
* 旧版本列名改写：``sys.dm_os_memory_clerks.pages_kb`` 与
  ``single_pages_kb`` / ``multi_pages_kb``；
* SQL Server 2005 的 I/O DMV 回退到 ``::fn_virtualfilestats``；
* 新版本才有的功能（扩展事件、资源调控器、AlwaysOn、Query Store、
  ``sys.dm_server_services``）在旧版本明确标记“不适用”；
* 只在结构类错误时尝试候选 SQL，权限、登录、连接错误不掩盖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple


# ── 版本模型 ────────────────────────────────────────────────────────────

_VERSION_NAMES = {
    9: 'SQL Server 2005',
    10: 'SQL Server 2008',
    11: 'SQL Server 2012',
    12: 'SQL Server 2014',
    13: 'SQL Server 2016',
    14: 'SQL Server 2017',
    15: 'SQL Server 2019',
    16: 'SQL Server 2022',
    17: 'SQL Server 2025',
}

_FAMILY_YEARS = {
    9: '2005', 10: '2008', 11: '2012', 12: '2014', 13: '2016',
    14: '2017', 15: '2019', 16: '2022', 17: '2025',
}

# 只映射仓库里真正存在的独立模板；其它版本使用默认模板。
_TEMPLATE_VERSIONS = {
    9: '2005',
    10: '2008',
    12: '2014',
}


@dataclass(frozen=True)
class SqlServerVersion:
    """解析后的 SQL Server 版本。"""

    raw: str
    major: int = 0
    minor: int = 0
    build: int = 0
    revision: int = 0
    product_level: str = ''
    edition: str = ''

    @property
    def family(self) -> str:
        if not self.major:
            return 'unknown'
        if self.major == 10 and self.minor >= 50:
            return 'sqlserver-2008r2'
        return 'sqlserver-%s' % _FAMILY_YEARS.get(self.major, self.major)

    @property
    def display(self) -> str:
        if not self.major:
            return self.raw
        name = _VERSION_NAMES.get(self.major, 'SQL Server %d' % self.major)
        if self.major == 10 and self.minor >= 50:
            name = 'SQL Server 2008 R2'
        version = '%d.%d.%d' % (self.major, self.minor, self.build)
        if self.revision:
            version += '.%d' % self.revision
        return '%s (%s)' % (name, version)

    @property
    def template_version(self) -> Optional[str]:
        return _TEMPLATE_VERSIONS.get(self.major)

    def at_least(self, major: int, minor: int = 0) -> bool:
        return (self.major, self.minor) >= (major, minor)


_VERSION_RE = re.compile(r'(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{1,6})(?:\.(\d{1,6}))?')
_PRODUCT_LEVEL_RE = re.compile(r'(?i)Microsoft\s+SQL\s+Server\s+\d{4}\s*\(([^)]+)\)')
_EDITION_RE = re.compile(
    r'(?i)(Enterprise|Standard|Express|Developer|Web|Workgroup|Datacenter|'
    r'Business Intelligence|Analysis Services|Personal|Desktop Engine)'
    r'(?:\s+Edition)?')


def parse_sqlserver_version(version_text: object) -> SqlServerVersion:
    """解析 SQL Server 版本字符串。

    支持 ``15.0.4322.2``、``Microsoft SQL Server 2019 (RTM-CU22) ...``、
    ``10.50.6000.34`` 等形式。年份只用于佐证，不直接当作主版本号。
    """
    raw = str(version_text or '').strip()
    if not raw:
        return SqlServerVersion(raw='')
    match = _VERSION_RE.search(raw)
    if not match:
        return SqlServerVersion(raw=raw)
    major = int(match.group(1))
    minor = int(match.group(2))
    build = int(match.group(3))
    revision = int(match.group(4) or 0)
    # SQL Server 2005 及以后的主版本为 9–17；排除日期/补丁号等误匹配。
    if not (9 <= major <= 17):
        return SqlServerVersion(raw=raw)
    level = ''
    level_match = _PRODUCT_LEVEL_RE.search(raw)
    if level_match:
        level = level_match.group(1).strip()
    edition = ''
    edition_match = _EDITION_RE.search(raw)
    if edition_match:
        edition = edition_match.group(0).strip()
    return SqlServerVersion(
        raw=raw, major=major, minor=minor, build=build,
        revision=revision, product_level=level, edition=edition)


def sqlserver_family(version_text: object) -> str:
    return parse_sqlserver_version(version_text).family


def template_version_for_major(major: object) -> Optional[str]:
    """返回仓库里与主版本匹配的独立模板版本。"""
    try:
        return _TEMPLATE_VERSIONS.get(int(major))
    except (TypeError, ValueError):
        return None


# ── 版本探测 SQL ────────────────────────────────────────────────────────

def sqlserver_version_query_candidates() -> List[str]:
    return [
        "SELECT SERVERPROPERTY('ProductVersion') AS product_version, "
        "SERVERPROPERTY('ProductLevel') AS product_level, "
        "SERVERPROPERTY('Edition') AS edition, @@VERSION AS version",
        "SELECT @@VERSION AS version",
        "SELECT SERVERPROPERTY('ProductVersion') AS product_version, "
        "SERVERPROPERTY('ProductLevel') AS product_level",
    ]


def _row_value(row: Any, *names: str) -> str:
    if isinstance(row, dict):
        lower = {str(key).lower(): value for key, value in row.items()}
        for name in names:
            value = lower.get(name.lower())
            if value not in (None, ''):
                return str(value).strip()
    return ''


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


def fetch_sqlserver_version(cursor: Any) -> Tuple[str, SqlServerVersion]:
    """读取 SQL Server 版本；所有探测失败时返回 ``('', 空版本)``。"""
    best_text = ''
    best = parse_sqlserver_version('')
    for sql in sqlserver_version_query_candidates():
        try:
            cursor.execute(sql)
            rows = cursor.fetchall() or []
        except Exception:
            continue
        text = ''
        full = ''
        for row in rows:
            product = _row_value(row, 'product_version', 'ProductVersion')
            full = _row_value(row, 'version', '@@VERSION')
            level = _row_value(row, 'product_level', 'ProductLevel')
            edition = _row_value(row, 'edition', 'Edition')
            candidate = full or product
            if candidate:
                extra = ' '.join(item for item in (level, edition) if item)
                text = candidate + ((' | ' + extra) if extra else '')
                break
        if not text:
            text = _rows_text(rows)
        if not text:
            continue
        parsed = parse_sqlserver_version(text)
        if not parsed.major:
            continue
        if full and ('microsoft sql server' in full.lower() or parsed.major >= 9):
            return full, parsed
        if not best_text:
            best_text, best = text, parsed
        if parsed.product_level or parsed.edition:
            return text, parsed
    return best_text, best


# ── SQL Server 版本/方言适配 ────────────────────────────────────────────

_IO_STATS_2005_WITH_FILES = """SELECT DB_NAME(vfs.DbId) AS db_name, mf.physical_name,
       vfs.NumberReads AS num_of_reads, vfs.NumberWrites AS num_of_writes,
       CAST(vfs.IoStallMS/NULLIF(vfs.NumberReads,0) AS DECIMAL(18,2)) AS avg_read_ms,
       CAST(vfs.IoStallMS/NULLIF(vfs.NumberWrites,0) AS DECIMAL(18,2)) AS avg_write_ms
FROM ::fn_virtualfilestats(NULL, NULL) vfs
JOIN sys.master_files mf
  ON vfs.DbId = mf.database_id AND vfs.FileId = mf.file_id
ORDER BY vfs.IoStallMS DESC"""

_IO_STATS_2005 = """SELECT DB_NAME(DbId) AS db_name, FileId AS file_id,
       CAST(IoStallMS/NULLIF(NumberReads,0) AS DECIMAL(18,2)) AS read_latency_ms,
       CAST(IoStallMS/NULLIF(NumberWrites,0) AS DECIMAL(18,2)) AS write_latency_ms,
       NumberReads AS num_of_reads, NumberWrites AS num_of_writes
FROM ::fn_virtualfilestats(NULL, NULL)
ORDER BY IoStallMS DESC"""

_UPTIME_2005 = """SELECT crdate AS start_time,
       DATEDIFF(SECOND, crdate, GETDATE()) AS uptime_sec
FROM master..sysdatabases
WHERE name = 'tempdb'"""


def _adapt_memory_clerks(text: str, parsed: SqlServerVersion) -> str:
    """2005/2008 使用 single/multi_pages_kb；2012+ 使用 pages_kb。"""
    upper = text.upper()
    if 'SYS.DM_OS_MEMORY_CLERKS' not in upper:
        return text
    if parsed.at_least(11):
        text = re.sub(
            r'\bSINGLE_PAGES_KB\s*\+\s*MULTI_PAGES_KB\b',
            'PAGES_KB', text, flags=re.I)
        text = re.sub(r'\bSINGLE_PAGES_KB\b', 'PAGES_KB', text, flags=re.I)
        text = re.sub(r'\bMULTI_PAGES_KB\b', 'PAGES_KB', text, flags=re.I)
    else:
        text = re.sub(
            r'\bPAGES_KB\b', '(SINGLE_PAGES_KB + MULTI_PAGES_KB)',
            text, flags=re.I)
    return text


def _adapt_uptime(text: str, parsed: SqlServerVersion, key: str) -> str:
    """SQL Server 2005 的 sys.dm_os_sys_info 没有 sqlserver_start_time。"""
    if parsed.major and parsed.major < 10 and (
            key == 'server_uptime' or 'SQLSERVER_START_TIME' in text.upper()):
        return _UPTIME_2005
    return text


def _adapt_io_stats(text: str, parsed: SqlServerVersion) -> str:
    """SQL Server 2005 没有 sys.dm_io_virtual_file_stats。"""
    if parsed.major and parsed.major >= 10:
        return text
    upper = text.upper()
    if 'SYS.DM_IO_VIRTUAL_FILE_STATS' not in upper:
        return text
    if 'PHYSICAL_NAME' in upper or 'SYS.MASTER_FILES' in upper:
        return _IO_STATS_2005_WITH_FILES
    return _IO_STATS_2005


def adapt_sqlserver_sql(sql: object, version_text: object = '',
                        key: str = '') -> str:
    """按 SQL Server 版本改写已确认不兼容的模板/业务 SQL。"""
    text = str(sql or '').strip()
    if not text:
        return text
    parsed = parse_sqlserver_version(version_text)
    if not parsed.major:
        return text
    text = _adapt_memory_clerks(text, parsed)
    text = _adapt_uptime(text, parsed, key)
    text = _adapt_io_stats(text, parsed)
    return text


def sqlserver_query_candidates(
    sql: object,
    version_text: object = '',
    key: str = '',
    failed_sql: object = '',
) -> List[str]:
    """返回结构类错误的候选 SQL 链（去重、保序）。"""
    adapted = adapt_sqlserver_sql(sql, version_text, key)
    candidates: List[str] = [adapted]
    parsed = parse_sqlserver_version(version_text)
    if parsed.major:
        # 模板已被执行过时，用失败语句再生成一次候选，覆盖旧库中
        # 尚未迁移到新列名的业务 SQL。
        old = adapt_sqlserver_sql(failed_sql or '', version_text, key)
        if old and old != adapted:
            candidates.append(old)
    # 单表查询列不兼容时退回 SELECT *；保留 TOP 限制。
    fallback = _star_fallback(adapted)
    if fallback:
        candidates.append(fallback)
    return [item for item in candidates if item]


def _star_fallback(sql: str) -> str:
    upper = sql.upper()
    if ' JOIN ' in upper or ' UNION ' in upper or ' FROM (' in upper:
        return ''
    match = re.match(
        r'(?is)^\s*SELECT\s+(?:TOP\s+\d+\s+)?(.*?)\s+FROM\s+'
        r'([A-Za-z0-9_\.\[\]#@]+)', sql)
    if not match:
        return ''
    top = re.match(r'(?is)^\s*SELECT\s+(TOP\s+\d+)\s+', sql)
    prefix = 'SELECT ' + (top.group(1) + ' ' if top else '')
    return '%s* FROM %s' % (prefix, match.group(2))


# ── 错误分类与“不适用”说明 ─────────────────────────────────────────────

_COMPATIBILITY_MARKERS = (
    'invalid column name', 'invalid object name', 'invalid column',
    'invalid object', 'could not find', 'could not be bound',
    'multi-part identifier', 'does not exist', "doesn't exist",
    'incorrect syntax', 'syntax error', 'undefined column',
    '列名无效', '对象名', '无效的列', '无效列', '语法错误',
    '无法绑定', '找不到', '不存在',
)
_NON_COMPATIBILITY_MARKERS = (
    'login failed', 'access denied', 'permission denied', 'not authorized',
    'authentication', 'connection', 'timeout', 'network-related',
    '登录失败', '权限', '认证', '连接失败', '超时',
)


def is_sqlserver_compatibility_error(error: object) -> bool:
    low = str(error or '').lower()
    if not low:
        return False
    if any(marker in low for marker in _NON_COMPATIBILITY_MARKERS):
        return False
    return any(marker in low for marker in _COMPATIBILITY_MARKERS)


def optional_feature_reason(sql: object, version_text: object = '',
                            cursor: Any = None) -> Optional[str]:
    """新版本才有的功能在旧版本返回“不适用”，避免误报为查询失败。"""
    upper = str(sql or '').upper()
    parsed = parse_sqlserver_version(version_text)
    major, minor = parsed.major, parsed.minor

    if 'SYS.DM_XE_SESSIONS' in upper and major and major < 10:
        return '不适用：SQL Server 2005 不提供扩展事件（需要 2008+）'
    if 'SYS.RESOURCE_GOVERNOR_' in upper and major and major < 10:
        return '不适用：SQL Server 2005 不提供资源调控器（需要 2008+）'
    if 'SYS.DM_SERVER_SERVICES' in upper and major and (
            major < 10 or (major == 10 and minor < 50)):
        return '不适用：sys.dm_server_services 需要 SQL Server 2008 R2+'
    if ('SYS.AVAILABILITY_' in upper or 'SYS.DM_HADR_' in upper) and major and major < 11:
        return '不适用：SQL Server 2005/2008 不提供 AlwaysOn 可用性组（需要 2012+）'
    if ('SYS.QUERY_STORE_' in upper or 'QUERY_STORE' in upper) and major and major < 13:
        return '不适用：Query Store 需要 SQL Server 2016+'
    if 'DISTRIBUTION.DBO.MSPUBLICATIONS' in upper and cursor is not None:
        try:
            cursor.execute("SELECT DB_ID('distribution') AS v")
            row = cursor.fetchone()
            value = row[0] if isinstance(row, (list, tuple)) else (
                row.get('v') if isinstance(row, dict) else row)
            if value in (None, ''):
                return '不适用：当前实例未配置复制分发库 distribution'
        except Exception:
            pass
    return None
