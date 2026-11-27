# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""GBase 8a 容量查询超时隔离回归。"""

from unittest.mock import Mock, patch

import pytest

from modules.entrypoints.main_gbase8a import GBase8aInspector


def _raw(description, rows, error=None):
    raw = Mock()
    raw.cursor.return_value.stmt = None
    raw.cursor.return_value.description = [(name,) for name in description]
    raw.cursor.return_value.fetchmany.side_effect = [rows, []]
    if error is not None:
        raw.cursor.return_value.execute.side_effect = error
    return raw


def _inspector():
    return GBase8aInspector('127.0.0.1', 5258, 'fixture', 'fixture', 'vc1')


def test_capacity_queries_are_moved_after_other_chapters():
    inspector = _inspector()
    sql_dict = {
        'W05_table_size_top': 'SELECT 5',
        'W06_schema_space': 'SELECT 6',
        'W07_backup_param': 'SELECT 7',
    }

    inspector._customize_queries(sql_dict)

    assert list(sql_dict) == ['W07_backup_param', 'W05_table_size_top', 'W06_schema_space']


def test_capacity_success_feeds_rule_metrics_without_reexecution():
    inspector = _inspector()
    raw = _raw(['DATA_MB'], [(12.5,), (3.5,)])

    with patch('modules.entrypoints.main_gbase8a.connect_gbase8a', return_value=raw):
        result = inspector._execute_capacity_query(
            'W05_table_size_top', 'SELECT TABLE_NAME, DATA_LENGTH AS DATA_MB FROM information_schema.TABLES')

    assert [row['DATA_MB'] for row in result['data']] == [12.5, 3.5]
    assert inspector.context['gbase8a_max_table_mb'] == 12.5
    assert inspector.context['_query_status']['W05_table_size_top']['status'] == 'collected'


def test_capacity_timeout_is_isolated_from_next_capacity_query():
    inspector = _inspector()
    timeout_raw = _raw(['DATA_MB'], [], TimeoutError('budget exhausted'))
    success_raw = _raw(['data_mb'], [(8.25,)])

    with patch('modules.entrypoints.main_gbase8a.connect_gbase8a',
               side_effect=[timeout_raw, success_raw]):
        inspector._execute_capacity_query('W05_table_size_top', 'SELECT 5')
        inspector._execute_capacity_query(
            'W06_schema_space', 'SELECT TABLE_SCHEMA, SUM(DATA_LENGTH) AS data_mb FROM information_schema.TABLES')

    assert inspector.context['_query_status']['W05_table_size_top']['status'] == 'not_applicable'
    assert inspector.context['_query_status']['W06_schema_space']['status'] == 'collected'
    assert inspector.context['gbase8a_max_table_mb'] == 0
    assert inspector.context['gbase8a_total_data_mb'] == 8.25


def test_capacity_connection_failure_keeps_report_pipeline_alive():
    inspector = _inspector()

    with patch('modules.entrypoints.main_gbase8a.connect_gbase8a',
               side_effect=ConnectionError('cluster unreachable')):
        result = inspector._execute_capacity_query('W06_schema_space', 'SELECT 6')

    assert result['data'] == []
    assert inspector.context['W06_schema_space'] == []
    assert inspector.context['gbase8a_total_data_mb'] == 0
    assert inspector.context['_query_status']['W06_schema_space']['status'] == 'not_applicable'


def test_w06_old_template_gets_system_schema_filter():
    from modules.inspection.gbase_compat import adapt_gbase8a_sql

    sql = ('SELECT TABLE_SCHEMA, COUNT(*) AS table_count FROM information_schema.TABLES '
           'GROUP BY TABLE_SCHEMA ORDER BY table_count DESC')
    adapted = adapt_gbase8a_sql(sql, '9.5.3.28', 'W06_schema_space')

    assert adapted.upper().count('TABLE_SCHEMA NOT IN') == 1
    assert "WHERE TABLE_SCHEMA NOT IN ('information_schema','gbase','gclusterdb','performance_schema')" in adapted
    assert adapt_gbase8a_sql(adapted, '9.5.3.28', 'W06_schema_space').count('TABLE_SCHEMA NOT IN') == 1
