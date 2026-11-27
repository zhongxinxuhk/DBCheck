# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

import sqlite3
from unittest.mock import Mock

import pytest

from modules.core import paths
from modules.inspection.dal import InspectionTemplateUnavailableError, require_template_db
from modules.inspection.engine import BaseInspectionEngine
from modules.monitor import history_store, screen_metrics
from modules.pro.metrics_collector import MetricsCollector


def test_inspection_reads_runtime_db_and_fails_without_templates(tmp_path, monkeypatch):
    resource_root = tmp_path / '_internal'
    runtime_db = tmp_path / 'data' / 'inspection.db'
    legacy_db = resource_root / 'data' / 'inspection.db'
    monkeypatch.setattr(paths, 'PROJECT_ROOT', resource_root)
    monkeypatch.setattr(paths, 'INSPECTION_DB', runtime_db)
    collector = BaseInspectionEngine('127.0.0.1', 3306, 'fixture', 'fixture', template_id=1)
    collector.db_type = 'mysql'
    collector.conn = Mock()
    monkeypatch.setattr(collector, 'connect', lambda: (True, 'fixture-version'))
    monkeypatch.setattr(collector, '_bind_self_instance', lambda: None)
    with pytest.raises(InspectionTemplateUnavailableError, match='巡检模板库不存在'):
        collector.collect_data()

    legacy_db.parent.mkdir(parents=True)
    legacy_db.touch()
    assert require_template_db() == str(legacy_db)
    runtime_db.parent.mkdir()
    with sqlite3.connect(runtime_db) as db:
        db.execute('CREATE TABLE inspection_chapter (id, template_id, sort_order)')
        db.execute('CREATE TABLE inspection_query (chapter_id, query_key, query_sql, enabled, sort_order)')
    assert require_template_db() == str(runtime_db)
    with pytest.raises(InspectionTemplateUnavailableError, match='没有可执行的查询'):
        collector.collect_data()


def test_sqlserver_home_uses_selected_jdbc_backend(monkeypatch):
    collector = MetricsCollector(db_path=':memory:')
    jdbc = Mock(return_value=(Mock(), {}))
    native = Mock()
    monkeypatch.setattr('modules.monitor.jdbc_collect_cli.open_monitor_jdbc', jdbc)
    monkeypatch.setattr('modules.db_types.compatibility.connect_sqlserver', native)
    source = {'db_type': 'sqlserver_jdbc', 'host': '127.0.0.1', 'port': 1433,
              'database': 'app', 'driver_version': 'selected', 'trust_server_certificate': False}
    collector._connect(dict(source, connection_mode='jdbc'))
    jdbc_type, options = jdbc.call_args.args
    assert jdbc_type == 'sqlserver_jdbc'
    assert options['driver_version'] == 'selected'
    assert options['trust_server_certificate'] is False
    native.assert_not_called()
    collector._connect(dict(source, connection_mode='odbc'))
    native.assert_called_once()


def test_dashboard_group_filters_current_rollup():
    def source(group, qps):
        return {'name': group, 'db_type': 'pg', 'group': group, 'host': '', 'port': 5432,
                'label': group, 'status': 'ok', 'qps': qps}

    collector = screen_metrics.ScreenCollector()
    collector.get_snapshot = lambda: {'a': source('prod', 5), 'b': source('test', 8)}
    collector._trend.append({'ts': 10, 'qps': 13, 'tps': 0,
                             'by_group': {'prod': (5, 0), 'test': (8, 0)}})
    overview = screen_metrics.build_overview(collector, 'prod')
    assert overview['kpis']['total_qps'] == 5
    assert overview['trend'] == [{'ts': 10, 'qps': 5}]
    assert overview['all_groups'] == ['prod', 'test']
    assert [node['id'] for node in overview['nodes']] == ['a']


def test_dashboard_history_uses_sample_group_after_source_moves(tmp_path, monkeypatch):
    store = history_store.HistoryStore(str(tmp_path / 'history.db'))
    try:
        store.write_snap({'id': 'a', 'ts': 100, 'status': 'ok', 'group': 'old', 'qps': 5})
        store.write_snap({'id': 'a', 'ts': 110, 'status': 'ok', 'group': 'new', 'qps': 8})
        store._flush()
        monkeypatch.setattr(history_store, 'get_history_store', lambda: store)
        collector = screen_metrics.ScreenCollector()
        collector.get_snapshot = lambda: {'a': {'group': 'new', 'name': 'source'}}
        monkeypatch.setattr(screen_metrics, 'get_screen_collector', lambda: collector)
        result = screen_metrics.build_history_overview(90, 120, 105, 'old')
        assert result['all_groups'] == ['new', 'old']
        assert result['kpis']['total_qps'] == 5
        assert [(n['group'], n['name']) for n in result['nodes']] == [('old', 'source')]
        after_move = screen_metrics.build_history_overview(90, 120, 115, 'old')
        assert not after_move['nodes']
        assert after_move['trend'] == [{'ts': 100, 'qps': 5}]
    finally:
        store.stop()


def test_windows_host_metrics_are_collected_only_after_posix_fails(monkeypatch):
    collector = MetricsCollector(db_path=':memory:')
    peer = Mock()
    monkeypatch.setattr('paramiko.SSHClient', lambda: peer)
    run = Mock(side_effect=[
        {'host_collector_source': 'unavailable'},
        {'host_collector_source': 'powershell', 'host_services_running': 2},
    ])
    monkeypatch.setattr(collector, '_run_host_script', run)
    ssh = {'host': 'fixture', 'port': 22, 'user': 'fixture', 'key_file': '', 'password': ''}
    assert collector._collect_host_via_ssh(ssh)['host_services_running'] == 2
    _, command, script = run.call_args.args
    assert command.startswith('powershell.exe ')
    assert 'Win32_Service' in script
    peer.close.assert_called_once()
