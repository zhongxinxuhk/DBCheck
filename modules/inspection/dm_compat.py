# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""达梦 DM7/DM8 巡检 SQL 的版本适配层（DM6 仅做版本识别）。

达梦巡检模板以 DM8 为基线，但现场仍存在 DM7、DM8.1.0、DM8.1.1 等版本。
这些版本的共同点是核心 Oracle 兼容视图基本一致，差异主要集中在：

* 版本号来源不同：DM7 的 ``V$VERSION.BANNER`` 直接包含 ``V7.6.0.x``，
  DM8 的 ``V$VERSION`` 往往只显示 ``V8``，真实小版本要从 ``ID_CODE``
  （如 ``1-3-100-2023.12.26-...``）或 ``V$INSTANCE.BUILD_VERSION`` 读取；
* 早期 DM8 没有 ``V$INSTANCE.BUILD_VERSION``，不能用它作为唯一版本来源；
* 部分数据守护/DSC 视图只在特定部署或版本中存在，单机或旧版本查询会报
  无效表/视图；
* ``V$SQL_HISTORY`` 在部分旧版本不可用，需要回退到
  ``V$LONG_EXEC_SQLS``；
* 会话状态在不同版本可能是 ``IDLE``、``I`` 或 ``INACTIVE``；
* ``V$PARAMETER.TYPE`` 的可读参数过滤应使用 ``READ ONLY``，不是 ``'R'``。

本模块只负责版本解析、SQL 轻量改写和结构类错误的候选链，不建立数据库连接，
也不改变权限、连接或查询结果的安全边界。调用方只在“无效列/表/语法”类错误
时使用候选链，权限、网络、认证错误仍按原逻辑报告。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 支持的达梦巡检版本族。dm8 表示只识别到大版本、无法确定小版本。
DM_FAMILIES = ('dm6', 'dm7', 'dm8.1.0', 'dm8.1.1', 'dm8.1.2', 'dm8.1.3', 'dm8')

_ID_CODE_RE = re.compile(r'^\s*(\d{1,2})-(\d{1,2})-(\d{1,5})(?:\D|$)')
_VERSION_RE = re.compile(
    r'(?i)(?:^|[^0-9])V?(\d{1,2})\.(\d{1,2})'
    r'(?:\.(\d{1,3}))?(?:\.(\d{1,5}))?')
_MAJOR_RE = re.compile(r'(?i)\b(?:DM|V)\s*(\d{1,2})\b')

# 结构类错误：可以安全尝试同义 SQL；权限/连接类错误不在此列
_COMPATIBILITY_ERROR_MARKERS = (
    'invalid column',
    'unknown column',
    'column not found',
    'invalid identifier',
    '无效的列',
    '无效列',
    '列名无效',
    'table or view does not exist',
    'invalid table',
    'invalid object',
    '无效的表',
    '无效表',
    '表或视图不存在',
    '对象不存在',
    '对象名无效',
    'syntax error',
    '语法错误',
    'sql command not properly ended',
    'invalid character',
    'ora-00904',
    'ora-00942',
    'ora-00933',
    '-2116',
    '-2106',
    '-2103',
    '-2101',
    '-2007',
)

_NON_COMPATIBILITY_MARKERS = (
    'permission',
    'not authorized',
    'denied',
    '权限',
    'authentication',
    'invalid password',
    '登录失败',
    'connection',
    '连接',
    'timeout',
    '超时',
)

