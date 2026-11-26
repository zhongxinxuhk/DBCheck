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
    assert "'blocked'" in queries.ORACLE_CONNECTION_SQL
    assert "'blocked'" in queries.SQLSERVER_CONNECTION_SQL

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
            {'name': 'master/data', 'total_mb': '200', 'free_mb': '50', 'max_mb': '300'},
            {'name': 'master/log', 'total_mb': '20'},
        ],
        screen_metrics.SQLSERVER_LOCKS_SQL: [{'n': 3}],
    })

    counters, extras = screen_metrics.collect_extra(engine, 'mssql-1', 'sqlserver')

    assert counters['batch_req'] == 100
    assert counters['trans'] == 25
    assert extras['tbs'][0]['name'] == 'master/data'
    assert extras['tbs'][0]['total_mb'] == 200
    assert extras['tbs'][0]['free_mb'] == 50
    assert extras['tbs'][0]['free_pct'] == 50
    assert extras['tbs'][1]['free_pct'] is None
    assert extras['lock_waits'] == 3


def test_oracle_connection_dispatch(monkeypatch):
    calls = {}

    def makedsn(**kwargs):
        calls['dsn'] = kwargs
        return 'dsn-1'

    def connect(**kwargs):
        calls['conn'] = kwargs
        return types.SimpleNamespace()

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
    assert calls['conn']['tcp_connect_timeout'] == 15

    engine._create_connection(
        {'host': 'ora-db', 'port': 1521, 'user': 'scott',
         'service_name': 'ORCL', 'use_sid': True},
        'tiger', 'oracle', 15,
    )
    assert calls['dsn'] == {'host': 'ora-db', 'port': 1521, 'sid': 'ORCL'}


def test_oracle_monitor_forwards_sysdba_to_dbapi_driver(monkeypatch):
    from modules import jdbc_connector
    from modules.monitor.jdbc_collect_cli import _collect

    captured = {}

    def connect(driver, url, properties, jars):
        captured.update(properties)
        return types.SimpleNamespace(close=lambda: None)

    monkeypatch.setitem(sys.modules, 'jaydebeapi', types.SimpleNamespace(connect=connect))
    monkeypatch.setattr(jdbc_connector, 'resolve_driver_jars', lambda *a, **kw: ['fixture.jar'])
    monkeypatch.setattr(jdbc_connector, '_start_jvm', lambda *_: None)
    monkeypatch.setattr(jdbc_connector, '_is_jvm_started', lambda: True)
    assert _collect(dict(db_type='oracle_jdbc', host='localhost', port=1521, user='sys',
                         service_name='ORCL', password='fixture', sysdba=True)) == {}
    assert captured['internal_logon'] == 'sysdba'
    assert captured['user'] == 'sys'


def test_sqlserver_connection_dispatch(monkeypatch):
    captured = {}

    def connect(conn_str, timeout):
        captured['conn_str'] = conn_str
        captured['timeout'] = timeout
        return types.SimpleNamespace()

    fake = types.SimpleNamespace(
        drivers=lambda: ['ODBC Driver 18 for SQL Server'],
        connect=connect,
    )
    monkeypatch.setitem(sys.modules, 'pyodbc', fake)

    engine = MonitorEngine()
    engine._create_connection(
        {'host': 'mssql-db', 'port': 1433, 'user': 'sa',
         'database': 'master', 'db_type': 'sqlserver_jdbc'},
        'secret;with}characters', 'sqlserver', 10,
    )

    conn_str = captured['conn_str']
    assert 'DRIVER={ODBC Driver 18 for SQL Server}' in conn_str
    assert 'SERVER={mssql-db,1433}' in conn_str
    assert 'Database={master}' in conn_str
    assert 'PWD={secret;with}}characters};' in conn_str
    assert 'TrustServerCertificate=yes' in conn_str
    assert captured['timeout'] == 10


