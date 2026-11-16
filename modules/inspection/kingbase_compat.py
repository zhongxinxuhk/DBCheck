# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""KingbaseES V8/V9 巡检 SQL 的版本适配层。

KingbaseES 的版本号形如 ``V008R006C007B0012``，同时保留 PostgreSQL
兼容内核版本（例如 V8R6 的 ``server_version_num`` 为 12.x）。不同内核的
WAL 函数名、``pg_stat_replication`` 列名和 ``pg_stat_activity`` 字段并不
完全一致；本模块统一做：

* 版本识别：``version()`` 的 KingbaseES 版本 + ``server_version_num`` /
  ``server_version``，兼容 ``(Kingbase) 12.1`` 这类 ``sys_ctl -V`` 输出；
* PostgreSQL 9.x 的 ``xlog`` 函数与 ``*_location`` 复制列；
* PostgreSQL 9.5 以下缺少 ``usebypassrls``、9.6 以下缺少 ``wait_event``；
* KingbaseES 的 ``pg_*`` 视图/函数不可见时回退到 ``sys_*`` 同义对象；
* 只在结构类错误时尝试候选 SQL，权限、认证、连接、超时不掩盖。

KingbaseES V7 及更早版本采用非 PostgreSQL 兼容内核，不属于本入口可用的
V8/V9 模板范围；版本解析仍能识别其版本号，便于给出明确诊断。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, List, Optional, Tuple

from modules.inspection import postgresql_compat


_KINGBASE_RE = re.compile(
    r'(?<![A-Za-z0-9])V(?P<major>\d{1,3})R(?P<release>\d{1,3})'
    r'(?:C(?P<patch>\d{1,3}))?(?:M\d+[A-Za-z0-9_]*)?'
    r'(?:B(?P<build>\d{1,4}))?', re.IGNORECASE)
_KINGBASE_DOT_RE = re.compile(
    r'KingbaseES\s*V(?P<major>\d{1,3})\.(?P<release>\d{1,2})'
    r'(?:\.(?P<patch>\d{1,3}))?', re.IGNORECASE)
_KINGBASE_PLAIN_RE = re.compile(
    r'KingbaseES\s+(?P<major>\d{1,2})\.(?P<release>\d{1,2})'
    r'(?:\.(?P<patch>\d{1,3}))?', re.IGNORECASE)
_NUMERIC_RE = re.compile(r'^\d{5,6}$')

# KingbaseES 的兼容视图在某些部署模式（尤其 Oracle 模式）只暴露 sys_*。
# 只替换已知的系统对象，避免误改业务表名。
_SYS_NAMES = (
    'pg_catalog',
    'pg_available_extensions',
    'pg_postmaster_start_time',
    'pg_last_wal_receive_lsn',
    'pg_stat_user_indexes',
    'pg_stat_user_tables',
    'pg_stat_replication',
    'pg_stat_bgwriter',
    'pg_stat_activity',
    'pg_stat_database',
    'pg_database_size',
    'pg_tablespace_size',
    'pg_relation_size',
    'pg_current_wal_lsn',
    'pg_wal_lsn_diff',
    'pg_walfile_name',
    'pg_current_xlog_location',
    'pg_xlog_location_diff',
    'pg_xlogfile_name',
    'pg_last_xlog_receive_location',
    'pg_last_xlog_replay_location',
    'pg_is_in_recovery',
    'pg_available_extensions',
    'pg_settings',
    'pg_extension',
    'pg_tablespace',
    'pg_database',
    'pg_roles',
    'pg_user',
    'pg_size_pretty',
    'pg_locks',
)

_VERSION_NAMES = {
    8: 'KingbaseES V8',
    9: 'KingbaseES V9',
}