# 可选视图：缺失时报告“不适用”，而不是“查询失败”。
_OPTIONAL_VIEW_REASONS = {
    'V$DSC_EP_INFO': '当前部署未提供 V$DSC_EP_INFO（仅 DMDSC 共享存储集群），已跳过',
    'V$DMWATCHER': '当前版本或部署未提供数据守护视图 V$DMWATCHER，已跳过',
    'V$MAL_INFO': '当前版本或部署未提供 MAL 视图 V$MAL_INFO，已跳过',
    'V$MAL_LINK_STATUS': '当前版本或部署未提供 MAL 链路视图 V$MAL_LINK_STATUS，已跳过',
    'V$ARCH_STATUS': '当前版本或部署未提供归档状态视图 V$ARCH_STATUS，已跳过',
    'V$RAPPLY_STAT': '当前版本或部署未提供数据守护应用统计视图 V$RAPPLY_STAT，已跳过',
    'V$SQL_HISTORY': '当前版本未提供 V$SQL_HISTORY，且 V$LONG_EXEC_SQLS 回退不可用，已跳过',
    'V$ERR_INFO': '当前版本未提供错误信息视图 V$ERR_INFO，已跳过',
    'V$ARCHIVED_LOG': '当前版本未提供归档日志视图 V$ARCHIVED_LOG，已跳过',
    'V$BACKUPSET': '当前版本未提供备份集视图 V$BACKUPSET，已跳过',
}


@dataclass(frozen=True)
class DmVersion:
    """解析后的达梦版本。

    ``build`` 对应 ID_CODE 的最后一段（如 ``1-3-100`` 中的 100），也是
    DM8 的月度/临时版本号。DM7 的 ``build`` 对应 banner 的末段。
    """

    raw: str
    major: int = 0
    minor: int = 0
    patch: int = 0
    build: int = 0

    @property
    def family(self) -> str:
        if self.major == 6:
            return 'dm6'
        if self.major == 7:
            return 'dm7'
        if self.major == 8:
            if self.minor == 1:
                if self.patch >= 3:
                    return 'dm8.1.3'
                if self.patch >= 2:
                    return 'dm8.1.2'
                if self.patch >= 1:
                    return 'dm8.1.1'
                return 'dm8.1.0'
            return 'dm8'
        return 'unknown'

    @property
    def display(self) -> str:
        """人类可读版本号，不带构建日期。"""
        if not self.major:
            return self.raw
        if self.major == 8 and self.minor == 1:
            return 'DM8 %d.%d.%d.%d' % (
                self.major, self.minor, self.patch, self.build)
        if self.build:
            return 'DM%d %d.%d.%d.%d' % (
                self.major, self.major, self.minor, self.patch, self.build)
        return 'DM%d %d.%d.%d' % (
            self.major, self.major, self.minor, self.patch)

    @property
    def supports_build_version(self) -> bool:
        """V$INSTANCE.BUILD_VERSION 在 DM8.1.2+ 可用。"""
        return self.major == 8 and self.minor == 1 and self.patch >= 2

    @property
    def is_legacy(self) -> bool:
        return self.major in (6, 7) or (
            self.major == 8 and self.minor == 1 and self.patch <= 1)


def parse_dm_version(version_text: object) -> DmVersion:
    """解析达梦版本号。

    支持 ``V7.6.0.145``、``8.1.3.100``、``DM Database Server 64 V8``、
    ``1-3-100-2023.12.26-...`` 等现场常见形式。无法解析时返回 major=0，
    调用方按保守模式处理。
    """
    raw = str(version_text or '').strip()
    if not raw:
        return DmVersion(raw='')

    # DM8 ID_CODE：1-3-100-2023.12.26-... 表示 8.1.3.100。
    match = _ID_CODE_RE.match(raw)
    if match:
        first, second, third = (int(match.group(i)) for i in (1, 2, 3))
        if first == 1:
            return DmVersion(raw=raw, major=8, minor=1,
                             patch=second, build=third)
        if first in (6, 7, 8):
            return DmVersion(raw=raw, major=first, minor=second,
                             patch=0, build=third)

    match = _VERSION_RE.search(raw)
    if match:
        major = int(match.group(1))
        minor = int(match.group(2))
        patch = int(match.group(3) or 0)
        build = int(match.group(4) or 0)
        # 排除日期片段（如 2023.12.26）被误当作版本号。
        if 6 <= major <= 9:
            return DmVersion(raw=raw, major=major, minor=minor,
                             patch=patch, build=build)

    match = _MAJOR_RE.search(raw)
    if match:
        major = int(match.group(1))
        if 6 <= major <= 9:
            return DmVersion(raw=raw, major=major)
    return DmVersion(raw=raw)