def test_sqlserver_monitor_honors_jdbc_mode(monkeypatch):
    source = {'id': 'ms', 'db_type': 'sqlserver_jdbc', 'connection_mode': 'jdbc'}
    manager = types.SimpleNamespace(get_instance_decrypted=lambda iid: source)
    monkeypatch.setattr('modules.monitor.engine.get_instance_manager', lambda: manager)
    monkeypatch.setattr('modules.core.paths.PORTABLE_ROOT', None)
    engine = MonitorEngine()
    batch = []

    def jdbc(inst, connector, timeout, template_key):
        batch.append((inst, connector, template_key))
        return {'SELECT 1': [{'value': 1}]}

    monkeypatch.setattr(engine, '_jdbc_run_batch', jdbc)
    assert engine.probe('ms', 'sqlserver_jdbc') is None
    assert batch == [(source, 'sqlserver_jdbc', 'sqlserver')]


def test_screen_probes_before_marking_session_error_as_down(monkeypatch):
    collector = screen_metrics.ScreenCollector()
    monkeypatch.setattr(screen_metrics, 'collect_extra', lambda *_: ({}, {}))
    source = {'id': 'ora', 'db_type': 'oracle'}
    conn = {'ora': {'error': 'ORA-00942'}}
    engine = MonitorEngine()
    monkeypatch.setattr(engine, 'run_query', lambda *_: [{'value': 1}])
    snap = collector._collect_one(engine, source, conn, {})
    assert snap['status'] == 'ok'
    assert snap['conn_err'] == 'ORA-00942'

    def refused(*_):
        raise ConnectionRefusedError('connection refused')

    monkeypatch.setattr(engine, 'run_query', refused)
    assert collector._collect_one(engine, source, conn, {})['status'] == 'down'
    conn['ora']['incomplete'] = True
    assert collector._collect_one(engine, source, conn, {})['status'] == 'pending'


def test_connection_blocked_sessions_are_counted(monkeypatch):
    engine = MonitorEngine()

    def fake_query(instance_id, sql, timeout=None):
        if sql == queries.ORACLE_CONNECTION_SQL:
            return [
                {'username': 'u1', 'state': 'blocked', 'total_connections': 103},
                {'username': 'u2', 'state': 'ACTIVE'},
                {'username': 'u3', 'state': 'INACTIVE'},
            ]
        if sql == queries.MAX_CONN_QUERY_SQL['oracle']:
            return [{'max_conn': 100}]
        return []

    monkeypatch.setattr(engine, '_connect_and_query', fake_query)
    result = engine._collect_conn('ora-1', 'oracle', 'Oracle (db:1521)')

    assert result['total'] == 103
    assert result['max_conn'] == 100
    assert result['connections'] == {'active': 1, 'idle': 1, 'blocked': 1}
    assert result['usage_pct'] == 103.0

def test_dm8_falls_back_to_running_samples_when_sql_history_empty(monkeypatch):
    engine = MonitorEngine()
    live_row = {'sql_text': 'SELECT 1', 'avg_time_s': 4}
    results = {queries.DM_SLOW_QUERY_SQL: [], queries.DM_SLOW_QUERY_FALLBACK_SQL: [live_row]}
    monkeypatch.setattr(engine, '_connect_and_query', lambda instance_id, sql: results[sql])

    result = engine._collect_slow('dm-1', 'dm', 'DM8')
    assert result['error'] is None
    assert result['data'] == [{**live_row, 'sample_kind': 'running'}]


def test_waiting_is_distinct_from_blocked_in_sqlserver_and_dm8(monkeypatch):
    engine = MonitorEngine()
    rows = [{'state': 'waiting'}, {'state': 'blocked'}, {'state': 'IDLE'}]
    monkeypatch.setattr(engine, '_connect_and_query',
                        lambda iid, sql: [{'max_conn': 100}] if sql in queries.MAX_CONN_QUERY_SQL.values() else rows)

    for family in ('sqlserver', 'dm'):
        result = engine._collect_conn('one', family, family)
        assert result['connections'] == {'active': 1, 'idle': 1, 'blocked': 1}