@dataclass(frozen=True)
class KingbaseVersion:
    """解析后的 KingbaseES 版本及其 PostgreSQL 兼容内核。"""

    raw: str
    major: int = 0
    release: int = 0
    patch: int = 0
    build: int = 0
    # 以下字段来自 server_version_num/server_version；为 0 时按版本映射推断。
    pg_major: int = 0
    pg_minor: int = 0
    pg_patch: int = 0
    pg_num: int = 0
    source: str = ''

    @property
    def family(self) -> str:
        if self.major:
            return 'kingbase-v%dr%d' % (self.major, self.release)
        if self.pg_major == 9:
            return 'kingbase-pg9.%d' % self.pg_minor
        if self.pg_major:
            return 'kingbase-pg%d' % self.pg_major
        return 'unknown'

    @property
    def display(self) -> str:
        if not self.major and not self.pg_major:
            return self.raw
        if self.major:
            label = _VERSION_NAMES.get(
                self.major, 'KingbaseES V%d' % self.major)
            if self.release:
                label += 'R%d' % self.release
            if self.patch:
                label += 'C%d' % self.patch
            if self.build:
                label += 'B%d' % self.build
        else:
            label = 'KingbaseES'
        major, minor = self.baseline
        if major:
            label += '（PostgreSQL %d.%d 兼容内核）' % (major, minor)
        return label

    @property
    def baseline(self) -> Tuple[int, int]:
        """返回用于 SQL 分支的 PostgreSQL 兼容内核 (major, minor)。"""
        if self.pg_major:
            return self.pg_major, self.pg_minor
        return _inferred_baseline(self.major, self.release)

    def at_least(self, major: int, release: int = 0) -> bool:
        return (self.major, self.release) >= (major, release)


def _inferred_baseline(major: int, release: int) -> Tuple[int, int]:
    """在无法读取 server_version 时按 KingbaseES 版本线推断内核。

    V8R6 的 ``sys_ctl -V`` 为 12.1；V8R2 为 PostgreSQL 9.6。V8R3 的
    兼容内核可能是 9.6/10，因此默认走旧名称，执行阶段再由候选链尝试
    新名称；V9 的模板字段与 12+ 一致。若实例能返回 server_version_num，
    始终以实际值为准。
    """
    if major <= 0:
        return 0, 0
    if major <= 7:
        return 9, 4
    if major == 8:
        if release <= 3:
            return 9, 6
        if release <= 5:
            return 10, 0
        return 12, 0
    if major >= 9:
        return 12, 0
    return 0, 0


def _extract_pg_version(raw: str) -> Tuple[int, int, int, int]:
    """从版本字符串中提取 PostgreSQL 内核版本。"""
    text = str(raw or '').strip()
    if not text:
        return 0, 0, 0, 0
    if _NUMERIC_RE.fullmatch(text):
        parsed = postgresql_compat.parse_postgresql_version(text)
        if parsed.major:
            return parsed.major, parsed.minor, parsed.patch, parsed.version_num
    # 去掉 KingbaseES 自己的 VxxxRxxx/Cxxx/Bxxx，避免把 8.6 当成内核。
    cleaned = _KINGBASE_RE.sub(' ', text)
    cleaned = _KINGBASE_DOT_RE.sub(' ', cleaned)
    cleaned = _KINGBASE_PLAIN_RE.sub(' ', cleaned)
    parsed = postgresql_compat.parse_postgresql_version(cleaned)
    if not parsed.major:
        return 0, 0, 0, 0
    return parsed.major, parsed.minor, parsed.patch, parsed.version_num


