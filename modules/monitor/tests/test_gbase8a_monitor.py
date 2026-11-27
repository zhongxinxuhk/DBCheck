# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""GBase 8a 监控大屏接入回归测试（纯映射/分派，不连实库）。"""

import modules.monitor.queries as queries
from modules.monitor import screen_metrics
from modules.monitor.engine import MonitorEngine


def test_gbase8a_monitor_templates_registered():
    assert queries.normalize_db_type('gbase8a') == 'gbase8a'
    assert 'gbase8a' in queries.SLOW_QUERY_TEMPLATES
    assert 'gbase8a' in queries.SLOW_QUERY_FALLBACK_TEMPLATES
    assert 'gbase8a' in queries.CONNECTION_TEMPLATES
    assert 'gbase8a' in queries.MAX_CONN_QUERY_SQL
    assert queries.MAX_CONN_QUERY_SQL['gbase8a'] == 'SELECT @@max_connections AS max_conn'
    assert 'information_schema.PROCESSLIST' in queries.SLOW_QUERY_TEMPLATES['gbase8a']
    assert 'information_schema.processlist' in queries.CONNECTION_TEMPLATES['gbase8a']


def test_gbase8a_uses_mysql_screen_family_with_8a_sql():
    assert screen_metrics.db_family('gbase8a') == 'mysql'

    class FakeEngine:
        def __init__(self):
            self.sqls = []

        def run_query(self, instance_id, sql):
            self.sqls.append(sql)
            if sql == screen_metrics.GBASE8A_STAT_SQL:
                return [
                    {'variable_name': 'Questions', 'value': '120'},
                    {'variable_name': 'Com_commit', 'value': '7'},
                    {'variable_name': 'Com_rollback', 'value': '1'},
                ]
            if sql == screen_metrics.GBASE8A_TBS_SQL:
                return [{'name': 'biz', 'total_mb': '12.5'}]
            raise AssertionError('unexpected SQL: %s' % sql)

    engine = FakeEngine()
    counters, extras = screen_metrics.collect_extra(engine, 'i-8a', 'gbase8a')

    assert counters['questions'] == 120
    assert counters['com_commit'] == 7
    assert extras['tbs'] == [{'name': 'biz', 'total_mb': 12.5,
                              'free_mb': None, 'free_pct': None}]
    assert screen_metrics.GBASE8A_STAT_SQL in engine.sqls
    # 8a 无 MySQL 主从复制，不应再执行 SHOW SLAVE/REPLICA STATUS
    assert all('SLAVE STATUS' not in sql and 'REPLICA STATUS' not in sql
               for sql in engine.sqls)


def test_gbase8a_connection_uses_native_adapter(monkeypatch):
    captured = {}

    def fake_connect(options):
        captured.update(options)
        return object()

    monkeypatch.setattr('modules.db_types.compatibility.connect_gbase8a', fake_connect)
    engine = MonitorEngine()
    instance = {
        'host': '10.0.0.8',
        'port': 5258,
        'user': 'reader',
        'password': 'secret',
        'db_type': 'gbase8a',
        'tls_ca_file': '/tmp/gbase-ca.pem',
    }

    engine._create_connection(instance, 'secret', 'gbase8a', 5)

    assert captured['host'] == '10.0.0.8'
    assert captured['port'] == 5258
    assert captured['password'] == 'secret'
    assert captured['database'] == ''
    assert captured['virtual_cluster'] == ''
    # 未建隧道时必须保留直连语义，让适配器继续强制 TLS CA 校验
    assert '_tunnel_ready' not in captured
    assert captured['tls_ca_file'] == '/tmp/gbase-ca.pem'


def test_gbase8a_connection_marks_existing_tunnel(monkeypatch):
    captured = {}

    def fake_connect(options):
        captured.update(options)
        return object()

    monkeypatch.setattr('modules.db_types.compatibility.connect_gbase8a', fake_connect)
    monkeypatch.setattr('modules.monitor.engine.resolve_db_endpoint',
                        lambda inst: ('127.0.0.1', 43210, object()))
    engine = MonitorEngine()
    instance = {
        'host': '10.0.0.8',
        'port': 5258,
        'user': 'reader',
        'password': 'secret',
        'db_type': 'gbase8a',
        'ssh_host': '10.0.0.8',
        'ssh_tunnel': True,
    }

    engine._create_connection(instance, 'secret', 'gbase8a', 5)

    assert captured['host'] == '127.0.0.1'
    assert captured['port'] == 43210
    assert captured['_tunnel_ready'] is True
    assert captured['ssh_tunnel'] is False


def test_background_queries_share_config_snapshot(monkeypatch):
    engine = MonitorEngine()
    source = {'id': 'gbase', 'db_type': 'gbase8a', 'host': 'db', 'updated_at': 'v1'}
    calls = []

    def query(config, sql):
        calls.append((config, sql))
        return []

    monkeypatch.setattr(engine, 'run_snapshot_query', query)
    monkeypatch.setattr('modules.monitor.engine.get_instance_manager',
                        lambda: (_ for _ in ()).throw(AssertionError('per-query source reload')))
    result = engine._collect_instance(source)

    assert [sql for _, sql in calls] == [
        queries.SLOW_QUERY_TEMPLATES['gbase8a'],
        queries.CONNECTION_TEMPLATES['gbase8a'],
        queries.MAX_CONN_QUERY_SQL['gbase8a'],
    ]
    assert all(config is source for config, _ in calls)
    assert result['slow']['error'] is None
    assert result['conn']['error'] is None


def test_monitor_worker_kills_blocked_process_and_can_subsequently_run(monkeypatch):
    import sys

    import pytest

    from modules.core.proc_registry import active_count
    from modules.monitor import worker_transport

    baseline = active_count()
    monkeypatch.setattr(worker_transport, '_worker_command',
                        lambda: [sys.executable, '-c', 'import time; time.sleep(30)'])
    with pytest.raises(TimeoutError, match='超时'):
        worker_transport.run_monitor_worker({}, timeout=0.15)
    assert active_count() == baseline

    monkeypatch.setattr(worker_transport, '_worker_command',
                        lambda: [sys.executable, '-c', 'print("{}")'])
    assert worker_transport.run_monitor_worker({}, timeout=3) == {}


def test_monitor_worker_uses_bounded_fetch_and_rejects_large_results(monkeypatch):
    from modules.monitor import jdbc_collect_cli as cli

    limits = []

    class Cursor:
        description = [('Count',)]

        def execute(self, sql):
            self.large_result = sql == 'SELECT 1'

        def fetchmany(self, limit):
            limits.append(limit)
            return [(1,)] * (limit if self.large_result else 1)

        def close(self):
            pass

    class Connection:
        cursor = Cursor

        def close(self):
            pass

    monkeypatch.setattr(MonitorEngine, '_create_connection', lambda *args: Connection())
    result = cli._collect({
        'db_type': 'gbase8a', 'native_sql': True,
        'source': {'host': 'db.example', 'port': 5258, 'db_type': 'gbase8a'},
        'queries': ['SELECT 1', 'SELECT 2'],
    })
    assert limits == [cli.MAX_QUERY_ROWS + 1] * 2
    assert result['q0']['rows'] == []
    assert f'{cli.MAX_QUERY_ROWS} 行' in result['q0']['error']
    assert result['q1'] == {'rows': [{'count': 1}], 'error': None}
