# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""PostgreSQL 9.6–18 巡检 SQL 的版本适配层。

PostgreSQL 的 WAL 函数、复制视图列名和统计视图在各版本间有变化。这里
统一处理：

* 版本识别：``server_version_num`` / ``server_version`` / ``version()``；
* PostgreSQL 9.x 的 ``xlog`` 函数与 ``pg_xlog`` 归档目录；
* 9.x ``pg_stat_replication`` 的 ``*_location`` 列名；
* PostgreSQL 13 以下 ``pg_stat_statements`` 的 ``*_time`` 时间列；
* 9.6 以下没有 ``wait_event`` 的查询标记“不适用”；
* 只在结构类错误时尝试候选 SQL，权限、认证、连接、超时不掩盖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple


_VERSION_NAMES = {
    9: 'PostgreSQL 9',
    10: 'PostgreSQL 10',
    11: 'PostgreSQL 11',
    12: 'PostgreSQL 12',
    13: 'PostgreSQL 13',
    14: 'PostgreSQL 14',
    15: 'PostgreSQL 15',
    16: 'PostgreSQL 16',
    17: 'PostgreSQL 17',
    18: 'PostgreSQL 18',
}

_WAL_PAIRS = (
    ('pg_current_wal_lsn', 'pg_current_xlog_location'),
    ('pg_walfile_name', 'pg_xlogfile_name'),
    ('pg_wal_lsn_diff', 'pg_xlog_location_diff'),
    ('pg_last_wal_receive_lsn', 'pg_last_xlog_receive_location'),
    ('pg_last_wal_replay_lsn', 'pg_last_xlog_replay_location'),
)

_REPLICATION_PAIRS = (
    ('sent_lsn', 'sent_location'),
    ('write_lsn', 'write_location'),
    ('flush_lsn', 'flush_location'),
    ('replay_lsn', 'replay_location'),
)

_SLOW_TIME_PAIRS = (
    ('total_exec_time', 'total_time'),
    ('mean_exec_time', 'mean_time'),
    ('max_exec_time', 'max_time'),
    ('min_exec_time', 'min_time'),
    ('stddev_exec_time', 'stddev_time'),
)


@dataclass(frozen=True)
class PostgreSqlVersion:
    """解析后的 PostgreSQL 版本。"""

    raw: str
    major: int = 0
    minor: int = 0
    patch: int = 0
    version_num: int = 0

    @property
    def family(self) -> str:
        if not self.major:
            return 'unknown'
        if self.major >= 10:
            return 'postgresql-%d' % self.major
        return 'postgresql-%d.%d' % (self.major, self.minor)

    @property
    def display(self) -> str:
        if not self.major:
            return self.raw
        name = _VERSION_NAMES.get(self.major, 'PostgreSQL %d' % self.major)
        if self.major == 9:
            name = 'PostgreSQL 9.%d' % self.minor
        if self.major >= 10:
            return '%s (%d.%d)' % (name, self.major, self.patch)
        if self.patch:
            return '%s (%d.%d.%d)' % (
                name, self.major, self.minor, self.patch)
        return '%s (%d.%d)' % (name, self.major, self.minor)

    def at_least(self, major: int, minor: int = 0) -> bool:
        if self.major != major:
            return self.major > major
        return self.minor >= minor


_VERSION_RE = re.compile(
    r'(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(\d{1,3}))?(?!\d)')
_NUMERIC_VERSION_RE = re.compile(r'^\d{5,6}$')


