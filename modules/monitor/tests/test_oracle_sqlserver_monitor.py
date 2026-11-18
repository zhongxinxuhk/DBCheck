# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Oracle / SQL Server 监控大屏接入回归测试（纯映射/解析/分派）。"""

import sys
import types

import modules.monitor.queries as queries
from modules.monitor import screen_metrics
from modules.monitor.engine import MonitorEngine


class FakeEngine:
    def __init__(self, mapping):
        self.mapping = mapping
        self.sqls = []

    def run_query(self, instance_id, sql):
        self.sqls.append(sql)
        return self.mapping.get(sql, [])


def test_oracle_sqlserver_templates_registered():
    assert queries.normalize_db_type('oracle_jdbc') == 'oracle'
    assert queries.normalize_db_type('sqlserver_jdbc') == 'sqlserver'

    for key in ('oracle', 'sqlserver'):
        assert key in queries.SLOW_QUERY_TEMPLATES
        assert key in queries.CONNECTION_TEMPLATES
        assert key in queries.MAX_CONN_QUERY_SQL
        assert key in queries.MAX_CONNECTION_DEFAULTS

    assert "NAME = 'sessions'" in queries.MAX_CONN_QUERY_SQL['oracle']

    assert screen_metrics.db_family('oracle') == 'oracle'
    assert screen_metrics.db_family('oracle_jdbc') == 'oracle'
    assert screen_metrics.db_family('sqlserver') == 'sqlserver'
    assert screen_metrics.db_family('sqlserver_jdbc') == 'sqlserver'


def test_oracle_screen_metrics_parse():
    engine = FakeEngine({
        screen_metrics.ORACLE_STAT_SQL: [
            {'name': 'execute count', 'value': '120'},
            {'name': 'user commits', 'value': '30'},
            {'name': 'user rollbacks', 'value': '2'},
            {'name': 'consistent gets', 'value': '1000'},
            {'name': 'db block gets', 'value': '200'},
            {'name': 'physical reads', 'value': '40'},
        ],
        screen_metrics.ORACLE_TBS_SQL: [
            {'name': 'USERS', 'total_mb': '100', 'max_mb': '200', 'free_mb': '50'},
        ],
        screen_metrics.ORACLE_REPL_SQL: [
            {'name': 'transport lag', 'value': '+00 00:00:02'},
            {'name': 'apply lag', 'value': '+00 00:00:07'},
        ],
        screen_metrics.ORACLE_LOCKS_SQL: [{'n': 4}],
        screen_metrics.ORACLE_IDENTITY_SQL: [{
            'database_role': 'PRIMARY', 'db_unique_name': 'ORCL_SITE1',
            'name': 'ORCL', 'open_mode': 'READ WRITE',
        }],
        screen_metrics.ORACLE_INSTANCE_SQL: [{
            'instance_name': 'orcl1', 'instance_number': 1, 'host_name': 'db1',
        }],
        screen_metrics.ORACLE_DEST_SQL: [{
            'db_unique_name': 'ORCL_SITE2', 'status': 'VALID',
        }],
    })

    counters, extras = screen_metrics.collect_extra(engine, 'ora-1', 'oracle')

    assert counters['exec_count'] == 120
    assert counters['commits'] == 30
    assert counters['rollbacks'] == 2
    assert counters['buf_reads'] == 1200
    assert counters['buf_disk'] == 40
    assert extras['tbs'][0]['name'] == 'USERS'
    assert extras['tbs'][0]['free_pct'] == 75.0
    assert extras['transport_lag_s'] == 2
    assert extras['apply_lag_s'] == 7
    assert extras['repl_lag_s'] == 7
    assert extras['lock_waits'] == 4
    assert extras['role'] == 'PRIMARY'
    assert extras['db_unique_name'] == 'ORCL_SITE1'
    assert extras['dests'] == ['ORCL_SITE2']


def test_sqlserver_screen_metrics_parse():
    engine = FakeEngine({
        screen_metrics.SQLSERVER_STAT_SQL: [
            {'counter_name': 'Batch Requests/sec', 'cntr_value': 100},
            {'counter_name': 'Transactions/sec', 'cntr_value': 20},
            {'counter_name': 'Transactions/sec', 'cntr_value': 5},
        ],
        screen_metrics.SQLSERVER_TBS_SQL: [
            {'name': 'biz', 'total_mb': '200'},
        ],
        screen_metrics.SQLSERVER_LOCKS_SQL: [{'n': 3}],
    })

    counters, extras = screen_metrics.collect_extra(engine, 'mssql-1', 'sqlserver')

    assert counters['batch_req'] == 100
    assert counters['trans'] == 25
    assert extras['tbs'][0]['name'] == 'biz'
    assert extras['tbs'][0]['total_mb'] == 200
    assert extras['lock_waits'] == 3


def test_oracle_connection_dispatch(monkeypatch):
    calls = {}

    def makedsn(**kwargs):
        calls['dsn'] = kwargs
        return 'dsn-1'

    def connect(**kwargs):
        calls['conn'] = kwargs
        return object()

    fake = types.SimpleNamespace(
        SYSDBA=2, AUTH_MODE_DEFAULT=0, makedsn=makedsn, connect=connect,
    )
    monkeypatch.setitem(sys.modules, 'oracledb', fake)

    engine = MonitorEngine()
    engine._create_connection(
        {'host': 'ora-db', 'port': 1521, 'user': 'scott', 'service_name': 'ORCLPDB'},
        'tiger', 'oracle', 15,
    )

    assert calls['dsn'] == {'host': 'ora-db', 'port': 1521, 'service_name': 'ORCLPDB'}
    assert calls['conn']['dsn'] == 'dsn-1'
    assert calls['conn']['mode'] == 0


def test_sqlserver_connection_dispatch(monkeypatch):
    captured = {}

    def connect(conn_str):
        captured['conn_str'] = conn_str
        return object()

    fake = types.SimpleNamespace(
        drivers=lambda: ['ODBC Driver 18 for SQL Server'],
        connect=connect,
    )
    monkeypatch.setitem(sys.modules, 'pyodbc', fake)

    engine = MonitorEngine()
    engine._create_connection(
        {'host': 'mssql-db', 'port': 1433, 'user': 'sa',
         'database': 'master', 'db_type': 'sqlserver_jdbc'},
        'secret', 'sqlserver', 10,
    )

    conn_str = captured['conn_str']
    assert 'DRIVER={ODBC Driver 18 for SQL Server}' in conn_str
    assert 'SERVER=mssql-db,1433' in conn_str
    assert 'Database=master' in conn_str
    assert 'TrustServerCertificate=yes' in conn_str
