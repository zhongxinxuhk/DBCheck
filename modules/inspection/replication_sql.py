# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""MySQL 系复制状态语句的版本自适应。

背景
----
MySQL 8.4.0 起移除了带 ``MASTER`` / ``SLAVE`` 的复制命令：

* ``SHOW MASTER STATUS``      -> ``SHOW BINARY LOG STATUS``（8.4.0+）
* ``SHOW SLAVE STATUS``       -> ``SHOW REPLICA STATUS``（8.0.22+ 提供，8.4 成为唯一语法）

而 MySQL 5.5/5.6/5.7、8.0.0-8.0.21 只认旧语法；MariaDB 在 10.5 起提供
``SHOW REPLICA STATUS`` 别名，但 ``SHOW SLAVE STATUS`` 仍在全部已发布版本可用。
巡检模板是静态 SQL，无法同时满足所有版本，因此运行时按 ``SELECT VERSION()``
的返回值选择语句，并在 1064/语法类错误时按候选链回退。

另外 ``SHOW REPLICA STATUS`` 的结果列已改为中性命名（``Source_Host``、
``Replica_IO_Running``、``Seconds_Behind_Source`` 等），旧代码读取
``Master_Host`` / ``Slave_IO_Running`` / ``Seconds_Behind_Master`` 会拿不到值，
本模块同时提供读取辅助函数。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# 巡检模板中与复制状态相关的查询键
REPLICATION_KEYS = frozenset({
    'master_status',
    'replication_lag',
    'slave_io_running',
    'slave_status',
    'repl_lag_detail',
    'repl_channels',
    'master_binlog',
    'replica_status',
})

# 语法类错误中可安全回退到同义语句的标识
_SYNTAX_ERROR_MARKERS = (
    'error in your sql syntax',
    'sqlsyntaxerrorexception',
    'syntax error',
    'sql语法',
    '语法错误',
    'unknown command',
    'unknown statement',
    '1064',
    '1149',
    '42000',
    'mariadb 1064',
)

_FLAVOR_MARKERS = (
    ('mariadb', 'mariadb'),
    ('tidb', 'tidb'),
    ('percona', 'percona'),
    ('oceanbase', 'oceanbase'),
    ('gbase', 'gbase'),
    ('tdsql', 'tdsql'),
)


def parse_version(version_text: str) -> Tuple[str, Tuple[int, int, int]]:
    """解析版本字符串为 ``(flavor, (major, minor, patch))``。

    无法解析版本号时返回 ``('', (0, 0, 0))``，调用方按最保守的旧语法处理。
    """
    text = str(version_text or '')
    low = text.lower()
    flavor = ''
    for marker, name in _FLAVOR_MARKERS:
        if marker in low:
            flavor = name
            break
    if not flavor:
        flavor = 'mysql'
    match = re.search(r'(\d+)\.(\d+)(?:\.(\d+))?', text)
    if not match:
        return flavor, (0, 0, 0)
    return flavor, (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3) or 0),
    )


def _at_least(version: Tuple[int, int, int], target: Tuple[int, int, int]) -> bool:
    return version >= target


def master_status_sql(flavor: str, version: Tuple[int, int, int]) -> str:
    """返回主库 Binlog 位点查询语句。"""
    if flavor in ('mysql', 'percona', 'tidb', 'oceanbase'):
        if _at_least(version, (8, 4, 0)) or version[0] >= 9:
            return 'SHOW BINARY LOG STATUS'
    return 'SHOW MASTER STATUS'


def replica_status_sql(flavor: str, version: Tuple[int, int, int]) -> str:
    """返回从库复制状态查询语句。

    MariaDB 统一用 ``SHOW SLAVE STATUS``：10.3-11.x 全部可用，且结果列保持
    ``Slave_*`` / ``Seconds_Behind_Master``，下游解析无需版本分支；若未来版本
    移除旧语法，``replication_query_candidates()`` 会自动回退到 REPLICA 语法。
    """
    if flavor in ('mysql', 'percona', 'oceanbase'):
        if _at_least(version, (8, 0, 22)):
            return 'SHOW REPLICA STATUS'
    return 'SHOW SLAVE STATUS'


def replication_sql_for(
    db_type: str,
    version_text: str,
    key: str,
    log_bin_enabled: Optional[bool] = None,
) -> Optional[str]:
    """按库种/版本/查询键返回应执行的复制状态 SQL。

    Args:
        db_type: 巡检库种（mysql / mariadb / percona / tdsqlc_mysql 等）
        version_text: ``SELECT VERSION()`` 的原始返回值
        key: 巡检模板查询键
        log_bin_enabled: 已探测到的 log_bin 状态；False 时主库位点直接给出说明行

    Returns:
        SQL 字符串；该库种不适用复制状态时返回 ``None``。
    """
    _ = db_type  # 版本字符串已包含 flavor，保留参数便于调用方语义清晰
    flavor, version = parse_version(version_text)
    if key in ('master_status', 'master_binlog'):
        if log_bin_enabled is False:
            return "SELECT '二进制日志未启用（log_bin=OFF），无 Binlog 位点可展示' AS 说明"
        return master_status_sql(flavor, version)
    if key in ('replication_lag', 'slave_io_running', 'slave_status',
               'repl_lag_detail', 'replica_status'):
        return replica_status_sql(flavor, version)
    if key == 'repl_channels':
        # 通道信息只有 REPLICA/SLAVE STATUS 带 Channel_Name；沿用同一选择。
        return replica_status_sql(flavor, version)
    return None