def _from_version_num(raw: str) -> PostgreSqlVersion:
    num = int(raw)
    if num >= 100000:
        major = num // 10000
        patch = num % 100
        return PostgreSqlVersion(
            raw=raw, major=major, minor=0, patch=patch, version_num=num)
    major = num // 10000
    minor = (num // 100) % 100
    patch = num % 100
    return PostgreSqlVersion(
        raw=raw, major=major, minor=minor, patch=patch, version_num=num)


def parse_postgresql_version(version_text: object) -> PostgreSqlVersion:
    """解析 ``9.6.24``、``PostgreSQL 16.3 ...`` 或 ``160003``。"""
    raw = str(version_text or '').strip()
    if not raw:
        return PostgreSqlVersion(raw='')
    if _NUMERIC_VERSION_RE.fullmatch(raw):
        parsed = _from_version_num(raw)
        if 9 <= parsed.major <= 99:
            return parsed
        return PostgreSqlVersion(raw=raw)
    match = _VERSION_RE.search(raw)
    if not match:
        return PostgreSqlVersion(raw=raw)
    major = int(match.group(1))
    if not (9 <= major <= 99):
        return PostgreSqlVersion(raw=raw)
    second = int(match.group(2))
    third = int(match.group(3) or 0)
    if major >= 10:
        # PostgreSQL 10+ 的 server_version 是 10.23 这类“主版本.小版本”，
        # 第三段只在部分厂商/完整版本字符串中出现。
        patch = third or second
        return PostgreSqlVersion(
            raw=raw, major=major, minor=0, patch=patch,
            version_num=major * 10000 + patch)
    version_num = major * 10000 + second * 100 + third
    return PostgreSqlVersion(
        raw=raw, major=major, minor=second, patch=third,
        version_num=version_num)


def postgresql_family(version_text: object) -> str:
    return parse_postgresql_version(version_text).family


# ── 版本探测 SQL ────────────────────────────────────────────────────────

def version_query_candidates() -> List[Tuple[str, Optional[int], Optional[int]]]:
    """返回 (SQL, version_num 列下标, version_text 列下标) 候选链。"""
    return [
        ("SELECT current_setting('server_version_num') AS version_num, "
         "current_setting('server_version') AS version_text", 0, 1),
        ("SHOW server_version_num", 0, None),
        ("SHOW server_version", None, 0),
        ("SELECT version() AS version_text", None, 0),
    ]


def _cell(row: Any, index: Optional[int], *names: str) -> str:
    if isinstance(row, dict):
        lower = {str(key).lower(): value for key, value in row.items()}
        for name in names:
            value = lower.get(name.lower())
            if value not in (None, ''):
                return str(value).strip()
        return ''
    if isinstance(row, (list, tuple)) and index is not None:
        if index < len(row) and row[index] not in (None, ''):
            return str(row[index]).strip()
    return ''


def fetch_postgresql_version(cursor: Any) -> Tuple[str, PostgreSqlVersion]:
    """读取 PostgreSQL 版本；所有探测失败时返回 ``('', 空版本)``。"""
    best_text = ''
    best = PostgreSqlVersion(raw='')
    for sql, num_index, text_index in version_query_candidates():
        try:
            cursor.execute(sql)
            rows = cursor.fetchall() or []
        except Exception:
            continue
        for row in rows:
            num = _cell(row, num_index, 'version_num', 'server_version_num')
            text = _cell(row, text_index, 'version_text', 'version', 'server_version')
            if not num and not text:
                continue
            parsed = (parse_postgresql_version(num) if num
                      else parse_postgresql_version(text))
            if not parsed.major and text:
                parsed = parse_postgresql_version(text)
            if not parsed.major:
                continue
            raw = text or parsed.display
            if 'postgresql' not in raw.lower():
                raw = parsed.display
            return raw, parsed
    return best_text, best


# ── SQL 版本/方言适配 ───────────────────────────────────────────────────

def _replace_words(text: str, old: str, new: str) -> str:
    return re.sub(r'(?i)\b%s\b' % re.escape(old), new, text)


def _adapt_wal_names(text: str, modern: bool) -> str:
    # 元组统一写成 (现代名, 旧名)；现代版本反向替换旧名。
    pairs = (tuple((new, old) for old, new in _WAL_PAIRS)
             if modern else _WAL_PAIRS)
    for old, new in pairs:
        text = _replace_words(text, old, new)
    # 归档目录在 10 之前是 pg_xlog/archive_status。
    if modern:
        text = re.sub(r'(?i)\bpg_xlog(?=/)', 'pg_wal', text)
    else:
        text = re.sub(r'(?i)\bpg_wal(?=/)', 'pg_xlog', text)
    return text


def _adapt_replication_columns(text: str, modern: bool) -> str:
    if 'PG_STAT_REPLICATION' not in text.upper():
        return text
    pairs = (tuple((new, old) for old, new in _REPLICATION_PAIRS)
             if modern else _REPLICATION_PAIRS)
    for old, new in pairs:
        text = _replace_words(text, old, new)
    return text


def _adapt_checkpointer_columns(text: str, parsed: PostgreSqlVersion) -> str:
    """PostgreSQL 17 起检查点统计从 pg_stat_bgwriter 拆到独立视图。"""
    if parsed.major < 17 or 'PG_STAT_BGWRITER' not in text.upper():
        return text
    compatible_view = (
        'SELECT b.*, '
        '(SELECT num_timed FROM pg_stat_checkpointer) AS checkpoints_timed, '
        '(SELECT num_requested FROM pg_stat_checkpointer) AS checkpoints_req, '
        '(SELECT num_requested FROM pg_stat_checkpointer) AS checkpoints_requested, '
        '(SELECT write_time FROM pg_stat_checkpointer) AS checkpoint_write_time, '
        '(SELECT sync_time FROM pg_stat_checkpointer) AS checkpoint_sync_time, '
        '(SELECT buffers_written FROM pg_stat_checkpointer) AS buffers_checkpoint, '
        '(SELECT stats_reset FROM pg_stat_checkpointer) AS checkpointer_stats_reset '
        'FROM pg_stat_bgwriter AS b'
    )
    return re.sub(r'(?i)\bFROM\s+pg_stat_bgwriter\b',
                  'FROM (' + compatible_view + ') AS dbpulse_bgwriter', text)


def _adapt_slow_query_columns(text: str, parsed: PostgreSqlVersion) -> str:
    upper = text.upper()
    if 'PG_STAT_STATEMENTS' not in upper and 'EXEC_TIME' not in upper:
        return text
    modern = parsed.major >= 13
    pairs = (tuple((new, old) for old, new in _SLOW_TIME_PAIRS)
             if modern else _SLOW_TIME_PAIRS)
    for old, new in pairs:
        text = _replace_words(text, old, new)
    return text


def adapt_postgresql_sql(sql: object, version_text: object = '',
                         key: str = '') -> str:
    """按 PostgreSQL 版本改写旧版函数、列名和归档目录。"""
    text = str(sql or '').strip()
    if not text:
        return text
    parsed = parse_postgresql_version(version_text)
    if not parsed.major:
        return text
    modern = parsed.major >= 10
    text = _adapt_wal_names(text, modern)
    text = _adapt_replication_columns(text, modern)
    text = _adapt_checkpointer_columns(text, parsed)
    text = _adapt_slow_query_columns(text, parsed)
    return text


def postgresql_query_candidates(
    sql: object,
    version_text: object = '',
    key: str = '',
    failed_sql: object = '',
) -> List[str]:
    """返回结构类错误的候选 SQL 链（去重、保序）。"""
    adapted = adapt_postgresql_sql(sql, version_text, key)
    candidates: List[str] = [adapted]
    old = adapt_postgresql_sql(failed_sql or '', version_text, key)
    if old and old != adapted:
        candidates.append(old)
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
    return 'SELECT * FROM %s' % match.group(2)


# ── 错误分类与“不适用”说明 ─────────────────────────────────────────────

_COMPATIBILITY_MARKERS = (
    'does not exist', "doesn't exist", 'undefined column',
    'syntax error', 'operator does not exist', 'could not determine data type',
    'undefined function', 'invalid input syntax', '列不存在', '字段不存在',
    '语法错误', '不存在',
)
_NON_COMPATIBILITY_MARKERS = (
    'permission denied', 'must be superuser', 'authentication failed',
    'password authentication', 'could not connect', 'connection refused',
    'connection timeout', 'timeout expired', 'canceling statement',
    '权限不足', '认证失败', '连接失败', '超时',
)


def is_postgresql_compatibility_error(error: object) -> bool:
    low = str(error or '').lower()
    if not low:
        return False
    if any(marker in low for marker in _NON_COMPATIBILITY_MARKERS):
        return False
    return any(marker in low for marker in _COMPATIBILITY_MARKERS)


def optional_feature_reason(sql: object, version_text: object = '',
                            cursor: Any = None) -> Optional[str]:
    """旧版本不支持的可选字段返回“不适用”，避免误报失败。"""
    upper = str(sql or '').upper()
    parsed = parse_postgresql_version(version_text)
    if (parsed.major and (parsed.major < 9 or
                          (parsed.major == 9 and parsed.minor < 6))
            and 'WAIT_EVENT' in upper):
        return '不适用：PostgreSQL 9.6+ 才提供 pg_stat_activity.wait_event 字段'
    return None
