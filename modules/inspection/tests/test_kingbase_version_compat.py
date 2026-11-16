# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""KingbaseES V8/V9 巡检 SQL 版本适配回归。"""

import unittest

from modules.core.data_security import assert_readonly_sql
from modules.inspection import kingbase_compat as kb
from modules.inspection.engine import BaseInspectionEngine


class _FakeCursor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.executed = []
        self.rollbacks = 0
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

    def rollback(self):
        self.rollbacks += 1

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class KingbaseVersionTest(unittest.TestCase):
    def test_version_matrix(self):
        cases = {
            'KingbaseES V008R006C007B0012 on x86_64-pc-linux-gnu': (
                'kingbase-v8r6', 8, 6, 7, 12, (12, 0)),
            'KingbaseES V008R003C002B0160': (
                'kingbase-v8r3', 8, 3, 2, 160, (9, 6)),
            'KingbaseES V009R001C002B0014 on x86_64': (
                'kingbase-v9r1', 9, 1, 2, 14, (12, 0)),
            'KingbaseES V009R002C016': (
                'kingbase-v9r2', 9, 2, 16, 0, (12, 0)),
            'KingbaseES V009R003C011': (
                'kingbase-v9r3', 9, 3, 11, 0, (12, 0)),
            'KingbaseES V8.6.0': (
                'kingbase-v8r6', 8, 6, 0, 0, (12, 0)),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                parsed = kb.parse_kingbase_version(raw)
                self.assertEqual(
                    (parsed.family, parsed.major, parsed.release,
                     parsed.patch, parsed.build, parsed.baseline),
                    expected)
                self.assertIn('KingbaseES', parsed.display)

    def test_ksql_and_sys_ctl_versions(self):
        banner = '(Kingbase) V008R006C005B0041'
        parsed = kb.parse_kingbase_version(banner)
        self.assertEqual(parsed.family, 'kingbase-v8r6')
        self.assertEqual(parsed.baseline, (12, 0))

        parsed = kb.parse_kingbase_version('(Kingbase) 12.1')
        self.assertEqual(parsed.family, 'kingbase-pg12')
        self.assertEqual(parsed.baseline, (12, 0))
        self.assertEqual(parsed.pg_patch, 1)

        parsed = kb.parse_kingbase_version('PostgreSQL 9.6.24')
        self.assertEqual(parsed.family, 'kingbase-pg9.6')
        self.assertEqual(parsed.baseline, (9, 6))

    def test_v7_is_not_treated_as_postgresql_v8(self):
        parsed = kb.parse_kingbase_version('KingbaseES V7R6')
        self.assertEqual(parsed.family, 'kingbase-v7r6')
        self.assertEqual(parsed.baseline, (9, 4))

    def test_unknown_version(self):
        parsed = kb.parse_kingbase_version('unknown')
        self.assertEqual(parsed.family, 'unknown')
        self.assertEqual(parsed.baseline, (0, 0))
        self.assertIsNone(kb.optional_feature_reason(
            'SELECT wait_event FROM pg_stat_activity', 'unknown'))


class KingbaseVersionDetectionTest(unittest.TestCase):
    def test_fetch_combines_banner_and_postgresql_baseline(self):
        cursor = _FakeCursor([
            [('KingbaseES V008R006C007B0012 on x86_64', '120001')],
        ])
        text, parsed = kb.fetch_kingbase_version(cursor)
        self.assertIn('V008R006C007B0012', text)
        self.assertEqual(parsed.family, 'kingbase-v8r6')
        self.assertEqual(parsed.pg_major, 12)
        self.assertEqual(parsed.pg_num, 120001)
        self.assertIn('SELECT version()', cursor.executed[0])
        self.assertEqual(len(cursor.executed), 1)

    def test_fetch_falls_back_to_server_version(self):
        cursor = _FakeCursor([
            Exception('permission denied'),
            Exception('permission denied'),
            Exception('permission denied'),
            [('12.1',)],
        ])
        text, parsed = kb.fetch_kingbase_version(cursor)
        self.assertEqual(text, '12.1')
        self.assertEqual(parsed.family, 'kingbase-pg12')
        self.assertEqual(parsed.baseline, (12, 0))
        self.assertEqual(cursor.rollbacks, 3)

    def test_fetch_all_fail(self):
        cursor = _FakeCursor([
            Exception('no access'),
            Exception('no access'),
            Exception('no access'),
            Exception('no access'),
            Exception('no access'),
            Exception('no access'),
        ])
        text, parsed = kb.fetch_kingbase_version(cursor)
        self.assertEqual(text, '')
        self.assertEqual(parsed.family, 'unknown')
        self.assertEqual(cursor.rollbacks, 6)

    def test_fetch_rolls_back_jdbc_cursor_connection(self):
        class NativeConnection:
            def __init__(self):
                self.rollbacks = 0

            def rollback(self):
                self.rollbacks += 1

        class JdbcCursor(_FakeCursor):
            def __init__(self):
                super().__init__([Exception('bad setting')] * 6)
                self._connection = NativeConnection()

            def rollback(self):
                raise AssertionError('jaydebeapi cursor has no rollback')

        cursor = JdbcCursor()
        kb.fetch_kingbase_version(cursor)
        self.assertEqual(cursor._connection.rollbacks, 6)


class KingbaseSqlAdaptationTest(unittest.TestCase):
    def test_v8r3_uses_xlog_and_legacy_replication_columns(self):
        wal = ('SELECT pg_walfile_name(pg_current_wal_lsn()) AS current_wal_file, '
               "pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0') AS wal_bytes_total")
        adapted = kb.adapt_kingbase_sql(
            wal, 'KingbaseES V008R003C002B0160', 'pg_wal_rate')
        self.assertIn('pg_xlogfile_name', adapted)
        self.assertIn('pg_current_xlog_location', adapted)
        self.assertIn('pg_xlog_location_diff', adapted)
        self.assertNotIn('pg_current_wal_lsn', adapted)
        assert_readonly_sql(adapted)

        replication = ('SELECT sent_lsn, write_lsn, flush_lsn, replay_lsn '
                       'FROM pg_stat_replication')
        adapted = kb.adapt_kingbase_sql(
            replication, 'KingbaseES V008R003C002B0160', 'pg_replication')
        self.assertIn('sent_location', adapted)
        self.assertIn('replay_location', adapted)
        self.assertNotIn('sent_lsn', adapted)

    def test_v8r6_keeps_modern_wal_and_replication_columns(self):
        wal = 'SELECT pg_current_wal_lsn() AS lsn'
        self.assertEqual(
            kb.adapt_kingbase_sql(
                wal, 'KingbaseES V008R006C007B0012', 'pg_wal_rate'),
            wal)
        replication = 'SELECT sent_lsn, replay_lsn FROM pg_stat_replication'
        self.assertEqual(
            kb.adapt_kingbase_sql(
                replication, '(Kingbase) 12.1', 'pg_replication'),
            replication)

    def test_v9_does_not_split_bgwriter_into_checkpointer(self):
        sql = 'SELECT * FROM pg_stat_bgwriter'
        adapted = kb.adapt_kingbase_sql(
            sql, 'KingbaseES V009R002C016', 'pg_checkpoint')
        self.assertEqual(adapted, sql)
        self.assertNotIn('pg_stat_checkpointer', adapted)

    def test_low_baseline_optional_features(self):
        sql = 'SELECT wait_event_type, wait_event FROM pg_stat_activity'
        reason = kb.optional_feature_reason(sql, 'PostgreSQL 9.4.26')
        self.assertIn('不适用', reason)
        self.assertIn('9.6', reason)
        self.assertIsNone(kb.optional_feature_reason(sql, 'V008R006C007B0012'))

    def test_low_baseline_removes_usebypassrls_instead_of_faking_it(self):
        sql = ('SELECT usename, userepl, usebypassrls, '
               'passwd IS NOT NULL AS has_password FROM pg_user')
        adapted = kb.adapt_kingbase_sql(sql, 'PostgreSQL 9.4.26', 'pg_users')
        self.assertNotIn('usebypassrls', adapted)
        self.assertIn('has_password', adapted)

    def test_low_baseline_filter_rewrite(self):
        sql = ("SELECT datname, count(*) FILTER (WHERE state='active') "
               'AS active_count FROM pg_stat_activity GROUP BY datname')
        adapted = kb.adapt_kingbase_sql(sql, 'PostgreSQL 9.3.25', 'pg_conn_by_db')
        self.assertNotIn('FILTER', adapted)
        self.assertIn('CASE WHEN state', adapted)

    def test_v9r2c16_uses_64bit_xid_risk_denominator(self):
        sql = ("SELECT datname, age(datfrozenxid) AS xid_age, "
               "round(age(datfrozenxid) * 100.0 / 2147483647, 2) "
               "AS wraparound_risk FROM pg_database")
        old = kb.adapt_kingbase_sql(
            sql, 'KingbaseES V009R002C015', 'pg_database_age')
        new = kb.adapt_kingbase_sql(
            sql, 'KingbaseES V009R002C016', 'pg_database_age')
        self.assertIn('2147483647', old)
        self.assertNotIn('2147483647', new)
        self.assertIn('9223372036854775807', new)

    def test_candidates_cover_sys_objects_and_alternate_wal_names(self):
        sql = 'SELECT pg_current_wal_lsn() AS lsn'
        candidates = kb.kingbase_query_candidates(
            sql, 'KingbaseES V008R003C002B0160', 'pg_wal_rate')
        self.assertTrue(any('pg_current_xlog_location' in c for c in candidates))
        self.assertTrue(any('sys_current_xlog_location' in c for c in candidates))
        self.assertTrue(any('pg_current_wal_lsn' in c for c in candidates))
        for candidate in candidates:
            assert_readonly_sql(candidate)

    def test_sys_candidate_rewrites_pg_catalog_schema(self):
        sql = 'SELECT count(*) FROM pg_catalog.pg_stat_activity'
        candidates = kb.kingbase_query_candidates(
            sql, 'KingbaseES V009R001C002B0014', 'pg_conn_detail')
        self.assertTrue(any('sys_catalog.sys_stat_activity' in c
                            for c in candidates))
        self.assertFalse(any('pg_catalog.sys_stat_activity' in c
                             for c in candidates))

    def test_candidates_recover_sys_stat_activity(self):
        sql = 'SELECT count(*) AS count FROM pg_stat_activity'
        candidates = kb.kingbase_query_candidates(
            sql, 'KingbaseES V009R001C002B0014', 'pg_conn_detail')
        self.assertTrue(any('sys_stat_activity' in c for c in candidates))
        self.assertEqual(len(candidates), len(set(candidates)))

    def test_error_classification(self):
        for error in (
                'column "sent_lsn" does not exist',
                'relation "pg_stat_activity" does not exist',
                'function pg_current_wal_lsn() does not exist',
                'syntax error at or near "pg_wal"',
                'ERROR: 42703: column does not exist',
                '字段不存在', '表不存在'):
            with self.subTest(error=error):
                self.assertTrue(kb.is_kingbase_compatibility_error(error))
        for error in (
                'permission denied for table pg_stat_activity',
                'password authentication failed for user "system"',
                'could not connect to server',
                'canceling statement due to statement timeout',
                '权限不足', '认证失败', '连接失败'):
            with self.subTest(error=error):
                self.assertFalse(kb.is_kingbase_compatibility_error(error))


class KingbaseTemplateMatrixTest(unittest.TestCase):
    def test_all_default_queries_are_safe_for_supported_versions(self):
        from modules.inspection.init_db import KINGBASE_DEFAULT_CHAPTERS
        queries = [(q['key'], q['sql']) for c in KINGBASE_DEFAULT_CHAPTERS
                   for q in c['queries']]
        self.assertGreaterEqual(len(queries), 25)
        versions = (
            'KingbaseES V008R002C001B0001',
            'KingbaseES V008R003C002B0160',
            'KingbaseES V008R006C007B0012',
            'KingbaseES V009R001C002B0014',
            'KingbaseES V009R002C016',
            'KingbaseES V009R003C011',
            'KingbaseES V009R004C012',
        )
        for version in versions:
            for key, sql in queries:
                adapted = kb.adapt_kingbase_sql(sql, version, key)
                with self.subTest(version=version, key=key):
                    assert_readonly_sql(adapted)
                    self.assertNotIn('pg_stat_checkpointer', adapted)

    def test_default_wal_and_archive_queries_follow_version(self):
        from modules.inspection.init_db import KINGBASE_DEFAULT_CHAPTERS
        queries = {q['key']: q['sql'] for c in KINGBASE_DEFAULT_CHAPTERS
                   for q in c['queries']}
        for key in ('pg_wal_rate', 'pg_is_in_recovery'):
            legacy = kb.adapt_kingbase_sql(
                queries[key], 'KingbaseES V008R003C002B0160', key)
            modern = kb.adapt_kingbase_sql(
                queries[key], 'KingbaseES V008R006C007B0012', key)
            with self.subTest(key=key):
                self.assertNotIn('pg_current_wal_lsn', legacy)
                self.assertIn('pg_current_wal_lsn', modern)

    def test_default_queries_have_chinese_report_labels(self):
        from modules.inspection.init_db import KINGBASE_DEFAULT_CHAPTERS
        from modules.intelligence.schema_knowledge import report_field_labels
        columns = (
            'VERSION', 'UPTIME', 'STATE', 'COUNT', 'WAIT_EVENT_TYPE',
            'WAIT_EVENT', 'SHARED_BUFFERS', 'NAME', 'SETTING', 'UNIT',
            'SHORT_DESC', 'TOTAL_LOCKS', 'GRANTED_LOCKS', 'WAITING_LOCKS',
            'PID', 'USENAME', 'DATNAME', 'APPLICATION_NAME', 'XACT_START',
            'XACT_SECONDS', 'QUERY', 'DATABASE_NAME', 'SIZE',
            'TABLESPACE_NAME', 'SCHEMANAME', 'TABLENAME', 'LIVE_ROWS',
            'DEAD_ROWS', 'DEAD_RATIO', 'LAST_VACUUM', 'LAST_AUTOVACUUM',
            'LAST_ANALYZE', 'LAST_AUTOANALYZE', 'RELNAME', 'N_LIVE_TUP',
            'INDEXNAME', 'IDX_SCAN',
            'IDX_TUP_READ', 'IDX_TUP_FETCH', 'CLIENT_ADDR', 'SENT_LSN',
            'WRITE_LSN', 'FLUSH_LSN', 'REPLAY_LSN', 'SYNC_STATE',
            'CURRENT_WAL_FILE', 'WAL_BYTES_TOTAL', 'CHECKPOINTS_TIMED',
            'CHECKPOINTS_REQUESTED', 'CHECKPOINT_WRITE_TIME',
            'CHECKPOINT_SYNC_TIME', 'BUFFERS_CHECKPOINT', 'STATS_RESET',
            'SPCNAME', 'XID_AGE', 'WRAPAROUND_RISK', 'USESUPER',
            'USECREATEDB', 'USEREPL', 'USEBYPASSRLS', 'HAS_PASSWORD',
            'ROLNAME', 'ROLSUPER', 'ROLINHERIT', 'ROLCREATEROLE',
            'ROLCREATEDB', 'ROLCANLOGIN', 'ROLREPLICATION', 'CONN_COUNT',
            'ACTIVE_COUNT', 'IS_STANDBY', 'LAST_LSN', 'INDEX_SIZE',
            'TABLE_COUNT', 'INDEX_COUNT', 'DEADLOCKS', 'EXTNAME',
            'EXTVERSION', 'EXTRELOCATABLE', 'DEFAULT_VERSION',
            'INSTALLED_VERSION', 'SENT_LOCATION', 'WRITE_LOCATION',
            'FLUSH_LOCATION', 'REPLAY_LOCATION',
        )
        labels = report_field_labels('kingbase', '', list(columns))
        for column, label in zip(columns, labels):
            with self.subTest(column=column):
                self.assertIn('（', label)
                self.assertNotIn('待维护', label)
                self.assertTrue(any('\u4e00' <= ch <= '\u9fff' for ch in label))
        self.assertGreaterEqual(len(KINGBASE_DEFAULT_CHAPTERS), 19)


class KingbaseEngineIntegrationTest(unittest.TestCase):
    def test_engine_rewrites_v8r3_wal_queries(self):
        engine = object.__new__(BaseInspectionEngine)
        engine.db_type = 'kingbase'
        engine.context = {'version': [{
            'VERSION': 'KingbaseES V008R003C002B0160'}]}
        engine._server_version_cache = 'KingbaseES V008R003C002B0160'
        engine._kingbase_version_probed = True
        queries = {
            'pg_wal_rate': (
                'SELECT pg_current_wal_lsn() AS current_wal_lsn'),
            'pg_replication': (
                'SELECT sent_lsn FROM pg_stat_replication'),
            'pg_checkpoint': 'SELECT * FROM pg_stat_bgwriter',
        }
        BaseInspectionEngine._adapt_kingbase_queries(engine, queries)
        self.assertIn('pg_current_xlog_location', queries['pg_wal_rate'])
        self.assertIn('sent_location', queries['pg_replication'])
        self.assertEqual(queries['pg_checkpoint'],
                         'SELECT * FROM pg_stat_bgwriter')

    def test_engine_retries_old_wal_function(self):
        class Cursor(_FakeCursor):
            def execute(self, sql):
                self.executed.append(sql)
                if 'pg_current_wal_lsn' in sql:
                    raise Exception(
                        'function pg_current_wal_lsn() does not exist')
                self._rows = [('0/16B6C50',)]

        engine = object.__new__(BaseInspectionEngine)
        engine.db_type = 'kingbase'
        engine.context = {'version': [{'VERSION': 'V008R003C002B0160'}]}
        engine._server_version_cache = 'V008R003C002B0160'
        engine._kingbase_version_probed = True
        engine.conn = None
        cursor = Cursor([])
        used = BaseInspectionEngine._retry_kingbase_sql(
            engine, cursor,
            'SELECT pg_current_wal_lsn() AS current_wal_lsn',
            'pg_wal_rate',
            Exception('function pg_current_wal_lsn() does not exist'))
        self.assertIn('pg_current_xlog_location', used)
        self.assertEqual(len(cursor.executed), 1)

    def test_engine_retries_pg_view_to_sys_view(self):
        class Cursor(_FakeCursor):
            def execute(self, sql):
                self.executed.append(sql)
                if 'pg_stat_activity' in sql:
                    raise Exception(
                        'relation "pg_stat_activity" does not exist')
                self._rows = [(3,)]

        engine = object.__new__(BaseInspectionEngine)
        engine.db_type = 'kingbase'
        engine.context = {'version': [{'VERSION': 'V009R001C002B0014'}]}
        engine._server_version_cache = 'V009R001C002B0014'
        engine._kingbase_version_probed = True
        engine.conn = None
        cursor = Cursor([])
        used = BaseInspectionEngine._retry_kingbase_sql(
            engine, cursor,
            'SELECT count(*) AS count FROM pg_stat_activity',
            'pg_conn_detail',
            Exception('relation "pg_stat_activity" does not exist'))
        self.assertIn('sys_stat_activity', used)
        self.assertEqual(len(cursor.executed), 1)

    def test_engine_does_not_retry_permission_error(self):
        engine = object.__new__(BaseInspectionEngine)
        engine.db_type = 'kingbase'
        engine.context = {}
        engine._server_version_cache = 'V009R001C002B0014'
        engine._kingbase_version_probed = True
        engine.conn = None
        cursor = _FakeCursor([])
        used = BaseInspectionEngine._retry_kingbase_sql(
            engine, cursor,
            'SELECT * FROM pg_stat_replication',
            'pg_replication',
            Exception('permission denied for view pg_stat_replication'))
        self.assertEqual(used, '')
        self.assertEqual(cursor.executed, [])


if __name__ == '__main__':
    unittest.main()