def is_replication_key(key: str) -> bool:
    return str(key or '').lower() in REPLICATION_KEYS


def is_syntax_error(exc: object) -> bool:
    """判断异常是否为可回退的语法/未知命令错误。"""
    text = str(exc or '').lower()
    if not text:
        return False
    return any(marker in text for marker in _SYNTAX_ERROR_MARKERS)


def replication_query_candidates(
    db_type: str,
    version_text: str,
    key: str,
    failed_sql: str = '',
    log_bin_enabled: Optional[bool] = None,
) -> List[str]:
    """返回复制状态查询的候选链（首选语法 + 兼容回退）。

    候选链会包含新旧两套语法，确保版本字符串被代理/中间件改写时仍可执行。
    """
    preferred = replication_sql_for(
        db_type, version_text, key, log_bin_enabled=log_bin_enabled)
    if preferred is None:
        return []
    if key in ('master_status', 'master_binlog'):
        candidates = [preferred, 'SHOW BINARY LOG STATUS', 'SHOW MASTER STATUS']
    else:
        candidates = [
            preferred,
            'SHOW REPLICA STATUS',
            'SHOW SLAVE STATUS',
        ]
    result: List[str] = []
    seen = {str(failed_sql or '').strip().rstrip(';').strip().lower()}
    for sql in candidates:
        normalized = sql.strip().rstrip(';').strip()
        if normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        result.append(normalized)
    return result


# ── 结果列兼容读取 ──────────────────────────────────────────────

_REPLICA_FIELD_ALIASES = {
    'seconds_behind_master': ('Seconds_Behind_Master', 'Seconds_Behind_Source'),
    'seconds_behind_source': ('Seconds_Behind_Source', 'Seconds_Behind_Master'),
    'slave_io_running': ('Slave_IO_Running', 'Replica_IO_Running'),
    'replica_io_running': ('Replica_IO_Running', 'Slave_IO_Running'),
    'slave_sql_running': ('Slave_SQL_Running', 'Replica_SQL_Running'),
    'replica_sql_running': ('Replica_SQL_Running', 'Slave_SQL_Running'),
    'master_host': ('Master_Host', 'Source_Host'),
    'source_host': ('Source_Host', 'Master_Host'),
    'master_user': ('Master_User', 'Source_User'),
    'source_user': ('Source_User', 'Master_User'),
    'master_log_file': ('Master_Log_File', 'Source_Log_File'),
    'source_log_file': ('Source_Log_File', 'Master_Log_File'),
    'read_master_log_pos': ('Read_Master_Log_Pos', 'Read_Source_Log_Pos'),
    'read_source_log_pos': ('Read_Source_Log_Pos', 'Read_Master_Log_Pos'),
    'relay_master_log_file': ('Relay_Master_Log_File', 'Relay_Source_Log_File'),
    'relay_source_log_file': ('Relay_Source_Log_File', 'Relay_Master_Log_File'),
    'exec_master_log_pos': ('Exec_Master_Log_Pos', 'Exec_Source_Log_Pos'),
    'exec_source_log_pos': ('Exec_Source_Log_Pos', 'Exec_Master_Log_Pos'),
    'slave_io_state': ('Slave_IO_State', 'Replica_IO_State'),
    'replica_io_state': ('Replica_IO_State', 'Slave_IO_State'),
    'slave_sql_running_state': ('Slave_SQL_Running_State', 'Replica_SQL_Running_State'),
    'replica_sql_running_state': ('Replica_SQL_Running_State', 'Slave_SQL_Running_State'),
    'master_server_id': ('Master_Server_Id', 'Source_Server_Id'),
    'source_server_id': ('Source_Server_Id', 'Master_Server_Id'),
    'master_uuid': ('Master_UUID', 'Source_UUID'),
    'source_uuid': ('Source_UUID', 'Master_UUID'),
}


def replica_field(row: Dict[str, object], name: str, default: object = '') -> object:
    """从复制状态行读取字段，兼容 ``Master_*``/``Source_*`` 与
    ``Slave_*``/``Replica_*`` 两套命名（大小写不敏感）。"""
    if not isinstance(row, dict):
        return default
    lookup = {str(k).lower(): v for k, v in row.items()}
    for candidate in _REPLICA_FIELD_ALIASES.get(str(name).lower(), (name,)):
        value = lookup.get(str(candidate).lower())
        if value is not None:
            return value
    return default


def replication_lag_seconds(row: Dict[str, object]) -> Optional[int]:
    """读取复制延迟秒数；NULL 表示复制线程未运行/无延迟数据。"""
    value = replica_field(row, 'Seconds_Behind_Source', None)
    if value is None:
        value = replica_field(row, 'Seconds_Behind_Master', None)
    if value is None:
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def replication_threads(row: Dict[str, object]) -> Tuple[str, str]:
    """返回 ``(IO线程状态, SQL线程状态)``，兼容新旧列名。"""
    io = str(replica_field(row, 'Replica_IO_Running', '') or '')
    sql = str(replica_field(row, 'Replica_SQL_Running', '') or '')
    return io, sql


def is_mysql_family(db_type: str) -> bool:
    """库种是否使用 MySQL 复制语义。"""
    text = str(db_type or '').lower()
    return any(token in text for token in (
        'mysql', 'mariadb', 'percona', 'tdsql', 'oceanbase', 'gbase8a'))
