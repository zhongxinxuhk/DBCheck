# -*- coding: utf-8 -*-
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
参数基线 query_sql 静态审查（不连库）。

用途
----
``inspection_baseline`` 里的 SQL 只有在真实实例上执行才知道对不对。参数名或视图名
写错时不会报错，只会返回空集 → 巡检记为 UNKNOWN，既不误报也查不出。历史经验：
DM8 的 ``BUFFER_SIZE`` / ``MAX_LOGIN_FAILURE`` 等条目就是这样静默失效了很久。

本脚本在**不连库**的前提下做能做的事，把可疑条目在接入前就挑出来：

A. ``FAIL`` 已登记为废弃（``DEPRECATED_PARAMS``）却仍是 ``enabled=1``
B. ``FAIL`` 同库型下 ``param_name`` 重复
C. ``WARN`` ``param_name`` 完全没有出现在 ``query_sql`` 里
           —— 要么 SQL 查错了对象，要么参数名与取数字段不一致，必查
D. ``WARN`` SQL 里找不到任何取数对象（FROM / SHOW / @@ / CALL / LIKE）
E. ``WARN`` SQL 含 ``<占位符>``、``{占位}`` 等未替换模板
F. ``INFO`` 取数对象不在该库型的已知视图清单内（可能是版本差异，需真库确认）

用法：
    python scripts/audit_baseline_sql.py
    python scripts/audit_baseline_sql.py --db-type dm8 --db-type gbase8a
    python scripts/audit_baseline_sql.py --include-disabled