def dm_family(version_text: object) -> str:
    """返回版本族，供调用方按族选择兼容策略。"""
    return parse_dm_version(version_text).family


def version_query_candidates() -> List[str]:
    """按“信息量从高到低”返回版本探测 SQL。

    先读 ID_CODE/BUILD_VERSION，避免 DM8 的 ``V$VERSION`` 只返回 ``V8``；
    最后才用 ``V$VERSION`` 兜底，兼容 DM7。
    """
    return [
        'SELECT ID_CODE FROM V$VERSION',
        'SELECT BUILD_VERSION FROM V$INSTANCE',
        'SELECT VERSION$ FROM V$VERSION',
        'SELECT BANNER FROM V$VERSION WHERE ROWNUM = 1',
        'SELECT * FROM V$VERSION',
    ]


def _row_text(row: Any) -> str:
    if isinstance(row, dict):
        values = row.values()
    elif isinstance(row, (list, tuple)):
        values = row
    else:
        values = (row,)
    parts = []
    for value in values:
        text = str(value).strip()
        if text and text not in parts:
            parts.append(text)
    return ' | '.join(parts)


def fetch_server_version(cursor: Any) -> Tuple[str, DmVersion]:
    """用当前游标读取最详细的达梦版本。

    返回 ``(展示文本, DmVersion)``。所有探测 SQL 失败时返回
    ``('', DmVersion(''))``，不抛出异常，避免阻断巡检连接流程。
    调用方负责保证 cursor 可用。
    """
    best_text = ''
    best_parsed = DmVersion(raw='')
    for sql in version_query_candidates():
        try:
            cursor.execute(sql)
            rows = cursor.fetchall() or []
        except Exception:
            continue
        text = ' | '.join(
            part for part in (_row_text(row) for row in rows) if part)
        if not text:
            continue
        parsed = parse_dm_version(text)
        if not parsed.major:
            continue
        if parsed.major == 8 and parsed.minor == 1:
            display = '%s (ID_CODE: %s)' % (parsed.display, text[:120]) \
                if text[0].isdigit() else text
            return display, parsed
        if parsed.major == 7 and (parsed.patch or parsed.build):
            return text, parsed
        if not best_text:
            best_text, best_parsed = text, parsed
        elif parsed.build and not best_parsed.build:
            best_text, best_parsed = text, parsed
    return best_text, best_parsed


def adapt_dm_sql(sql: object, version_text: object = '', key: str = '') -> str:
    """把 DM8 基线 SQL 改成当前达梦版本可执行的写法。

    只做已确认的轻量改写，不做危险的字符串拼接：

    * 会话状态过滤统一排除 ``IDLE``、``I``、``INACTIVE``；
    * ``V$PARAMETER.TYPE != 'R'`` 改为 ``TYPE <> 'READ ONLY'``。

    ``LIMIT`` 在 DM7/DM8 均可用，因此不做改写。其余版本差异交给
    ``dm_query_candidates()`` 在执行报错后回退。
    """
    text = str(sql or '')
    if not text:
        return text
    text = re.sub(
        r"(?i)\bSTATE\s*(?:<>|!=)\s*'IDLE'",
        "UPPER(STATE) NOT IN ('IDLE', 'I', 'INACTIVE')",
        text,
    )
    text = re.sub(
        r"(?i)\bTYPE\s*(?:<>|!=)\s*'R'",
        "TYPE <> 'READ ONLY'",
        text,
    )
    return text


def is_compatibility_error(error: object) -> bool:
    """是否是可通过候选 SQL 回退的结构类错误。"""
    low = str(error or '').lower()
    if not low:
        return False
    if any(marker in low for marker in _NON_COMPATIBILITY_MARKERS):
        return False
    return any(marker in low for marker in _COMPATIBILITY_ERROR_MARKERS)


