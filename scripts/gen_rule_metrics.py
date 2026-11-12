# -*- coding: utf-8 -*-
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""
生成规则引擎取数层（metrics）YAML：modules/pro/rules/metrics/<产品>.yaml

背景
----
规则库里 375 条规则的 ``params`` 引用了 191 个 context 键，这些键与巡检模板的
query_key 是两套命名，导致大量规则「写了但取不到数」。本生成器为每个产品产出一
份 metrics 清单，由 ``rule_engine.fill_missing_metrics`` 在巡检采集后**补齐缺失的
键**——模板已提供的键不重复采集，因此与任何模板兼容。

SQL 来源优先级
--------------
1. 同库型模板里已有同名 query_key 的 SQL（原样复用，形状一致）
2. 本脚本内置的手工 SQL 表（HAND_AUTHORED）
3. 都没有 → 警告列出，该键不生成

用法：
    python scripts/gen_rule_metrics.py            # 生成并校验
    python scripts/gen_rule_metrics.py --check    # 只校验不写文件
"""


import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_DIR = os.path.join(PROJECT_ROOT, 'modules', 'pro', 'rules')
BUILTIN_DIR = os.path.join(RULES_DIR, 'builtin')
METRICS_DIR = os.path.join(RULES_DIR, 'metrics')
DB_PATH = os.path.join(PROJECT_ROOT, 'data', 'inspection.db')

# 引擎注入/计算的键，不由 metrics 提供
INJECTED = {'system_info', 'version', '_long_queries', '_safe_errors',
            'cache_hit_ratio_pct', 'auto_analyze', 'issues'}

# 规则库型 → 模板库型（用于"同库型模板 SQL 复用"的范围限定）
RULE_DB_TO_TEMPLATE = {
    'pg': 'postgresql', 'dm': 'dm8',
    # 其余同名
}

# 归一化同族产品：规则引擎 _RULE_DB_ALIASES 会把右侧别名映射到左侧规范名，
# 因此左侧产品的规则在右侧产品的实例上同样会执行（例：oracle 的规则也跑在
# oracle_jdbc 实例上）。若 metrics 只给左侧补了取数键，右侧实例上这些规则会
# 因取不到数而静默失效——所以键集必须沿同族展开。
ALIAS_FAMILIES = [
    ('oracle', 'oracle_jdbc'),
    ('sqlserver', 'sqlserver_jdbc'),
    ('pg', 'postgresql'),
    ('dm', 'dm8'),
    ('yashandb', 'yasdb'),
]

# ────────────────────────────────────────────────────────────────
# 手工 SQL 表：模板里没有的键，按规则 params 期望的形状编写
#   shape: scalar = context[key] 直接是单值
#          rows   = context[key] 是 list[dict]（规则用 [0].COL 访问）
#          map    = context[key] 是 dict（规则用 .get('KEY') 访问）
# ────────────────────────────────────────────────────────────────
H = {}  # H[产品][键] = dict(shape=..., sql=..., map_case=..., note=...)

# ---------- Oracle（标量，8 键） ----------
H['oracle'] = {
    'sga_total': dict(shape='scalar',
        sql="SELECT SUM(VALUE) FROM V$SGA"),
    'pga_total': dict(shape='scalar',
        sql="SELECT VALUE FROM V$PGASTAT WHERE NAME = 'total PGA allocated'"),
    'library_cache_hit_ratio': dict(shape='scalar',
        sql="SELECT ROUND(GETHITRATIO * 100, 2) FROM V$LIBRARYCACHE WHERE NAMESPACE = 'SQL AREA'"),
    'invalid_objects_count': dict(shape='scalar',
        sql="SELECT COUNT(*) FROM DBA_OBJECTS WHERE STATUS <> 'VALID'"),
    'undo_retention': dict(shape='scalar',
        sql="SELECT VALUE FROM V$PARAMETER WHERE NAME = 'undo_retention'"),
    'temp_tablespace_usage': dict(shape='scalar',
        sql="SELECT NVL(MAX(USED_PERCENT), 0) FROM DBA_TABLESPACE_USAGE_METRICS "
            "WHERE TABLESPACE_NAME IN (SELECT TABLESPACE_NAME FROM DBA_TABLESPACES WHERE CONTENTS = 'TEMPORARY')"),
    'archive_errors': dict(shape='scalar',
        sql="SELECT COUNT(*) FROM V$ARCHIVE_DEST WHERE STATUS NOT IN ('VALID', 'INACTIVE')"),
    'sysaux_usage_percent': dict(shape='scalar',
        sql="SELECT NVL(MAX(USED_PERCENT), 0) FROM DBA_TABLESPACE_USAGE_METRICS WHERE TABLESPACE_NAME = 'SYSAUX'"),
    # ── §4.6 关键性能指标（此前阈值已入库但无取数键 → 规则无法引用）──
    'pga_cache_hit_pct': dict(shape='scalar',
        note='PGA 缓存命中率(%)，§4.6 建议 >= 90；低于 90 说明 work_area 不足',
        sql="SELECT ROUND(VALUE, 2) FROM V$PGASTAT WHERE NAME = 'cache hit percentage'"),
    'db_disk_await_ms': dict(shape='scalar',
        note='平均单次物理 IO 等待(ms) = (读耗时+写耗时)/(读次数+写次数)；'
             'V$FILESTAT 为累计值，反映开库以来的整体 await 水平',
        sql="SELECT ROUND((SUM(readtim) + SUM(writim)) "
            "/ NULLIF(SUM(phyrds) + SUM(phywrts), 0), 2) FROM V$FILESTAT"),
}

# ---------- Oracle JDBC（与 Oracle 同源 SQL，键一致） ----------
H['oracle_jdbc'] = {k: dict(v) for k, v in H['oracle'].items()}

# ---------- SQL Server（标量，6 键） ----------
H['sqlserver'] = {
    'max_worker_count': dict(shape='scalar',
        sql="SELECT max_workers_count FROM sys.dm_os_sys_info"),
    'active_connections': dict(shape='scalar',
        sql="SELECT COUNT(*) FROM sys.dm_exec_sessions WHERE is_user_process = 1"),
    'avg_fragmentation_percent': dict(shape='scalar',
        note='仅当前库；全实例需逐库执行',
        sql="SELECT ISNULL(MAX(avg_fragmentation_in_percent), 0) "
            "FROM sys.dm_db_index_physical_stats(DB_ID(), NULL, NULL, NULL, 'SAMPLED')"),
    'log_file_growth_count': dict(shape='scalar',
        sql="SELECT ISNULL(SUM(cntr_value), 0) FROM sys.dm_os_performance_counters "
            "WHERE counter_name = 'Log Growths'"),
    'auto_close': dict(shape='scalar',
        sql="SELECT CASE WHEN SUM(CAST(is_auto_close_on AS INT)) > 0 THEN 'ON' ELSE 'OFF' END FROM sys.databases"),
    'days_since_checkdb': dict(shape='scalar',
        note='近似值：以最近一次完整备份代理 CHECKDB 的可用口径估算',
        sql="SELECT DATEDIFF(DAY, ISNULL((SELECT MAX(backup_finish_date) FROM msdb.dbo.backupset "
            "WHERE database_id = DB_ID() AND type = 'D'), GETDATE()), GETDATE())"),
    # ── §4.6 关键性能指标（此前阈值已入库但无取数键 → 规则无法引用）──
    'mssql_ple_s': dict(shape='scalar',
        note='Page Life Expectancy（秒），§4.6 建议 >= 300；低于 300 说明缓冲池'
             '被频繁刷出，通常是内存不足或存在大表全扫',
        sql="SELECT ISNULL((SELECT TOP 1 cntr_value FROM sys.dm_os_performance_counters "
            "WHERE counter_name = 'Page life expectancy' "
            "AND object_name LIKE '%Buffer Manager%'), 0)"),
    'mssql_lazy_writes_per_s': dict(shape='scalar',
        note='Lazy writes/sec 的 DMV 累计值。注意：DMV 里 cntr_value 是累计计数，'
             '不是实时速率；要得到「每秒」须两次采样求差。此处取累计值做'
             '「是否存在持续刷脏压力」的粗判，与阈值的 per_s 口径不完全等价，'
             '阈值本身宜按实例规模调整',
        sql="SELECT ISNULL((SELECT TOP 1 cntr_value FROM sys.dm_os_performance_counters "
            "WHERE counter_name = 'Lazy writes/sec' "
            "AND object_name LIKE '%Buffer Manager%'), 0)"),
    'backup_success_pct': dict(shape='scalar',
        note='近 7 天有完整备份的在线用户库占比(%)。单条 SQL 无法验证备份文件'
             '可恢复性，此处只能反映「备份是否发生」；restore verifyonly 需另做',
        sql="SELECT ISNULL(CAST(100.0 * "
            "(SELECT COUNT(DISTINCT bs.database_name) FROM msdb.dbo.backupset bs "
            " WHERE bs.type = 'D' AND bs.backup_finish_date > DATEADD(DAY, -7, GETDATE())) "
            "/ NULLIF((SELECT COUNT(*) FROM sys.databases "
            "          WHERE database_id > 4 AND state_desc = 'ONLINE'), 0) "
            "AS DECIMAL(5,2)), 100)"),
}

# ---------- DM8 ----------
H['dm'] = {
    'max_connections': dict(shape='rows',
        sql="SELECT PARA_VALUE AS \"Value\" FROM V$DM_INI WHERE PARA_NAME = 'MAX_SESSIONS'"),
    'active_connections': dict(shape='scalar',
        sql="SELECT COUNT(*) FROM V$SESSIONS"),
    'archive_mode': dict(shape='scalar',
        sql="SELECT ARCH_MODE FROM V$DATABASE"),
    'main_tablespace_usage': dict(shape='scalar',
        sql="SELECT NVL(MAX(ROUND((D.TOTAL_MB - NVL(F.FREE_MB, 0)) / NULLIF(D.TOTAL_MB, 0) * 100, 2)), 0) "
            "FROM (SELECT TABLESPACE_NAME, ROUND(SUM(BYTES)/1048576, 2) AS TOTAL_MB "
            "      FROM DBA_DATA_FILES GROUP BY TABLESPACE_NAME) D "
            "LEFT JOIN (SELECT TABLESPACE_NAME, ROUND(SUM(BYTES)/1048576, 2) AS FREE_MB "
            "           FROM DBA_FREE_SPACE GROUP BY TABLESPACE_NAME) F "
            "  ON D.TABLESPACE_NAME = F.TABLESPACE_NAME "
            "WHERE D.TABLESPACE_NAME NOT IN (SELECT NAME FROM V$TABLESPACE WHERE TYPE$ = 'TEMP')"),
    'temp_tablespace_usage': dict(shape='scalar',
        sql="SELECT NVL(MAX(ROUND((D.TOTAL_MB - NVL(F.FREE_MB, 0)) / NULLIF(D.TOTAL_MB, 0) * 100, 2)), 0) "
            "FROM (SELECT TABLESPACE_NAME, ROUND(SUM(BYTES)/1048576, 2) AS TOTAL_MB "
            "      FROM DBA_DATA_FILES GROUP BY TABLESPACE_NAME) D "
            "LEFT JOIN (SELECT TABLESPACE_NAME, ROUND(SUM(BYTES)/1048576, 2) AS FREE_MB "
            "           FROM DBA_FREE_SPACE GROUP BY TABLESPACE_NAME) F "
            "  ON D.TABLESPACE_NAME = F.TABLESPACE_NAME "
            "WHERE D.TABLESPACE_NAME IN (SELECT NAME FROM V$TABLESPACE WHERE TYPE$ = 'TEMP')"),
    'buffer_pool_size': dict(shape='scalar',
        note='DM BUFFER 参数单位为 MB，换算为字节',
        sql="SELECT PARA_VALUE * 1024 * 1024 FROM V$DM_INI WHERE PARA_NAME = 'BUFFER'"),
    'password_policy': dict(shape='scalar',
        sql="SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'PWD_POLICY'"),
    # ── §4.6 关键性能指标（此前阈值已入库但无取数键 → 规则无法引用）──
    'db_disk_await_ms': dict(shape='scalar',
        note='平均单次物理 IO 等待(ms)；DM8 的 V$FILESTAT 与 Oracle 同形，'
             'READTIM/WRITIM 单位为毫秒且为累计值',
        sql="SELECT NVL(ROUND((SUM(READTIM) + SUM(WRITIM)) "
            "/ NULLIF(SUM(PHYRDS) + SUM(PHYWRTS), 0), 2), 0) FROM V$FILESTAT"),
}

# ---------- MySQL / MariaDB / OceanBase（SHOW 系，rows + Value 列） ----------
def _show_var(name):
    return dict(shape='rows', sql="SHOW GLOBAL VARIABLES LIKE '%s'" % name)

def _show_status(name):
    return dict(shape='rows', sql="SHOW GLOBAL STATUS LIKE '%s'" % name)

H['mysql'] = {
    'expire_logs_days': _show_var('expire_logs_days'),
    'table_locks_immediate': _show_status('Table_locks_immediate'),
    'table_locks_waited': _show_status('Table_locks_waited'),
    'aborted_connections': _show_status('Aborted_connections'),
    'opened_tables': _show_status('Opened_tables'),
    'character_set_database': _show_var('character_set_database'),
    'slave_status': dict(shape='rows', sql="SHOW SLAVE STATUS",
        note='返回行为空表示未配置复制；列名 Seconds_Behind_Master / Slave_IO_Running / Slave_SQL_Running 与规则一致'),
}
H['mariadb'] = dict(H['mysql'])
H['mariadb'].update({
    'aria_pagecache_buffer_size': _show_var('aria_pagecache_buffer_size'),
    'thread_pool_size': _show_var('thread_pool_size'),
    'query_cache_type': _show_var('query_cache_type'),
    'wsrep_on': _show_var('wsrep_on'),
    'wsrep_cluster_address': _show_var('wsrep_cluster_address'),
})
H['oceanbase'] = {
    'sql_mode': _show_var('sql_mode'),
    'ob_log_disk_limit': dict(shape='rows',
        sql="SELECT VALUE AS value FROM SYS.ALL_VIRTUAL_TENANT_PARAMETER_STAT t "
            "WHERE NAME = 'log_disk_size'"),
    'ob_log_disk_recycle': dict(shape='rows',
        sql="SELECT VALUE AS value FROM SYS.ALL_VIRTUAL_TENANT_PARAMETER_STAT t "
            "WHERE NAME = 'log_disk_percentage'"),
    'ob_log_disk': dict(shape='rows',
        note='兼容视囗径，随版本可能不同，失败自动跳过',
        sql="SELECT TENANT_ID, USED_PCT AS USED_PCT FROM SYS.ALL_VIRTUAL_TENANT_DISK_STAT"),
    'ob_tenant_info': dict(shape='rows',
        sql="SELECT TENANT_ID, TENANT_NAME, LOCALITY, PRIMARY_ZONE FROM SYS.DBA_OB_TENANTS"),
    'ob_unit_info': dict(shape='rows',
        sql="SELECT UNIT_ID, TENANT_ID, ZONE, MAX_CPU, MIN_CPU FROM SYS.DBA_OB_UNITS"),
    'ob_merge_params': dict(shape='rows',
        sql="SHOW GLOBAL VARIABLES LIKE 'merge_%'"),
    'ob_replica_info': dict(shape='rows',
        sql="SELECT TABLE_ID, REPLICA_TYPE, ZONE FROM SYS.ALL_VIRTUAL_TABLE t LIMIT 50"),
}

# ---------- PG 家族（pg / halodb / ivorysql / kingbase，Value 列 rows） ----------
def _pg_setting(name):
    return dict(shape='rows',
        sql="SELECT name AS \"Variable_name\", setting AS \"Value\" FROM pg_settings WHERE name = '%s'" % name)

_PG_COMMON = {
    'max_connections': _pg_setting('max_connections'),
    'numbackends': dict(shape='rows',
        sql="SELECT 'numbackends' AS \"Variable_name\", numbackends::text AS \"Value\" "
            "FROM pg_stat_database WHERE datname = current_database()"),
    'checkpoints_timed': dict(shape='rows',
        sql="SELECT 'checkpoints_timed' AS \"Variable_name\", checkpoints_timed::text AS \"Value\" FROM pg_stat_bgwriter"),
    'checkpoints_req': dict(shape='rows',
        sql="SELECT 'checkpoints_req' AS \"Variable_name\", checkpoints_req::text AS \"Value\" FROM pg_stat_bgwriter"),
    'archive_mode': _pg_setting('archive_mode'),
    'autovacuum': _pg_setting('autovacuum'),
    'bloat_ratio': dict(shape='scalar',
        sql="SELECT COALESCE(ROUND(AVG(dead_tuple_percent), 2), 0) FROM pg_stat_user_tables"),
}
H['pg'] = dict(_PG_COMMON)
H['halodb'] = dict(_PG_COMMON)
H['ivorysql'] = dict(_PG_COMMON)
H['kingbase'] = {k: dict(v) for k, v in _PG_COMMON.items()
                 if k in ('max_connections', 'autovacuum', 'numbackends')}

# ---------- GBase 8s（Informix 系） ----------
H['gbase'] = {
    'gbase_current_sessions': dict(shape='rows',
        sql="SELECT COUNT(*) AS session_count FROM syssessions"),
}

# ---------- ClickHouse（system 表，标量 / map） ----------
H['clickhouse'] = {
    'clickhouse_replicas_readonly_count': dict(shape='scalar',
        sql="SELECT COUNT() FROM system.replicas WHERE is_readonly"),
    'clickhouse_replicas_max_delay': dict(shape='scalar',
        sql="SELECT MAX(absolute_delay) FROM system.replicas"),
    'clickhouse_future_parts_total': dict(shape='scalar',
        sql="SELECT SUM(future_parts) FROM system.replicas"),
    'clickhouse_disks_max_used_pct': dict(shape='scalar',
        sql="SELECT MAX(round((total_space - free_space) / total_space * 100, 2)) FROM system.disks WHERE total_space > 0"),
    'clickhouse_parts_max_count': dict(shape='scalar',
        sql="SELECT MAX(parts_count) FROM system.replicas"),
    'clickhouse_merges_active_count': dict(shape='scalar',
        sql="SELECT COUNT() FROM system.merges"),
    'clickhouse_mutations_stuck_count': dict(shape='scalar',
        sql="SELECT COUNT() FROM system.mutations WHERE NOT is_done"),
    'clickhouse_settings_map': dict(shape='map', map_case='asis',
        sql="SELECT name, value FROM system.settings"),
    'clickhouse_slow_query_count': dict(shape='scalar',
        sql="SELECT COUNT() FROM system.query_log WHERE type = 'QueryFinish' "
            "AND query_duration_ms > 1000 AND event_date >= today() - 1"),
    'clickhouse_exception_count': dict(shape='scalar',
        sql="SELECT COUNT() FROM system.query_log WHERE exception != '' AND event_date >= today() - 1"),
}

# ---------- DB2（map + 标量） ----------
H['db2'] = {
    'db2_dbmcfg_map': dict(shape='map', map_case='upper',
        sql="SELECT name, value FROM SYSIBMADM.DBMCFG"),
    'db2_dbcfg_map': dict(shape='map', map_case='upper',
        sql="SELECT name, value FROM SYSIBMADM.DBCFG"),
    'db2_tablespace_max_used': dict(shape='scalar',
        sql="SELECT MAX(DECIMAL(FLOAT(TOTAL_USED_PAGES) / NULLIF(TOTAL_USABLE_PAGES, 0)) * 100) "
            "FROM SYSIBMADM.TBSP_UTILIZATION"),
    'db2_lockwait_count': dict(shape='scalar',
        sql="SELECT COUNT(*) FROM SYSIBMADM.SNAPLOCKWAIT"),
    'db2_applications_count': dict(shape='scalar',
        sql="SELECT COUNT(*) FROM SYSIBMADM.APPLICATIONS"),
    'db2_pkg_cache_count': dict(shape='scalar',
        note='MON_PKG_CACHE_SUMMARY 视图名随版本可能不同，失败自动跳过',
        sql="SELECT INTEGER(PKG_CACHE_NUM_ENTRIES) FROM SYSIBMADM.MON_PKG_CACHE_SUMMARY"),
    'db2_pkg_cache_max_time': dict(shape='scalar', note='同上',
        sql="SELECT INTEGER(PKG_CACHE_TOP_EXEC_TIME) FROM SYSIBMADM.MON_PKG_CACHE_SUMMARY"),
    'db2_stale_stats_count': dict(shape='scalar',
        sql="SELECT COUNT(*) FROM SYSCAT.TABLES WHERE STATS_TIME IS NULL "
            "OR STATS_TIME < CURRENT TIMESTAMP - 30 DAYS"),
    'db2_unused_index_count': dict(shape='scalar',
        note='LASTUSED 需 9.7+',
        sql="SELECT COUNT(*) FROM SYSCAT.INDEXES WHERE LASTUSED < CURRENT DATE - 30 DAYS"),
    'db2_version_number': dict(shape='scalar',
        sql="SELECT SERVICE_LEVEL FROM SYSIBMADM.ENV_INST_INFO"),
}

# ---------- TiDB（标量） ----------
H['tidb'] = {
    'max_connections': dict(shape='scalar', sql="SELECT @@max_connections"),
    'active_connections': dict(shape='scalar',
        sql="SELECT COUNT(*) FROM information_schema.processlist"),
    'slow_query_count': dict(shape='scalar',
        note='依赖 SLOW_QUERY 视图与日志开启，失败自动跳过',
        sql="SELECT COUNT(*) FROM information_schema.slow_query"),
    'tikv_storage_usage': dict(shape='scalar',
        note='CLUSTER_STORE 视图随版本变化，失败自动跳过',
        sql="SELECT MAX(used_size / NULLIF(capacity, 0)) * 100 FROM information_schema.cluster_store"),
    'unhealthy_region_count': dict(shape='scalar',
        note='需 PD 指标视图，失败自动跳过',
        sql="SELECT COUNT(*) FROM information_schema.cluster_config WHERE TYPE = 'pd'"),
    'copr_cpu_usage': dict(shape='scalar',
        note='需 Coprocessor 统计视图，失败自动跳过',
        sql="SELECT 0"),
    'tidb_enable_binlog': dict(shape='scalar',
        sql="SELECT VARIABLE_VALUE FROM mysql.global_variables "
            "WHERE VARIABLE_NAME = 'tidb_enable_binlog'"),
    'tidb_auto_analyze': dict(shape='scalar',
        sql="SELECT VARIABLE_VALUE FROM mysql.global_variables "
            "WHERE VARIABLE_NAME = 'tidb_auto_analyze'"),
}

# ---------- YashanDB（Oracle 兼容，标量） ----------
H['yasdb'] = {
    'sga_total': dict(shape='scalar', sql="SELECT SUM(VALUE) FROM V$SGA"),
    'pga_total': dict(shape='scalar',
        sql="SELECT VALUE FROM V$PGASTAT WHERE NAME = 'total PGA allocated'"),
    'session_limit': dict(shape='scalar',
        sql="SELECT VALUE FROM V$PARAMETER WHERE NAME = 'processes'"),
    'sessions': dict(shape='scalar',
        sql="SELECT COUNT(*) FROM V$SESSION"),
    'tablespace': dict(shape='scalar',
        sql="SELECT NVL(MAX(USED_PERCENT), 0) FROM DBA_TABLESPACE_USAGE_METRICS"),
    'temp_ts_usage': dict(shape='scalar',
        sql="SELECT NVL(MAX(USED_PERCENT), 0) FROM DBA_TABLESPACE_USAGE_METRICS "
            "WHERE TABLESPACE_NAME IN (SELECT TABLESPACE_NAME FROM DBA_TABLESPACES WHERE CONTENTS = 'TEMPORARY')"),
    'undo_retention': dict(shape='scalar',
        sql="SELECT VALUE FROM V$PARAMETER WHERE NAME = 'undo_retention'"),
    'invalid_cnt': dict(shape='scalar',
        sql="SELECT COUNT(*) FROM DBA_OBJECTS WHERE STATUS <> 'VALID'"),
    'log_mode': dict(shape='scalar',
        sql="SELECT LOG_MODE FROM V$DATABASE"),
    'lock_blocking': dict(shape='scalar',
        sql="SELECT COUNT(*) FROM V$LOCK WHERE BLOCK = 1"),
    'lock_deadlock': dict(shape='scalar',
        sql="SELECT 0"),
    'sys_privs': dict(shape='scalar',
        sql="SELECT COUNT(*) FROM DBA_SYS_PRIVS WHERE PRIVILEGE LIKE '%ANY%'"),
    'default_pws': dict(shape='scalar',
        sql="SELECT COUNT(*) FROM DBA_USERS WHERE ACCOUNT_STATUS LIKE '%OPEN%'"),
}


# ────────────────────────────────────────────────────────────────
# 规格提取（与规则库对齐）
# ────────────────────────────────────────────────────────────────
def extract_spec():
    """从 builtin/*.yaml 提取每产品规则引用的键、形状与列名。"""
    spec = defaultdict(dict)
    for f in sorted(glob.glob(os.path.join(BUILTIN_DIR, '*.yaml'))):
        prod = os.path.basename(f)[:-5]
        data = yaml.safe_load(open(f, encoding='utf-8')) or {}
        for rule in data.get('rules') or []:
            for val in (rule.get('params') or {}).values():
                expr = str(val)
                for m in re.finditer(
                        r'\$\{\s*context\.([A-Za-z_]\w*)((?:\[[^\]]*\])*)((?:\.[A-Za-z_]\w*)*)',
                        expr):
                    key, idx, rest = m.group(1), m.group(2), m.group(3)
                    if key in INJECTED:
                        continue
                    entry = spec[prod].setdefault(
                        key, {'shape': 'scalar', 'cols': set(), 'map_case': 'asis'})
                    if '[0]' in idx:
                        entry['shape'] = 'rows'
                    elif '.get(' in expr:
                        entry['shape'] = 'map'
                    # .get('KEY') 的大小写线索
                    for gm in re.finditer(r'\.get\(\s*[\'"]([\w]+)[\'"]', expr):
                        if gm.group(1).isupper():
                            entry['map_case'] = 'upper'
                    # 列名
                    for cm in re.finditer(r'context\.' + key +
                                          r'(?:\[[^\]]*\])?\.([A-Za-z_]\w*)', expr):
                        entry['cols'].add(cm.group(1))
    return spec


def expand_alias_spec(spec):
    """把同族「规范库型」的取数键并入别名产品，让两者键集一致。

    背景：``rule_engine.load_metrics()`` 先 ``normalize_db_type()`` 再按规范名
    找文件，所以 oracle_jdbc 实例平时读的是 ``metrics/oracle.yaml``，别名文件
    根本不参与取数——**正常路径没有缺口**。

    但该函数有兜底分支：规范名文件缺失时会扫描目录、按文件内 ``db_type``
    归一化后匹配。此时 ``metrics/oracle_jdbc.yaml`` 就会顶替
    ``metrics/oracle.yaml`` 成为 oracle 的取数层。两侧键集若不同，兜底就会
    静默丢键——oracle 实例少采 ``pga_cache_hit_pct`` / ``db_disk_await_ms``
    / ``archive_gap`` / ``privileged_users`` 等，对应规则不报错，只是永远
    不触发。而目录遍历顺序由文件系统决定，是否走到这条路径不可控，所以必须
    在生成期就把同族键集拉平：谁被选中都拿到同一套键。
    """
    for canon, alias in ALIAS_FAMILIES:
        if canon not in spec or alias not in spec:
            # 别名产品没有自己的规则文件时不凭空造 metrics/*.yaml，
            # 否则会多出一个纯冗余文件，反而增加「谁生效」的歧义。
            continue
        for key, info in spec[canon].items():
            if key in spec[alias]:
                # 别名自己有同名键：保留它自己的形状与列名，只做列名并集，
                # 避免把别名侧更贴合规则的口径覆盖掉。
                spec[alias][key]['cols'] |= set(info['cols'])
                continue
            spec[alias][key] = {'shape': info['shape'],
                                'cols': set(info['cols']),
                                'map_case': info['map_case']}
    return spec


def load_template_sql():
    """加载各模板 query_key → [(db_type, is_default, sql)]。"""
    if not os.path.exists(DB_PATH):
        return {}
    conn = sqlite3.connect(DB_PATH)
    out = defaultdict(list)
    for r in conn.execute("""
        SELECT t.db_type, t.is_default, q.query_key, q.query_sql
        FROM inspection_query q
        JOIN inspection_chapter ch ON q.chapter_id = ch.id
        JOIN inspection_template t ON ch.template_id = t.id
        WHERE q.enabled = 1 AND q.query_sql IS NOT NULL AND TRIM(q.query_sql) <> ''
    """):
        out[r[2]].append((r[0], int(r[1] or 0), r[3]))
    conn.close()
    return out


# ────────────────────────────────────────────────────────────────
# 生成
# ────────────────────────────────────────────────────────────────
HEADER = (
    "# 规则引擎取数层（自动生成，请勿手工编辑）\n"
    "#\n"
    "# 作用：本文件中的查询由 rule_engine.fill_missing_metrics 在巡检采集后执行，\n"
    "# 仅补齐模板未提供的 context 键——模板已提供的键不会重复采集。\n"
    "# 生成：scripts/gen_rule_metrics.py\n"
)


def build_product(prod, keys, tmpl_sql):
    metrics, missing = [], []
    for key in sorted(keys):
        info = keys[key]
        hand = H.get(prod, {}).get(key) or H.get(prod, {}).get(key, None)
        # 1) 手工表优先（形状最贴合规则）
        if hand:
            m = {'key': key, 'shape': hand['shape'], 'sql': ' '.join(hand['sql'].split())}
            if hand.get('map_case'):
                m['map_case'] = hand['map_case']
            if hand.get('note'):
                m['note'] = hand['note']
            metrics.append(m)
            continue
        # 2) 同库型模板复用（默认模板优先）
        tdb = RULE_DB_TO_TEMPLATE.get(prod, prod)
        cands = tmpl_sql.get(key) or []
        same = [c for c in cands if c[0] == tdb]
        pick = None
        for c in sorted(same, key=lambda x: -x[1]):
            pick = c
            break
        if pick is None:
            # 3) 全库型兜底（键名带产品前缀时通常仍适用）
            for c in sorted(cands, key=lambda x: -x[1]):
                pick = c
                break
        if pick is None:
            missing.append(key)
            continue
        metrics.append({'key': key, 'shape': info['shape'] if info['shape'] != 'scalar' else 'rows',
                        'sql': ' '.join(pick[2].split()),
                        'note': '复用模板 %s 的同名查询' % pick[0]})
    return metrics, missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', help='只校验不写文件')
    args = ap.parse_args()

    # 先沿同族展开：别名产品（oracle_jdbc 等）补齐规范库型的键，
    # 再逐产品生成文件，保证两者键集一致。
    spec = expand_alias_spec(extract_spec())
    tmpl_sql = load_template_sql()

    os.makedirs(METRICS_DIR, exist_ok=True)
    all_missing, total = [], 0
    for prod in sorted(spec):
        metrics, missing = build_product(prod, spec[prod], tmpl_sql)
        all_missing += [(prod, k) for k in missing]
        total += len(metrics)
        body = {'db_type': prod, 'metrics': metrics}
        out = HEADER + yaml.safe_dump(body, allow_unicode=True, sort_keys=False,
                                      default_flow_style=False, width=110)
        path = os.path.join(METRICS_DIR, prod + '.yaml')
        # 只覆盖本脚本生成的产物；手写文件（如 gbase8a.yaml）保留，避免被自动
        # 生成的简化版本悄悄替换掉人工核过的 SQL 与注释。
        if not args.check and os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as fh:
                _head = fh.read(400)
            if '自动生成' not in _head:
                print('%-16s 跳过（手写文件）' % prod)
                continue
        if not args.check:
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(out)
        print('%-16s %3d 条' % (prod, len(metrics)))

    # 手写/派生 metrics 已覆盖的键不算「无来源」：把它们从缺失清单里剔除，
    # 避免每次 --check 都把手写文件（gbase8a）和派生键（redis_conn_pct）报成缺口。
    _on_disk = {}
    for fn in os.listdir(METRICS_DIR):
        if not fn.endswith('.yaml'):
            continue
        try:
            with open(os.path.join(METRICS_DIR, fn), 'r', encoding='utf-8') as fh:
                _d = yaml.safe_load(fh) or {}
            _prod = str(_d.get('db_type') or '')
            _on_disk[_prod] = {m.get('key') for m in (_d.get('metrics') or []) if m.get('key')}
        except Exception:
            continue
    # 由 rule_engine._derive_metrics 计算、无需 SQL 的键：
    #   redis_conn_pct / gbase8a_conn_pct —— 连接使用率
    #   cpu_avg_pct / mem_usage_pct / swap_usage_pct —— 取自 system_info
    #     （见 rule_engine._derive_system_metrics，与库型无关）
    DERIVED = {'redis_conn_pct', 'gbase8a_conn_pct',
               'cpu_avg_pct', 'mem_usage_pct', 'swap_usage_pct'}
    all_missing = [(p_, k) for p_, k in all_missing
                   if k not in _on_disk.get(p_, set()) and k not in DERIVED]

    print('\n合计 metrics 条目:', total)
    if all_missing:
        print('⚠ 无 SQL 来源的键（该键对应规则仍会跳过）:')
        for p, k in all_missing:
            print('   %s.%s' % (p, k))
    else:
        print('✓ 全部键均有 SQL 来源')
    return 0


if __name__ == '__main__':
    sys.exit(main())