"""


import argparse
import os
import re
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

DEFAULT_DB = os.path.join(PROJECT_ROOT, 'data', 'inspection.db')

from modules.inspection.business_baseline_seed import DEPRECATED_PARAMS  # noqa: E402

# 各库型已知可用的取数对象（小写）。只收录「确认存在」的视图/表；
# 不在清单内不算错，只记为 INFO 供真库确认。
KNOWN_OBJECTS = {
    'dm8': {
        'v$dm_ini', 'v$database', 'v$sessions', 'v$session', 'v$tablespace',
        'v$parameter', 'dba_data_files', 'dba_free_space', 'dba_users',
        'dba_profiles', 'dba_sys_privs', 'dba_role_privs', 'v$lock',
        'v$instance', 'v$thread', 'v$rlogfile', 'v$logmnr_logs', 'v$backup_info',
        'v$archived_log', 'v$dm_archivelog', 'sys.dba_profiles',
    },
    'oracle': {
        'v$parameter', 'v$database', 'v$instance', 'v$sga', 'v$pgastat',
        'v$librarycache', 'v$sqlarea', 'v$session', 'v$lock', 'v$tablespace',
        'dba_tablespace_usage_metrics', 'dba_data_files', 'dba_free_space',
        'dba_users', 'dba_profiles', 'dba_sys_privs', 'dba_role_privs',
        'dba_objects', 'dba_audit_trail', 'v$archive_dest', 'v$archived_log',
        'v$backup', 'v$rman_backup_job_details', 'v$resource_limit',
        'gv$instance', 'v$log', 'v$logfile', 'v$controlfile',
    },
    'sqlserver': {
        'sys.configurations', 'sys.databases', 'sys.dm_os_sys_info',
        'sys.dm_os_performance_counters', 'sys.dm_exec_sessions',
        'sys.dm_exec_requests', 'sys.dm_db_index_physical_stats',
        'sys.dm_os_wait_stats', 'sys.server_principals', 'sys.server_role_members',
        'sys.dm_hadr_availability_group_states', 'sys.dm_hadr_database_replica_states',
        'msdb.dbo.backupset', 'msdb.dbo.backupmediafamily', 'sys.master_files',
        'sys.dm_io_virtual_file_stats', 'sys.filegroups', 'sys.database_files',
        'sys.sql_logins', 'sys.dm_exec_query_stats',
    },
    'gbase8a': {
        'gbase', 'gbase_variables', 'information_schema.session_variables',
        'information_schema.global_variables', 'gbase_sysdb',
    },
}

# 取数对象抽取：FROM / JOIN 后的标识符、SHOW 后的对象、@@变量
_OBJ_RE = re.compile(
    r'\b(?:from|join)\s+([A-Za-z_][\w$#.]*)'
    r'|\bshow\s+(?:global\s+|session\s+)?(?:variables|status|engine|databases)',
    re.IGNORECASE,
)
_AT_RE = re.compile(r'@@([A-Za-z_]\w*)')
_TEMPLATE_RE = re.compile(r'<[A-Za-z_\u4e00-\u9fa5][^<>]{0,30}>|\{[A-Za-z_]\w*\}|\$\{[^}]+\}')

# 无 FROM 但依然有取数来源的表达式（系统函数 / 文档型命令）
_SOURCE_FUNC_RE = re.compile(
    r'\b(?:serverproperty|connectionproperty|is_srvrolemember|is_member|'
    r'scope_identity|getparameter|runcommand|admincommand|serverstatus|'
    r'db\.get\w+|current_setting|version)\s*\(', re.IGNORECASE)

# 引号字面量（用于判断「按名过滤」的查询里参数名是否对得上）
_QUOTED_RE = re.compile(r"'([^']{1,80})'")

# 只认「按名过滤」上下文：name / PARA_NAME / VARIABLE_NAME / RESOURCE_NAME ...
# 区分 `WHERE name = 'x'`（按名取数）与 `WHERE status IN ('FULL')`（按值过滤），
# 后者与 param_name 无关，不该参与比对。
_NAME_FILTER_RE = re.compile(
    r"(?:para_?name|variable_?name|setting_?name|resource_?name|\bname)"
    r"\s*(?:=|like)\s*'([^']{1,80})'", re.IGNORECASE)

# 非 SQL 查询语言的库型（如 MongoDB 用文档命令），跳过 SQL 形态类检查
NON_SQL_TYPES = {'mongodb', 'redis', 'redis-cluster'}

# 已确认的「逻辑参数名 ≠ 物理设置名」映射（版本适配，description_zh 已注明）。
# 这些不是错配：param_name 取运维熟悉的逻辑名，query_sql 按目标版本查物理设置。
# 若 query_sql 改去查别的东西，仍会被下面的比对重新抓出来。
DOCUMENTED_NAME_MAP = {
    ('clickhouse', 'max_concurrent_queries'): {
        'physical': 'max_concurrent_queries_for_all_users',
        'note': 'ClickHouse 26.7 拆分全局/单用户并发设置，逻辑名保留原语义',
    },
    ('clickhouse', 'max_server_memory_usage'): {
        'physical': 'max_memory_usage_for_all_queries',
        'note': '26.7 起全局查询内存上限改用该设置，逻辑名沿用旧称',
    },
}


def _norm(s):
    """归一化：去掉分隔符与括号并转小写，用于跨命名风格比对。

    ``max degree of parallelism`` / ``max_degree_of_parallelism`` /
    ``max server memory (MB)`` / ``ivorysql.oracle_compatibility``
    这类差异都应视为「同一个参数」。
    """
    return re.sub(r'[\s_\-.()\[\]/]+', '', str(s or '')).lower()


def _strip_noise(s):
    """去掉命名噪声：schema 前缀（a.b → b）与括号单位（(MB)）。"""
    s = str(s or '')
    if '.' in s and ' ' not in s:
        s = s.rsplit('.', 1)[-1]
    s = re.sub(r'\([^)]*\)', '', s)
    return _norm(s)


# 允许的参数名后缀差异：布尔标记与常见单位
_SUFFIX_NOISE = ('enabled', 'disabled', 'on', 'off', 'mb', 'gb', 'kb', 'ms', 'pct', 'sec')


def _norm_equiv(param, literal):
    """判断 param_name 与 SQL 按名过滤的字面量是否指同一个参数。

    容忍：schema 前缀、括号单位、``_on`` / ``_off`` / ``_mb`` 这类后缀差异；
    不容忍：``max_concurrent_queries`` vs
    ``max_concurrent_queries_for_all_users`` 这种**确实是两个不同参数**的差异。
    """
    a, b = _strip_noise(param), _strip_noise(literal)
    if not a or not b:
        return False
    if a == b:
        return True
    if b.startswith(a) or a.startswith(b):
        longer, shorter = (a, b) if len(a) > len(b) else (b, a)
        tail = longer[len(shorter):]
        if tail in _SUFFIX_NOISE:
            return True
    return False


def _has_source(sql):
    low = sql.lower()
    if re.search(r'\bfrom\b|\bshow\b|@@|\bcall\b|\bexec\b|\blike\b', low):
        return True
    if _SOURCE_FUNC_RE.search(sql):
        return True
    return False


def _name_filters(sql):
    """抽取 SQL 中「按名过滤」的字面量（WHERE name = 'x' / LIKE 'x'）。"""
    return [m.group(1) for m in _NAME_FILTER_RE.finditer(sql)]


def _objects(sql):
    """抽取 SQL 引用的取数对象（小写、去掉别名片段）。"""
    out = set()
    for m in _OBJ_RE.finditer(sql):
        obj = (m.group(1) or '').strip().strip(',;')
        if obj:
            out.add(obj.lower())
    for m in _AT_RE.finditer(sql):
        out.add('@@' + m.group(1).lower())
    return out


def audit(db_path=DEFAULT_DB, db_types=None, include_disabled=False):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    where = ["1=1"]
    args = []
    if db_types:
        where.append("db_type IN (%s)" % ','.join('?' for _ in db_types))
        args.extend(db_types)
    if not include_disabled:
        where.append("COALESCE(enabled, 1) = 1")

    cur.execute(
        "SELECT id, db_type, param_name, query_sql, operator, expected_value, "
        "risk_level, enabled FROM inspection_baseline WHERE %s "
        "ORDER BY db_type, param_name" % ' AND '.join(where), tuple(args))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    fails, warns, infos = [], [], []

    # B. 重复
    seen = {}
    for r in rows:
        key = (r['db_type'], r['param_name'])
        if key in seen:
            fails.append({**r, 'kind': 'DUP',
                          'msg': '与 id=%s 重复' % seen[key]})
        else:
            seen[key] = r['id']

    # A. 废弃但启用
    for r in rows:
        dep = (DEPRECATED_PARAMS.get(r['db_type']) or {}).get(r['param_name'])
        if dep:
            fails.append({**r, 'kind': 'DEPRECATED_ON',
                          'msg': '已登记废弃但 enabled=1：%s' % dep[0]})

    for r in rows:
        sql = (r.get('query_sql') or '').strip()
        pname = str(r.get('param_name') or '')
        is_sql = r['db_type'] not in NON_SQL_TYPES

        if not sql:
            warns.append({**r, 'kind': 'NO_SQL', 'msg': 'query_sql 为空'})
            continue

        # C. 「按名过滤」的查询里，参数名与 SQL 字面量对不上
        #    只在 SQL 确实用 `name = 'x'` / `LIKE 'x'` 按名取数时才判定，
        #    避免把「按值过滤」（WHERE status IN ('FULL')）误判成错名。
        if pname and is_sql:
            nfs = _name_filters(sql)
            if nfs and not any(_norm_equiv(pname, x) for x in nfs):
                documented = DOCUMENTED_NAME_MAP.get((r['db_type'], pname))
                if documented and any(_norm_equiv(documented['physical'], x)
                                      for x in nfs):
                    infos.append({**r, 'kind': 'DOCUMENTED_MAP',
                                  'msg': '逻辑参数名对应物理设置 %s（已登记）：%s' % (
                                      documented['physical'], documented['note'])})
                else:
                    warns.append({**r, 'kind': 'NAME_MISMATCH',
                                  'msg': '按名过滤的字面量 %s 与 param_name 不一致，'
                                         '会拿错对象的值做比对' % ', '.join(
                                             "'%s'" % x for x in nfs[:3])})
            elif not nfs and not re.search(re.escape(pname), sql, re.IGNORECASE):
                # 既无按名过滤，参数名也没在 SQL 里出现 → 疑似语义错配（低置信）
                infos.append({**r, 'kind': 'SEMANTIC_SUSPECT',
                              'msg': 'param_name 未出现在 SQL 且无按名过滤，'
                                     '请确认真实取数字段与判定语义是否一致'})

        # D. 无取数对象
        if is_sql and not _has_source(sql):
            warns.append({**r, 'kind': 'NO_SOURCE',
                          'msg': '未识别到取数对象（FROM / SHOW / @@ / 系统函数）'})

        # E. 未替换模板
        if _TEMPLATE_RE.search(sql):
            warns.append({**r, 'kind': 'TEMPLATE',
                          'msg': 'SQL 含未替换的占位符：%s' %
                                 _TEMPLATE_RE.search(sql).group(0)})

        # F. 未知对象
        known = KNOWN_OBJECTS.get(r['db_type'])
        if known:
            unknown = sorted(o for o in _objects(sql)
                             if o not in known and not o.startswith('@@'))
            if unknown:
                infos.append({**r, 'kind': 'UNKNOWN_OBJ',
                              'msg': '取数对象不在已知清单（需真库确认）：%s' %
                                     ', '.join(unknown)})

    return rows, fails, warns, infos


def main():
    ap = argparse.ArgumentParser(description='参数基线 query_sql 静态审查')
    ap.add_argument('--db', default=DEFAULT_DB, help='inspection.db 路径')
    ap.add_argument('--db-type', action='append', dest='db_types',
                    help='只审查指定库型，可重复')
    ap.add_argument('--include-disabled', action='store_true',
                    help='同时审查已停用条目')
    args = ap.parse_args()

    if not os.path.isfile(args.db):
        print('[ERROR] 数据库不存在: %s' % args.db)
        return 2

    rows, fails, warns, infos = audit(args.db, args.db_types,
                                      args.include_disabled)

    print('=' * 74)
    print('  参数基线 query_sql 静态审查')
    print('=' * 74)
    print('  审查条目 %d 条（%s）' % (
        len(rows), '含已停用' if args.include_disabled else '仅启用'))

    by_type = {}
    for r in rows:
        by_type[r['db_type']] = by_type.get(r['db_type'], 0) + 1
    print('  分布：%s' % '  '.join('%s=%d' % (k, v)
                                  for k, v in sorted(by_type.items())))

    if fails:
        print('\n  [FAIL] %d 项（必须修）' % len(fails))
        for f in fails:
            print('    %-10s %-34s %s' % (f['db_type'], f['param_name'], f['msg']))

    if warns:
        print('\n  [WARN] %d 项（接入前应逐条真库确认）' % len(warns))
        for w in warns:
            print('    %-10s %-34s %s' % (w['db_type'], w['param_name'], w['msg']))

    if infos:
        print('\n  [INFO] %d 项（取数对象待真库确认，多为版本差异）' % len(infos))
        for i in infos:
            print('    %-10s %-34s %s' % (i['db_type'], i['param_name'], i['msg']))

    print('\n  合计：FAIL %d / WARN %d / INFO %d' % (
        len(fails), len(warns), len(infos)))
    print('  提示：WARN/INFO 不阻断，但每接入一个新实例，请先手工执行对应 '
          'query_sql 确认返回非空。')
    print('=' * 74)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
