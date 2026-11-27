# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from modules import jdbc_metrics_cli, jdbc_test_cli
from modules.db_types import connection_tests
from modules.db_types.dbtype_registry import get_db_meta
from modules.inspection import execution
from modules.monitor import queries
from modules.monitor.engine import MonitorEngine
from modules.pro.metrics_collector import MetricsCollector


@pytest.mark.parametrize(('options', 'database'), [
    ({'driver_version': 'selected'}, 'business'),
    ({'driver_version': 'selected', 'database': ''}, ''),
])
def test_connection_worker_keeps_selected_database(monkeypatch, options, database):
    def popen(*args, stdin, stdout, **kwargs):
        payload = json.load(stdin)
        assert payload['kwargs'] == {'driver_version': 'selected', 'database': database}
        stdout.write(jdbc_test_cli.RESULT_PREFIX + '{"ok": true, "msg": "connected"}\n')
        return SimpleNamespace(poll=lambda: 0, returncode=0)

    monkeypatch.setattr(subprocess, 'Popen', popen)
    original = options.copy()
    assert connection_tests.run_jdbc_test_subprocess(
        'mysql', {'database': 'business'}, options) == (True, 'connected')
    assert options == original


@pytest.mark.parametrize(('dialect', 'user', 'database'), [
    ('mariadb', 'fixture', 'business'),
    ('oceanbase', 'fixture@tenant', ''),
])
def test_inspection_preflight_uses_database_specific_driver(monkeypatch, dialect, user, database):
    probe = Mock(return_value=(False, 'fixture preflight failure'))
    monkeypatch.setattr(execution, f'test_{dialect}_connection', probe)
    task = {}
    execution._run_inspection_task(
        'fixture', task,
        {'_db_type': dialect, 'ip': '127.0.0.1', 'port': 3306, 'user': 'fixture',
         'password': 'fixture', 'database': database, 'tenant': 'tenant',
         'driver_version': 'selected'},
        '', None, None, lambda *_: None,
    )
    probe.assert_called_once_with('127.0.0.1', 3306, user, 'fixture', database, 'selected')
    assert get_db_meta(dialect).connect_test == dialect


def test_postgres_monitor_routes_saved_driver_and_allows_custom_queries(monkeypatch):
    engine = MonitorEngine()
    batch = Mock(side_effect=lambda inst, dialect, sqls, timeout, **options:
                 {sqls[0]: [{'database': 'postgres'}]})
    cached = Mock(return_value=[])
    monkeypatch.setattr(engine, '_run_monitor_batch', batch)
    monkeypatch.setattr(engine, '_jdbc_query', cached)
    source = {'id': 'pg1', 'db_type': 'postgresql', 'driver_version': '42.7.13'}
    engine.run_snapshot_query(source, queries.PG_CONNECTION_SQL)
    assert cached.call_args.args[1] == 'pg'
    sql = 'SELECT current_database() AS database'
    assert engine.run_snapshot_query(source, sql) == [{'database': 'postgres'}]
    assert batch.call_args.args[1:3] == ('pg', [sql])
    engine.run_snapshot_query(dict(source, driver_version=''), sql)
    assert batch.call_args.kwargs['native'] is True


def test_postgres_pinned_connection_test_surfaces_driver_failure(monkeypatch):
    monkeypatch.setattr(jdbc_test_cli, '_tcp_preflight', lambda *_: None)
    monkeypatch.setattr('modules.jdbc_connector.open_jdbc_connection',
                        lambda *args, **kwargs: (None, {'error': 'missing selected driver'}))
    fallback = Mock(return_value=(True, 'native'))
    monkeypatch.setattr(jdbc_test_cli, '_pg_fallback', fallback)
    assert jdbc_test_cli._test_pg_jdbc({'kwargs': {'driver_version': 'missing'}}) == (
        False, 'missing selected driver')
    fallback.assert_not_called()
    assert jdbc_test_cli._test_pg_jdbc({}) == (True, 'native')


@pytest.mark.parametrize(('states', 'expected'), [
    ([('ACTIVE', 2), ('IDLE', 3), ('CREATE', 1)], {'total_connections': 6, 'active_sessions': 2}),
    ([('r', 2), ('W ', 1), ('I', 3)], {'total_connections': 6, 'active_sessions': 3}),
    ([('IDLE', 4)], {'total_connections': 4, 'active_sessions': 0}),
    (None, {'total_connections': 9}),
])
def test_dm_session_counts_for_current_and_legacy_states(states, expected):
    cursor = Mock(fetchall=Mock(return_value=states), fetchone=Mock(return_value=(9,)))

    def execute(sql):
        if 'sysstat' in sql or (states is None and 'GROUP BY' in sql):
            raise RuntimeError('unavailable view or column')

    cursor.execute.side_effect = execute
    collector = MetricsCollector(db_path=':memory:')
    assert collector._collect_dm(Mock(cursor=Mock(return_value=cursor))) == expected
    assert 'FROM V$SESSIONS GROUP BY STATE' in cursor.execute.call_args_list[1].args[0]


def test_ssh_metrics_use_connection_timing_instead_of_local_probe(monkeypatch):
    collector = MetricsCollector(db_path=':memory:')
    source = {'id': 'source', 'db_type': 'pg', 'ssh_tunnel': True}
    connection = Mock()
    monkeypatch.setattr(MetricsCollector, '_connect', lambda self, inst: connection)
    monkeypatch.setattr(MetricsCollector, '_collect_deep',
                        lambda *args: {'latency_ms': 999, 'numbackends': 1})
    times = iter((1, 1.028))
    monkeypatch.setattr(jdbc_metrics_cli, 'time', SimpleNamespace(perf_counter=lambda: next(times)))
    monkeypatch.setattr(collector, '_collect_metrics_subprocess',
                        lambda inst: jdbc_metrics_cli._collect(inst, 'pg'))
    monkeypatch.setattr(collector, '_attach_host', Mock())
    probe = Mock()
    monkeypatch.setattr(collector, '_tcp_probe', probe)
    result = collector.collect_one(source)
    assert (result['available'], result['latency_ms'], result['latency_source']) == (
        True, 28.0, 'tunnel_connect')
    assert result['numbackends'] == 1
    connection.close.assert_called_once()
    probe.assert_not_called()