def parse_kingbase_version(version_text: object) -> KingbaseVersion:
    """解析 KingbaseES 版本号、``version()`` banner 或 PG 内核版本。"""
    raw = str(version_text or '').strip()
    if not raw:
        return KingbaseVersion(raw='')
    major = release = patch = build = 0
    source = ''
    match = _KINGBASE_RE.search(raw)
    if match:
        major = int(match.group('major'))
        release = int(match.group('release'))
        patch = int(match.group('patch') or 0)
        build = int(match.group('build') or 0)
        source = 'kingbase banner'
    else:
        match = _KINGBASE_DOT_RE.search(raw) or _KINGBASE_PLAIN_RE.search(raw)
        if match:
            major = int(match.group('major'))
            release = int(match.group('release'))
            patch = int(match.group('patch') or 0)
            source = 'kingbase version'
    pg_major, pg_minor, pg_patch, pg_num = _extract_pg_version(raw)
    if not major and pg_major:
        source = source or 'postgresql baseline'
    return KingbaseVersion(
        raw=raw, major=major, release=release, patch=patch, build=build,
        pg_major=pg_major, pg_minor=pg_minor, pg_patch=pg_patch,
        pg_num=pg_num, source=source)


def kingbase_family(version_text: object) -> str:
    return parse_kingbase_version(version_text).family


# ── 版本探测 SQL ────────────────────────────────────────────────────────

def version_query_candidates() -> List[Tuple[str, str]]:
    """返回 (SQL, 结果类型) 候选链；类型为 combined/banner/num/text。"""
    return [
        ("SELECT version() AS version_text, "
         "current_setting('server_version_num') AS version_num", 'combined'),
        ('SELECT version() AS version_text', 'banner'),
        ("SELECT current_setting('server_version_num') AS version_num", 'num'),
        ('SHOW server_version_num', 'num'),
        ("SELECT current_setting('server_version') AS version_text", 'text'),
        ('SHOW server_version', 'text'),
    ]


def _cell(row: Any, index: int = 0,
          keys: Tuple[str, ...] = ()) -> str:
    if isinstance(row, dict):
        for key in keys or ('version_text', 'version_num', 'version',
                            'server_version', 'server_version_num'):
            value = row.get(key)
            if value in (None, ''):
                value = row.get(key.upper())
            if value not in (None, ''):
                return str(value).strip()
        return ''
    if isinstance(row, (list, tuple)) and len(row) > index:
        if row[index] not in (None, ''):
            return str(row[index]).strip()
    return ''


def _rollback_cursor(cursor: Any) -> None:
    """探测失败后结束当前事务，避免后续候选全部落入 aborted 状态。"""
    targets = (
        cursor,
        getattr(cursor, 'connection', None),
        getattr(cursor, 'conn', None),
        getattr(cursor, '_connection', None),
        getattr(cursor, 'raw', None),
        getattr(getattr(cursor, 'connection', None), 'raw', None),
    )
    for target in targets:
        rollback = getattr(target, 'rollback', None)
        if callable(rollback):
            try:
                rollback()
                return
            except Exception:
                continue


def fetch_kingbase_version(
        cursor: Any) -> Tuple[str, KingbaseVersion]:
    """读取 KingbaseES banner 与 PostgreSQL 兼容内核版本。

    优先用一条 SQL 同时取 ``version()`` 和 ``server_version_num``；单条
    探测失败后回滚再试下一条，避免数据库事务进入 aborted 状态。所有探测
    失败时返回 ``('', 空版本)``，不阻断连接。
    """
    banner = ''
    pg_text = ''
    pg_num = ''
    for sql, kind in version_query_candidates():
        try:
            cursor.execute(sql)
            rows = cursor.fetchall() or []
        except Exception:
            _rollback_cursor(cursor)
            continue
        for row in rows:
            if kind == 'combined':
                banner = _cell(row, 0, ('version_text',)) or banner
                pg_num = _cell(row, 1, ('version_num',)) or pg_num
                continue
            value = _cell(row)
            if not value:
                continue
            if kind == 'banner':
                banner = value
            elif kind == 'num':
                pg_num = value
            else:
                pg_text = pg_text or value
        if (kind == 'combined' and banner and pg_num) or \
                (kind in ('banner', 'num', 'text') and
                 (banner and pg_num)):
            break

    if not banner and not pg_text and not pg_num:
        return '', KingbaseVersion(raw='')

    parsed = parse_kingbase_version(banner or pg_text or pg_num)
    pg = postgresql_compat.parse_postgresql_version(pg_num or pg_text or '')
    if not pg.major and banner:
        major, minor, patch, num = _extract_pg_version(banner)
        if major:
            pg = postgresql_compat.PostgreSqlVersion(
                raw=banner, major=major, minor=minor, patch=patch,
                version_num=num)
    if pg.major:
        parsed = replace(
            parsed, pg_major=pg.major, pg_minor=pg.minor,
            pg_patch=pg.patch, pg_num=pg.version_num,
            source=parsed.source or 'postgresql baseline')
    if not parsed.major and not parsed.pg_major:
        # 有些代理只返回 ``KingbaseES``，但 server_version 已足以分支；
        # 若两者都没有，按未知版本返回，调用方保持原 SQL。
        return (banner or pg_text or pg_num), parsed
    detail = banner or pg_text or pg_num
    if not detail:
        detail = 'KingbaseES (PostgreSQL %d.%d)' % parsed.baseline
    return detail, parsed


