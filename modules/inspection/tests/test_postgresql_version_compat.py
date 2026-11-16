# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""PostgreSQL 9.6–18 巡检 SQL 版本适配回归。"""

import unittest

from modules.core.data_security import assert_readonly_sql
from modules.inspection import postgresql_compat as pg
from modules.inspection.engine import BaseInspectionEngine


class _FakeCursor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.executed = []
        self._rows = []

    def execute(self, sql):
        self.executed.append(sql)
        if not self.responses:
            self._rows = []
            return
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        self._rows = item

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class PostgreSqlVersionTest(unittest.TestCase):
    def test_version_matrix(self):
        cases = {
            '9.4.26': ('postgresql-9.4', 9, 4, 26),
            '9.5.25': ('postgresql-9.5', 9, 5, 25),
            '9.6.24': ('postgresql-9.6', 9, 6, 24),
            '10.23': ('postgresql-10', 10, 0, 23),
            '11.22': ('postgresql-11', 11, 0, 22),
            '12.22': ('postgresql-12', 12, 0, 22),
            '13.18': ('postgresql-13', 13, 0, 18),
            '14.15': ('postgresql-14', 14, 0, 15),
            '15.10': ('postgresql-15', 15, 0, 10),
            '16.3': ('postgresql-16', 16, 0, 3),
            '17.2': ('postgresql-17', 17, 0, 2),
            '18.0': ('postgresql-18', 18, 0, 0),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                parsed = pg.parse_postgresql_version(raw)
                self.assertEqual(
                    (parsed.family, parsed.major, parsed.minor, parsed.patch),
                    expected)
                self.assertIn('PostgreSQL', parsed.display)

    def test_numeric_version_and_banner(self):
        parsed = pg.parse_postgresql_version('160003')
        self.assertEqual(parsed.version_num, 160003)
        self.assertEqual(parsed.family, 'postgresql-16')
        banner = ('PostgreSQL 9.6.24 on x86_64-pc-linux-gnu, compiled by '
                  'gcc (GCC) 4.8.5')
        parsed = pg.parse_postgresql_version(banner)
        self.assertEqual((parsed.major, parsed.minor, parsed.patch),
                         (9, 6, 24))
        self.assertEqual(parsed.family, 'postgresql-9.6')

    def test_unknown_version(self):
        self.assertEqual(pg.parse_postgresql_version('').family, 'unknown')
        self.assertEqual(pg.parse_postgresql_version('unknown').major, 0)


class PostgreSqlVersionDetectionTest(unittest.TestCase):
    def test_fetch_prefers_server_version_num(self):
        cursor = _FakeCursor([[('160003', '16.3')]])
        text, parsed = pg.fetch_postgresql_version(cursor)
        self.assertIn('PostgreSQL 16', text)
        self.assertEqual(parsed.family, 'postgresql-16')
        self.assertIn('server_version_num', cursor.executed[0])

    def test_fetch_falls_back_to_show(self):
        cursor = _FakeCursor([
            Exception('permission denied'),
            [('9.6.24',)],
        ])
        _text, parsed = pg.fetch_postgresql_version(cursor)
        self.assertEqual(parsed.family, 'postgresql-9.6')
        self.assertIn('SHOW server_version_num', cursor.executed[1])

    def test_fetch_all_fail(self):
        cursor = _FakeCursor([
            Exception('no access'),
            Exception('no access'),
            Exception('no access'),
            Exception('no access'),
        ])
        text, parsed = pg.fetch_postgresql_version(cursor)
        self.assertEqual(text, '')
        self.assertEqual(parsed.major, 0)


class PostgreSqlSqlAdaptationTest(unittest.TestCase):
    def test_wal_functions_on_9x(self):
        sql = ('SELECT pg_current_wal_lsn() AS lsn, '
               'pg_walfile_name(pg_current_wal_lsn()) AS file, '
               'pg_wal_lsn_diff(pg_current_wal_lsn(), \'0/0\') AS diff')
        adapted = pg.adapt_postgresql_sql(sql, '9.6.24')
        self.assertIn('pg_current_xlog_location', adapted)
        self.assertIn('pg_xlogfile_name', adapted)
        self.assertIn('pg_xlog_location_diff', adapted)
        self.assertNotIn('pg_current_wal_lsn', adapted)
        assert_readonly_sql(adapted)

    def test_wal_functions_reverse_on_modern(self):
        sql = ('SELECT pg_current_xlog_location(), pg_xlogfile_name('
               'pg_current_xlog_location()), pg_xlog_location_diff('
               'pg_current_xlog_location(), \'0/0\')')
        adapted = pg.adapt_postgresql_sql(sql, '16.3')
        self.assertIn('pg_current_wal_lsn', adapted)
        self.assertIn('pg_walfile_name', adapted)
        self.assertIn('pg_wal_lsn_diff', adapted)
        self.assertNotIn('pg_xlog', adapted)

    def test_archive_path_on_9x(self):
        sql = "SELECT count(*) FROM pg_ls_dir('pg_wal/archive_status')"
        adapted = pg.adapt_postgresql_sql(sql, '9.6.24')
        self.assertIn("'pg_xlog/archive_status'", adapted)
        self.assertNotIn('pg_wal/', adapted)
        assert_readonly_sql(adapted)

    def test_replication_columns_on_9x(self):
        sql = ('SELECT sent_lsn, write_lsn, flush_lsn, replay_lsn '
               'FROM pg_stat_replication')
        adapted = pg.adapt_postgresql_sql(sql, '9.6.24')
        self.assertIn('sent_location', adapted)
        self.assertIn('replay_location', adapted)
        self.assertNotIn('sent_lsn', adapted)
        modern = pg.adapt_postgresql_sql(
            'SELECT sent_location, replay_location FROM pg_stat_replication',
            '16.3')
        self.assertIn('sent_lsn', modern)
        self.assertNotIn('sent_location', modern)

    def test_slow_query_columns_before_13(self):
        sql = ('SELECT total_exec_time, mean_exec_time, max_exec_time, '
               'min_exec_time, stddev_exec_time FROM pg_stat_statements')
        adapted = pg.adapt_postgresql_sql(sql, '12.22', 'pg_top_by_total_time')
        self.assertIn('total_time', adapted)
        self.assertIn('mean_time', adapted)
        self.assertNotIn('total_exec_time', adapted)
        self.assertNotIn('mean_exec_time', adapted)

    def test_slow_query_columns_on_13_plus(self):
        sql = ('SELECT total_time, mean_time, max_time, min_time, stddev_time '
               'FROM pg_stat_statements')
        adapted = pg.adapt_postgresql_sql(sql, '13.18', 'pg_top_by_total_time')
        self.assertIn('total_exec_time', adapted)
        self.assertIn('mean_exec_time', adapted)
        self.assertNotIn('total_time', adapted)

    def test_checkpointer_columns_on_17(self):
        adapted = pg.adapt_postgresql_sql(
            'SELECT * FROM pg_stat_bgwriter', '17.2', 'pg_checkpoint')
        self.assertIn('pg_stat_checkpointer', adapted)
        self.assertIn('checkpoints_timed', adapted)
        self.assertIn('checkpoints_requested', adapted)
        assert_readonly_sql(adapted)
        self.assertEqual(
            pg.adapt_postgresql_sql(
                'SELECT * FROM pg_stat_bgwriter', '16.3', 'pg_checkpoint'),
            'SELECT * FROM pg_stat_bgwriter')

    def test_optional_wait_event_before_9_6(self):
        sql = ('SELECT wait_event_type, wait_event, count(*) '
               'FROM pg_stat_activity GROUP BY 1, 2')
        reason = pg.optional_feature_reason(sql, '9.5.25')
        self.assertIn('不适用', reason)
        self.assertIn('9.6', reason)
        self.assertIsNone(pg.optional_feature_reason(sql, '9.6.24'))

    def test_error_classification(self):
        for error in ("column \"sent_lsn\" does not exist",
                      'function pg_current_wal_lsn() does not exist',
                      'syntax error at or near "pg_wal"',
                      '字段不存在', '列不存在'):
            with self.subTest(error=error):
                self.assertTrue(pg.is_postgresql_compatibility_error(error))
        for error in ('permission denied for table pg_stat_activity',
                      'password authentication failed for user "postgres"',
                      'could not connect to server',
                      'canceling statement due to statement timeout',
                      '权限不足', '认证失败'):
            with self.subTest(error=error):
                self.assertFalse(pg.is_postgresql_compatibility_error(error))


class PostgreSqlTemplateMatrixTest(unittest.TestCase):
    def test_all_default_queries_are_safe_for_supported_versions(self):
        from modules.inspection.init_db import POSTGRESQL_DEFAULT_CHAPTERS
        queries = [q['sql'] for c in POSTGRESQL_DEFAULT_CHAPTERS
                   for q in c['queries']]
        self.assertGreaterEqual(len(queries), 49)
        for version in ('9.6.24', '10.23', '11.22', '12.22', '13.18',
                        '14.15', '15.10', '16.3', '17.2', '18.0'):
            for sql in queries:
                adapted = pg.adapt_postgresql_sql(sql, version)
                with self.subTest(version=version, sql=sql[:80]):
                    assert_readonly_sql(adapted)

    def test_default_wal_and_archive_queries_use_old_names(self):
        from modules.inspection.init_db import POSTGRESQL_DEFAULT_CHAPTERS
        for key in ('pg_wal_status', 'pg_wal_rate', 'pg_is_in_recovery',
                    'pg_replication', 'pg_archive_ready'):
            sql = next(q['sql'] for c in POSTGRESQL_DEFAULT_CHAPTERS
                       for q in c['queries'] if q['key'] == key)
            adapted = pg.adapt_postgresql_sql(sql, '9.6.24', key)
            with self.subTest(key=key):
                self.assertNotIn('pg_current_wal_lsn', adapted)
                self.assertNotIn('pg_wal/', adapted)
                if key == 'pg_replication':
                    self.assertIn('sent_location', adapted)


class PostgreSqlReportLabelTest(unittest.TestCase):
    def test_legacy_and_checkpointer_aliases_have_chinese_labels(self):
        from modules.intelligence.schema_knowledge import report_field_labels
        columns = ('SENT_LOCATION', 'WRITE_LOCATION', 'FLUSH_LOCATION',
                   'REPLAY_LOCATION', 'CHECKPOINTS_REQUESTED',
                   'CHECKPOINTER_STATS_RESET')
        labels = report_field_labels('postgresql', '', list(columns))
        for column, label in zip(columns, labels):
            with self.subTest(column=column):
                self.assertIn('（', label)
                self.assertNotIn('待维护', label)
                self.assertTrue(any('\u4e00' <= ch <= '\u9fff' for ch in label))


class PostgreSqlEngineIntegrationTest(unittest.TestCase):
    def test_engine_rewrites_wal_queries(self):
        engine = object.__new__(BaseInspectionEngine)
        engine.db_type = 'postgresql'
        engine.context = {'version': [{
            'VERSION': 'PostgreSQL 9.6.24 on x86_64-pc-linux-gnu'}]}
        engine._server_version_cache = (
            'PostgreSQL 9.6.24 on x86_64-pc-linux-gnu')
        queries = {
            'pg_wal_status': (
                'SELECT pg_current_wal_lsn() AS current_wal_lsn'),
            'pg_archive_ready': (
                "SELECT count(*) FROM pg_ls_dir('pg_wal/archive_status')"),
        }
        BaseInspectionEngine._adapt_postgresql_queries(engine, queries)
        self.assertIn('pg_current_xlog_location', queries['pg_wal_status'])
        self.assertIn("'pg_xlog/archive_status'", queries['pg_archive_ready'])

    def test_engine_retries_old_wal_function(self):
        class Cursor(_FakeCursor):
            def execute(self, sql):
                self.executed.append(sql)
                if 'pg_current_wal_lsn' in sql:
                    raise Exception(
                        'function pg_current_wal_lsn() does not exist')
                self._rows = [('0/16B6C50',)]

        engine = object.__new__(BaseInspectionEngine)
        engine.db_type = 'postgresql'
        engine.context = {'version': [{'VERSION': '9.6.24'}]}
        engine._server_version_cache = '9.6.24'
        engine.conn = None
        cursor = Cursor([])
        used = BaseInspectionEngine._retry_postgresql_sql(
            engine, cursor,
            'SELECT pg_current_wal_lsn() AS current_wal_lsn',
            'pg_wal_status',
            Exception('function pg_current_wal_lsn() does not exist'))
        self.assertIn('pg_current_xlog_location', used)
        self.assertEqual(len(cursor.executed), 1)

    def test_engine_does_not_retry_permission_error(self):
        engine = object.__new__(BaseInspectionEngine)
        engine.db_type = 'postgresql'
        engine.context = {}
        engine._server_version_cache = '16.3'
        engine.conn = None
        cursor = _FakeCursor([])
        used = BaseInspectionEngine._retry_postgresql_sql(
            engine, cursor,
            'SELECT * FROM pg_stat_replication',
            'pg_replication',
            Exception('permission denied for table pg_stat_replication'))
        self.assertEqual(used, '')
        self.assertEqual(cursor.executed, [])


if __name__ == '__main__':
    unittest.main()
