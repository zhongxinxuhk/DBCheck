# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""GBase 8a / 8s 巡检 SQL 版本与方言适配回归。"""

import sqlite3
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modules.inspection import gbase_compat as gb
from modules.inspection.engine import BaseInspectionEngine

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DB_PATH = _REPO_ROOT / 'data' / 'inspection.db'


@pytest.mark.parametrize(('code', 'message', 'hint'), [
    (1816, "Unknown VC 'gbase'", "虚拟集群 'gbase'（错误 1816）"),
    (1064, 'SQL syntax error', '切换虚拟集群的命令（错误 1064）'),
])
def test_gbase8a_rejected_vc_does_not_change_inspection_scope(code, message, hint):
    import pymysql

    from modules.db_types.compatibility import connect_gbase8a

    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    error = pymysql.OperationalError(code, message)
    cursor.execute.side_effect = error
    with patch('pymysql.connect', return_value=connection) as connect:
        with pytest.raises(ValueError, match=hint) as caught:
            connect_gbase8a(dict(host='127.0.0.1', port=5258, user='fixture',
                                 password='fixture', database='gbase'))

    assert caught.value.__cause__ is error
    cursor.execute.assert_called_once_with('USE VC gbase')
    connection.close.assert_called_once()
    connect.assert_called_once()


def test_gbase8a_without_vc_leaves_account_scope_in_place():
    from modules.db_types.compatibility import connect_gbase8a

    connection = MagicMock()
    with patch('pymysql.connect', return_value=connection):
        result = connect_gbase8a(dict(host='127.0.0.1', port=5258, user='fixture',
                                      password='fixture', database=''))

    assert result is connection
    connection.cursor.assert_not_called()


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