# ── SQL 版本/方言适配 ───────────────────────────────────────────────────

def _replace_words(text: str, old: str, new: str) -> str:
    return re.sub(r'(?i)\b%s\b' % re.escape(old), new, text)


def _adapt_legacy_activity(text: str, key: str,
                           baseline: Tuple[int, int]) -> str:
    """处理 9.6 以下 pg_stat_activity 的旧列。

    9.2 之前是 ``procpid``/``current_query``；这里只对仓库里明确使用
    这些列的键改写，避免误改业务 SQL。
    """
    if baseline >= (9, 2):
        return text
    if key == 'pg_long_xact':
        text = _replace_words(text, 'pid', 'procpid')
        text = _replace_words(text, 'query', 'current_query')
    return text


def _adapt_filter(text: str, baseline: Tuple[int, int]) -> str:
    """9.4 之前没有聚合 FILTER，改写模板中的连接数统计。"""
    if baseline >= (9, 4):
        return text
    return re.sub(
        r'(?is)count\(\*\)\s+FILTER\s*\(\s*WHERE\s+state\s*=\s*'
        r"'active'\s*\)",
        "sum(CASE WHEN state = 'active' THEN 1 ELSE 0 END)",
        text)


def _adapt_usebypassrls(text: str, key: str,
                        baseline: Tuple[int, int]) -> str:
    """9.5 之前 pg_user 没有 usebypassrls，移除该字段而不是伪造 false。"""
    if key != 'pg_users' or baseline >= (9, 5):
        return text
    return re.sub(r'(?is),\s*usebypassrls\b', '', text)


def _adapt_with_baseline(text: str, key: str,
                         baseline: Tuple[int, int]) -> str:
    if not text or not baseline[0]:
        return text
    version_hint = '%d.%d' % baseline
    if key != 'pg_checkpoint':
        # KingbaseES V9 仍保留 pg_stat_bgwriter 的检查点列，不能套用
        # PostgreSQL 17 的 pg_stat_checkpointer 拆分；其它 WAL/复制差异
        # 与 PostgreSQL 同版本一致。
        text = postgresql_compat.adapt_postgresql_sql(
            text, version_hint, key)
    text = _adapt_legacy_activity(text, key, baseline)
    text = _adapt_filter(text, baseline)
    text = _adapt_usebypassrls(text, key, baseline)
    return text


def _adapt_xid_age(text: str, key: str,
                   parsed: KingbaseVersion) -> str:
    """V9R2C16 起使用 64 位 XID，回卷风险分母同步调整。"""
    if key != 'pg_database_age':
        return text
    xid64 = (parsed.major > 9 or
             (parsed.major == 9 and
              (parsed.release > 2 or
               (parsed.release == 2 and parsed.patch >= 16))))
    if not xid64:
        return text
    return text.replace('2147483647', '9223372036854775807')


