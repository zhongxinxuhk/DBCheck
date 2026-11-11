# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""规则引擎全链路验证：metrics 取数 → context 补齐 → 规则命中。

用可编排的假连接（按 SQL 特征返回预设行）替代真实数据库，验证：
  1. 各产品 get_enabled_rules 数量与归一化/去重是否正确；
  2. fill_missing_metrics 只补缺失键、形状转换（scalar/map/rows）是否正确；
  3. 补齐后规则能否真正命中（这是治理前 174 条"接线但不执行"的根因）；
  4. 模板已提供的键不被覆盖。
"""
import sys
sys.path.insert(0, '/Users/xuzhongxin/DBCheck')

from modules.pro.rule_engine import (
    RuleEngine, normalize_db_type, load_metrics, fill_missing_metrics,
    analyze_with_plugins, _derive_metrics,
)


class FakeCursor:
    """按 SQL 特征返回预设结果，模拟不同产品的系统视图。"""

    def __init__(self, canned):
        self.canned = canned
        self.description = None
        self._rows = []

    def execute(self, sql):
        key = None
        for sig, payload in self.canned.items():
            if sig in sql:
                key = sig
                break
        if key is None:
            raise RuntimeError('未编排的 SQL: ' + sql[:60])
        cols, rows = self.canned[key]
        self.description = [(c,) for c in cols]
        self._rows = rows

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeConn:
    def __init__(self, canned):
        self.canned = canned

    def cursor(self):
        return FakeCursor(self.canned)


print('=' * 78)
print('一、规则加载与归一化')
print('=' * 78)
eng = RuleEngine()
for db in ('oracle', 'sqlserver', 'dm8', 'gbase8a', 'gbase', 'mysql', 'postgresql', 'mariadb'):
    rules = eng.get_enabled_rules(db)
    files = sorted({r.get('_source', '?') for r in rules})
    print(f'  {db:12s} -> {len(rules):3d} 条  来源 {files}')

print()
print('归一化抽查: postgresql->%s, dm8->%s, oracle_jdbc->%s'
      % (normalize_db_type('postgresql'), normalize_db_type('dm8'), normalize_db_type('oracle_jdbc')))

print()
print('=' * 78)
print('二、metrics 取数与派生（Oracle）')
print('=' * 78)
canned_oracle = {
    'V$ARCHIVE_DEST': (['CNT'], [(0,)]),
    'DBA_OBJECTS': (['CNT'], [(7,)]),
    'V$LIBRARYCACHE': (['R'], [(91.5,)]),
    'V$PGASTAT': (['V'], [(3489660928,)]),
    'V$SGA': (['S'], [(8589934592,)]),
    'DBA_TABLESPACE_USAGE_METRICS': (['U'], [(91.2,)]),
    'V$PARAMETER': (['V'], [(900,)]),
}
ctx = {}  # 空 context：模拟模板完全没提供任何键
n = fill_missing_metrics('oracle', ctx, FakeConn(canned_oracle))
print(f'  补齐 {n} 项: {sorted(ctx)}')
issues = analyze_with_plugins('oracle', ctx)
print(f'  规则命中 {len(issues)} 条:')
for it in issues:
    print(f"    [{it.get('col2')}] {it.get('col1')}")

print()
print('=' * 78)
print('三、模板已提供的键不被覆盖')
print('=' * 78)
ctx2 = {'temp_tablespace_usage': 42}
n2 = fill_missing_metrics('oracle', ctx2, FakeConn(canned_oracle))
print(f'  补齐 {n2} 项；temp_tablespace_usage 仍为 {ctx2["temp_tablespace_usage"]}（应为 42，不被 91.2 覆盖）')

print()
print('=' * 78)
print('四、GBase 8a MPP（全新规则，10 条）')
print('=' * 78)
canned_gbase = {
    'COUNT(*) FROM information_schema.PROCESSLIST': (['C'], [(180,)]),
    '@@max_connections': (['M'], [(200,)]),
    "COMMAND <> 'Sleep'": (['C'], [(3,)]),
    "COMMAND = 'Sleep'": (['C'], [(2,)]),
    'NOT EXISTS (SELECT': (['C'], [(12,)]),
    'DEFAULT_CHARACTER_SET_NAME NOT IN': (['C'], [(1,)]),
    'MAX(DATA_LENGTH)': (['M'], [(184320.5,)]),
    'SUM(DATA_LENGTH)': (['M'], [(6291456.0,)]),
    "TABLE_TYPE = 'BASE TABLE' AND TABLE_SCHEMA": (['C'], [(61000,)]),
    'COUNT(*) FROM information_schema.SCHEMATA': (['C'], [(63,)]),
}
gctx = {}
gn = fill_missing_metrics('gbase8a', gctx, FakeConn(canned_gbase))
print(f'  补齐 {gn} 项')
print(f'  派生 conn_pct = {gctx.get("gbase8a_conn_pct")}%（180/200，应为 90.0）')
gissues = analyze_with_plugins('gbase8a', gctx)
print(f'  规则命中 {len(gissues)} 条（10 条规则中预期 9 条命中，conn_pct=90 不触发 >90）:')
for it in gissues:
    print(f"    [{it.get('col2')}] {it.get('col1')}")

print()
print('=' * 78)
print('五、SQL Server / DM8 规则命中')
print('=' * 78)
canned_mssql = {
    'is_user_process = 1': (['C'], [(420,)]),
    'is_auto_close_on AS INT': (['S'], [('ON',)]),
    'avg_fragmentation_in_percent': (['F'], [(62.5,)]),
    'DATEDIFF(DAY, ISNULL': (['D'], [(21,)]),
    'Log Growths': (['G'], [(35,)]),
    'max_workers_count': (['W'], [(512,)]),
}
mctx = {}
mn = fill_missing_metrics('sqlserver', mctx, FakeConn(canned_mssql))
missues = analyze_with_plugins('sqlserver', mctx)
print(f'  补齐 {mn} 项 | 规则命中 {len(missues)} 条')
for it in missues:
    print(f"    [{it.get('col2')}] {it.get('col1')}")

print()
print('=' * 78)
print('六、失败隔离：单条 SQL 报错不影响其它键')
print('=' * 78)


class FlakyConn:
    def cursor(self):
        class C:
            description = None

            def execute(self, sql):
                raise RuntimeError('模拟版本不支持的视图')

            def fetchall(self):
                return []

            def close(self):
                pass
        return C()


broken = {}
ret = fill_missing_metrics('oracle', broken, FlakyConn())
print(f'  全失败时返回 {ret}，未抛异常 ✓（已补键: {sorted(broken)}）')