class GbaseVersionParseTest(unittest.TestCase):
    def test_gbase8a_version_matrix(self):
        cases = {
            '9.5.3.28.12509af275af GBase Enterprise Server':
                ('gbase8a-9.5.3+', 9, 5, 3, 28),
            'GBase 8a MPP Cluster V9.5.3.27':
                ('gbase8a-9.5.3+', 9, 5, 3, 27),
            'GBase 8a MPP Cluster V9.5.2.1':
                ('gbase8a-9.5.2', 9, 5, 2, 1),
            'GBase 8a MPP Cluster V8.6.2.1':
                ('gbase8a-8.6', 8, 6, 2, 1),
            'GBase 8a MPP Cluster V8.5.2':
                ('gbase8a-8.5', 8, 5, 2, 0),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                parsed = gb.parse_gbase8a_version(raw)
                self.assertEqual(
                    (parsed.family, parsed.major, parsed.minor,
                     parsed.patch, parsed.build), expected)

    def test_gbase8s_version_matrix(self):
        cases = {
            'GBase 8s V8.8.0.0': ('gbase8s-8.8', 8, 8, 0, 0),
            'GBase 8s 8.5.1': ('gbase8s-8.5', 8, 5, 1, 0),
            '12.10.FC12W1': ('gbase8s-12.10', 12, 10, 0, 0),
            'GBase 8s': ('gbase8s-8.0', 8, 0, 0, 0),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                parsed = gb.parse_gbase8s_version(raw)
                self.assertEqual(
                    (parsed.family, parsed.major, parsed.minor,
                     parsed.patch, parsed.build), expected)

    def test_unknown_version_is_conservative(self):
        self.assertEqual(gb.parse_gbase8a_version('').family, 'unknown')
        self.assertEqual(gb.parse_gbase8s_version('').family, 'unknown')


class GbaseVersionDetectionTest(unittest.TestCase):
    def test_gbase8a_version_prefers_gbase_marker(self):
        cursor = _FakeCursor([
            [('9.5.3.28.12509af275af GBase Enterprise Server',)],
        ])
        text, parsed = gb.fetch_gbase8a_version(cursor)
        self.assertIn('GBase', text)
        self.assertEqual(parsed.family, 'gbase8a-9.5.3+')
        self.assertEqual(cursor.executed[0], 'SELECT VERSION()')

    def test_gbase8a_falls_back_to_comment(self):
        cursor = _FakeCursor([
            Exception('unknown column'),
            [('5.7.36-log',)],
            [('GBase 8a MPP Cluster V8.6.2.1',)],
        ])
        text, parsed = gb.fetch_gbase8a_version(cursor)
        self.assertEqual(parsed.family, 'gbase8a-8.6')
        self.assertIn('8.6.2.1', text)

    def test_gbase8s_version_candidates(self):
        cursor = _FakeCursor([
            Exception('invalid column'),
            [('GBase 8s V8.8.0.0',)],
        ])
        text, parsed = gb.fetch_gbase8s_version(cursor)
        self.assertEqual(parsed.family, 'gbase8s-8.8')
        self.assertIn('V8.8', text)
        self.assertIn("DBINFO('version', 'full')", cursor.executed[0])


class GbaseSqlAdaptationTest(unittest.TestCase):
    def test_gbase8s_sysmaster_prefix_and_limit(self):
        sql = ('SELECT tabid, COUNT(*) AS column_count FROM syscolumns '
               'GROUP BY tabid ORDER BY column_count DESC LIMIT 10')
        adapted = gb.adapt_gbase8s_sql(sql, 'GBase 8s V8.8', 'gbase_column_stats')
        self.assertTrue(adapted.startswith('SELECT FIRST 10 '))
        self.assertNotIn('LIMIT 10', adapted)
        self.assertIn('FROM syscolumns', adapted)

    def test_gbase8s_prefixes_join_views_once(self):
        sql = ('SELECT s.name FROM sysdbspaces s '
               'JOIN syschunks c ON s.dbsnum=c.dbsnum')
        adapted = gb.adapt_gbase8s_sql(sql)
        self.assertIn('FROM sysmaster:sysdbspaces', adapted)
        self.assertIn('JOIN sysmaster:syschunks', adapted)
        self.assertNotIn('sysmaster:sysmaster:', adapted)
        self.assertEqual(adapted.count('sysmaster:sysdbspaces'), 1)

    def test_gbase8s_old_dbspace_query_is_replaced(self):
        old = ('SELECT name AS dbspace_name, pagesize, nchunks, fchunk, '
               'create_size, extend_size, max_size FROM sysdbspaces ORDER BY name')
        adapted = gb.adapt_gbase8s_sql(old, '', 'gbase_dbspace_usage')
        self.assertIn('sysmaster:syschunks', adapted)
        self.assertIn('used_pct', adapted)
        self.assertNotIn('create_size', adapted)

    def test_gbase8s_known_legacy_queries_are_normalized(self):
        version = gb.adapt_gbase8s_sql(
            'SELECT DBINFO("version", "full") AS version '
            'FROM systables WHERE tabid = 1', '', 'gbase_version')
        status = gb.adapt_gbase8s_sql(
            'SELECT COUNT(*) AS table_count FROM systables '
            'WHERE tabtype = "T"', '', 'gbase_instance_status')
        self.assertIn("DBINFO('version', 'full')", version)
        self.assertNotIn('"', version)
        self.assertIn("tabtype = 'T'", status)
        self.assertNotIn('"', status)

    def test_gbase8a_table_locks_falls_back_to_processlist(self):
        sql = 'SELECT * FROM information_schema.TABLE_LOCKS'
        candidates = gb.gbase8a_query_candidates(sql, '9.5.3', 'gbase8a_lock')
        self.assertTrue(any('information_schema.PROCESSLIST' in item
                            for item in candidates))

    def test_gbase8a_cluster_view_falls_back(self):
        sql = 'SELECT * FROM information_schema.CLUSTER_MONIT_INFO'
        candidates = gb.gbase8a_query_candidates(sql, '8.6.2', 'gbase8a_ha')
        self.assertTrue(any('SHOW GCLUSTER NODES' in item for item in candidates))

    def test_optional_views_have_clear_reasons(self):
        for view in ('CLUSTER_MONIT_INFO', 'GNODES_RESOURCE_POOL_USAGE',
                     'GNODES_USER_DISKSPACE_USAGE', 'TABLE_LOCKS',
                     'COORDINATORS_TASK_INFORMATION', 'EVENTS'):
            with self.subTest(view=view):
                reason = gb.optional_view_reason('SELECT * FROM %s' % view, 'x')
                self.assertTrue(reason)
                self.assertIn('已跳过', reason)

    def test_compatibility_error_markers(self):
        for error in ('Unknown column \'X\' in field list',
                      "Table 'information_schema.EVENTS' doesn't exist",
                      'You have an error in your SQL syntax',
                      '无效的列名[FOO]', '表或视图不存在'):
            with self.subTest(error=error):
                self.assertTrue(gb.is_gbase_compatibility_error(error))
        for error in ('Access denied for user', 'Connection reset',
                      '权限不足', 'timeout'):
            with self.subTest(error=error):
                self.assertFalse(gb.is_gbase_compatibility_error(error))


class GbaseEngineIntegrationTest(unittest.TestCase):
    def test_engine_rewrites_gbase8s_template_queries(self):
        engine = object.__new__(BaseInspectionEngine)
        engine.db_type = 'gbase'
        engine.context = {'version': [{'VERSION': 'GBase 8s V8.8.0.0'}]}
        engine._server_version_cache = 'GBase 8s V8.8.0.0'
        queries = {
            'gbase_locks_detail': 'SELECT * FROM syslocks LIMIT 50',
            'gbase_dbspace_usage': 'SELECT * FROM sysdbspaces',
        }
        BaseInspectionEngine._adapt_gbase_queries(engine, queries)
        self.assertIn('sysmaster:syslocks', queries['gbase_locks_detail'])
        self.assertTrue(queries['gbase_locks_detail'].startswith('SELECT FIRST 50'))
        self.assertIn('sysmaster:sysdbspaces', queries['gbase_dbspace_usage'])

    def test_engine_retries_gbase8a_table_locks(self):
        class Cursor(_FakeCursor):
            def execute(self, sql):
                self.executed.append(sql)
                if 'TABLE_LOCKS' in sql:
                    raise Exception("Table 'information_schema.TABLE_LOCKS' doesn't exist")
                self._rows = [(1, 'ok')]

        engine = object.__new__(BaseInspectionEngine)
        engine.db_type = 'gbase8a'
        engine.context = {'version': [{'VERSION': 'GBase 8a MPP Cluster V9.5.3.27'}]}
        engine._server_version_cache = 'GBase 8a MPP Cluster V9.5.3.27'
        engine.database = 'vc1'
        engine.conn = None
        cursor = Cursor([])
        used = BaseInspectionEngine._retry_gbase_sql(
            engine, cursor, 'SELECT * FROM information_schema.TABLE_LOCKS',
            'gbase8a_lock',
            Exception("Table 'information_schema.TABLE_LOCKS' doesn't exist"))
        self.assertIn('PROCESSLIST', used)
        self.assertFalse(any(item.startswith('USE VC ') for item in cursor.executed))


class GbaseTemplateSqlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _DB_PATH.exists():
            raise unittest.SkipTest('inspection.db not found')

    def _queries(self, db_type):
        conn = sqlite3.connect(str(_DB_PATH))
        try:
            return [row[0] for row in conn.execute(
                'SELECT DISTINCT q.query_sql FROM inspection_query q '
                'JOIN inspection_chapter c ON q.chapter_id=c.id '
                'JOIN inspection_template t ON c.template_id=t.id '
                'WHERE t.db_type=? AND q.enabled=1', (db_type,))]
        finally:
            conn.close()

    def test_all_gbase8s_queries_are_adapted(self):
        from modules.inspection.init_db import GBASE_DEFAULT_CHAPTERS
        queries = [q['sql'] for ch in GBASE_DEFAULT_CHAPTERS
                   for q in ch['queries']]
        self.assertGreaterEqual(len(queries), 20)
        for sql in queries:
            adapted = gb.adapt_gbase8s_sql(sql, 'GBase 8s V8.8')
            with self.subTest(sql=sql[:80]):
                self.assertNotIn(' LIMIT ', ' ' + adapted.upper() + ' ')
                self.assertNotIn(' FROM SYSDATABASES', adapted.upper())
                self.assertNotIn(' FROM SYSCONFIG', adapted.upper())
                self.assertNotIn(' FROM SYSSESSIONS', adapted.upper())
                self.assertNotIn(' FROM SYSLOCKS', adapted.upper())

    def test_all_gbase8s_database_queries_are_adapted(self):
        queries = self._queries('gbase')
        self.assertGreaterEqual(len(queries), 20)
        for sql in queries:
            adapted = gb.adapt_gbase8s_sql(sql, 'GBase 8s V8.8')
            with self.subTest(sql=sql[:80]):
                self.assertNotIn(' LIMIT ', ' ' + adapted.upper() + ' ')
                self.assertNotIn(' FROM SYSDATABASES', adapted.upper())
                self.assertNotIn(' FROM SYSCONFIG', adapted.upper())
                self.assertNotIn('DBINFO("VERSION"', adapted.upper())

    def test_all_gbase8a_template_queries_round_trip(self):
        queries = self._queries('gbase8a')
        self.assertGreaterEqual(len(queries), 50)
        for sql in queries:
            with self.subTest(sql=sql[:80]):
                candidates = gb.gbase8a_query_candidates(sql, '9.5.3.28')
                self.assertTrue(candidates)
                self.assertEqual(candidates[0],
                                 gb.adapt_gbase8a_sql(sql, '9.5.3.28'))


if __name__ == '__main__':
    unittest.main()