def adapt_kingbase_sql(sql: object, version_text: object = '',
                       key: str = '') -> str:
    """按 KingbaseES 的 PostgreSQL 兼容内核改写模板 SQL。"""
    text = str(sql or '').strip()
    if not text:
        return text
    parsed = parse_kingbase_version(version_text)
    if not parsed.baseline[0]:
        return text
    text = _adapt_with_baseline(text, key, parsed.baseline)
    return _adapt_xid_age(text, key, parsed)


def _to_sys_objects(sql: str) -> str:
    text = sql
    for name in sorted(_SYS_NAMES, key=len, reverse=True):
        text = re.sub(
            r'(?i)(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])' % re.escape(name),
            'sys_' + name[3:], text)
    return text


def kingbase_query_candidates(
    sql: object,
    version_text: object = '',
    key: str = '',
    failed_sql: object = '',
) -> List[str]:
    """返回 KingbaseES 结构类错误的候选 SQL 链（去重、保序）。"""
    original = str(sql or '').strip()
    if not original:
        return []
    parsed = parse_kingbase_version(version_text)
    baseline = parsed.baseline
    candidates: List[str] = []

    def add(candidate: str) -> None:
        candidate = str(candidate or '').strip()
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    primary = _adapt_with_baseline(original, key, baseline)
    add(primary)
    # KingbaseES 的 PG 兼容模式通常提供 pg_*，Oracle 模式或裁剪安装只
    # 保留 sys_*；同一语义对象作为第二候选链。
    add(_to_sys_objects(primary))

    # V8R3 等版本线可能同时保留旧/新 WAL 函数和复制列名。主候选之外
    # 再准备另一条内核分支，只有结构类错误才会触发。
    alternate = (9, 6) if baseline >= (10, 0) else (12, 0)
    alt_sql = _adapt_with_baseline(original, key, alternate)
    add(alt_sql)
    add(_to_sys_objects(alt_sql))

    failed = str(failed_sql or '').strip()
    if failed:
        add(failed)
        add(_to_sys_objects(failed))
    add(original)
    return candidates


# ── 错误分类与“不适用”说明 ─────────────────────────────────────────────

_COMPATIBILITY_MARKERS = (
    'does not exist', "doesn't exist", 'undefined column',
    'undefined function', 'undefined table', 'syntax error',
    'operator does not exist', 'could not determine data type',
    'invalid input syntax', '列不存在', '字段不存在', '表不存在',
    '视图不存在', '语法错误', '42703', '42p01', '42883', '42601',
)
_NON_COMPATIBILITY_MARKERS = (
    'permission denied', 'must be superuser', 'authentication failed',
    'password authentication', 'could not connect', 'connection refused',
    'connection timeout', 'timeout expired', 'canceling statement',
    '权限不足', '认证失败', '连接失败', '超时',
)


def is_kingbase_compatibility_error(error: object) -> bool:
    low = str(error or '').lower()
    if not low:
        return False
    if any(marker in low for marker in _NON_COMPATIBILITY_MARKERS):
        return False
    return any(marker in low for marker in _COMPATIBILITY_MARKERS)


def optional_feature_reason(sql: object, version_text: object = '',
                            cursor: Any = None) -> Optional[str]:
    """低版本缺失的可选字段返回“不适用”，不记为查询失败。"""
    upper = str(sql or '').upper()
    parsed = parse_kingbase_version(version_text)
    baseline = parsed.baseline
    if baseline[0] and baseline < (9, 6) and 'WAIT_EVENT' in upper:
        return ('不适用：该 KingbaseES 实例的 PostgreSQL 兼容内核低于 9.6，'
                '未提供 pg_stat_activity.wait_event 字段')
    if (baseline[0] and baseline < (9, 5) and 'USEBYPASSRLS' in upper
            and 'PG_USER' in upper):
        return ('不适用：该 KingbaseES 实例的 PostgreSQL 兼容内核低于 9.5，'
                'pg_user 未提供 usebypassrls 字段')
    return None