def _sql_history_fallback(sql: str) -> Optional[str]:
    """V$SQL_HISTORY 不可用时，用 V$LONG_EXEC_SQLS 提供同名列。"""
    if 'V$SQL_HISTORY' not in sql.upper():
        return None
    limit_match = re.search(r'(?i)\bLIMIT\s+(\d+)', sql)
    limit = limit_match.group(1) if limit_match else '10'
    return (
        "SELECT N_RUNS AS EXEC_ID, "
        "'' AS SQL_ID, NULL AS SESS_ID, NULL AS SESS_SEQ, "
        "NULL AS TRX_ID, NULL AS THREAD_ID, "
        "SQL_TEXT AS TOP_SQL_TEXT, FINISH_TIME AS START_TIME, "
        "EXEC_TIME AS TIME_USED, 0 AS N_LOGIC_READ, 0 AS N_PHY_READ, "
        "0 AS AFFECTED_ROWS, 'Y' AS IS_OVER "
        "FROM V$LONG_EXEC_SQLS ORDER BY EXEC_TIME DESC LIMIT %s" % limit
    )


def _error_history_fallback(sql: str) -> Optional[str]:
    """错误信息视图在旧版本可能叫 V$RUNTIME_ERR_HISTORY。"""
    if 'V$ERR_INFO' not in sql.upper():
        return None
    limit_match = re.search(r'(?i)\bLIMIT\s+(\d+)', sql)
    limit = limit_match.group(1) if limit_match else '20'
    return 'SELECT * FROM V$RUNTIME_ERR_HISTORY LIMIT %s' % limit


def _archive_log_fallback(sql: str) -> Optional[str]:
    """部分版本使用 V$ARCH_FILE 暴露归档文件。"""
    if 'V$ARCHIVED_LOG' not in sql.upper():
        return None
    limit_match = re.search(r'(?i)\bLIMIT\s+(\d+)', sql)
    limit = limit_match.group(1) if limit_match else '20'
    return 'SELECT * FROM V$ARCH_FILE LIMIT %s' % limit


def _star_fallback(sql: str) -> Optional[str]:
    """简单单表查询的兜底：列不存在时改为 SELECT *。

    只处理没有 JOIN/UNION/子查询的简单语句；保留原 LIMIT，避免一次
    回退把大表全部拉回。
    """
    upper = sql.upper()
    if ' JOIN ' in upper or ' UNION ' in upper or ' FROM (' in upper:
        return None
    match = re.match(
        r'(?is)^\s*SELECT\s+(.*?)\s+FROM\s+([A-Za-z0-9_$#\.]+)', sql)
    if not match:
        return None
    table = match.group(2)
    limit_match = re.search(r'(?i)\bLIMIT\s+(\d+)', sql)
    suffix = ' LIMIT %s' % limit_match.group(1) if limit_match else ''
    return 'SELECT * FROM %s%s' % (table, suffix)


def dm_query_candidates(
    sql: object,
    version_text: object = '',
    key: str = '',
    failed_sql: object = '',
) -> List[str]:
    """返回结构类错误后的候选 SQL 链（去重，不含空串）。

    候选顺序：版本改写后的原 SQL -> 版本特定同义视图 -> 简单单表
    ``SELECT *`` 兜底。调用方只在 ``is_compatibility_error()`` 为真时
    使用，避免掩盖权限或连接问题。
    """
    adapted = adapt_dm_sql(sql, version_text, key)
    candidates: List[str] = []
    for item in (adapted, _sql_history_fallback(adapted),
                 _error_history_fallback(adapted),
                 _archive_log_fallback(adapted),
                 _star_fallback(adapted)):
        if item and item not in candidates:
            candidates.append(item)
    return candidates


def optional_view_reason(sql: object, key: str = '') -> Optional[str]:
    """可选视图缺失时返回“不适用”说明，否则返回 None。"""
    upper = str(sql or '').upper()
    for view, reason in _OPTIONAL_VIEW_REASONS.items():
        if view in upper:
            return reason
    return None
