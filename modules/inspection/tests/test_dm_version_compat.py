# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""达梦 DM6/DM7/DM8 巡检 SQL 版本适配回归。"""

import sqlite3
import unittest
from pathlib import Path

from modules.inspection import dm_compat as dm
from modules.inspection.engine import BaseInspectionEngine


_REPO_ROOT = Path(__file__).resolve().parents[3]
_DB_PATH = _REPO_ROOT / 'data' / 'inspection.db'


class _FakeCursor:
    """只实现版本探测所需的 execute/fetchall。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self._rows = []
        self.executed = []

    def execute(self, sql):
        self.executed.append(sql)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        self._rows = item

    def fetchall(self):
        return self._rows


class DmVersionParseTest(unittest.TestCase):
    def test_version_matrix(self):
        cases = {
            'V7.6.0.145-Build(2019.03.20-104220)ENT': ('dm7', 7, 6, 0, 145),
            'DM Database Server x64 V7.6.0.183-Build': ('dm7', 7, 6, 0, 183),
            '1-0-147-2019.03.28-123456-ENT': ('dm8.1.0', 8, 1, 0, 147),
            '1-1-48-19.11.29-116408-ENT': ('dm8.1.1', 8, 1, 1, 48),
            '1-2-46-21.08.20-145962-10022-ENT': ('dm8.1.2', 8, 1, 2, 46),
            '1-3-100-2023.12.26-213242-20081-ENT': ('dm8.1.3', 8, 1, 3, 100),
            '8.1.3.100': ('dm8.1.3', 8, 1, 3, 100),
            'V8.1.1.126': ('dm8.1.1', 8, 1, 1, 126),
            'DM Database Server 64 V8': ('dm8', 8, 0, 0, 0),
            'V8': ('dm8', 8, 0, 0, 0),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                parsed = dm.parse_dm_version(raw)
                self.assertEqual(
                    (parsed.family, parsed.major, parsed.minor,
                     parsed.patch, parsed.build), expected)
                self.assertEqual(dm.dm_family(raw), expected[0])

    def test_unknown_version_is_conservative(self):
        parsed = dm.parse_dm_version('')
        self.assertEqual(parsed.major, 0)
        self.assertEqual(parsed.family, 'unknown')
        self.assertFalse(parsed.supports_build_version)
        self.assertIsNone(dm.optional_view_reason('SELECT 1 FROM DUAL'))

    def test_date_is_not_treated_as_version(self):
        parsed = dm.parse_dm_version('2023.12.26')
        self.assertEqual(parsed.major, 0)


class DmVersionDetectionTest(unittest.TestCase):
    def test_dm8_id_code_is_preferred(self):
        cursor = _FakeCursor([[('1-3-100-2023.12.26-213242-20081-ENT',)]])
        text, parsed = dm.fetch_server_version(cursor)
        self.assertEqual(parsed.family, 'dm8.1.3')
        self.assertIn('8.1.3.100', text)
        self.assertIn('ID_CODE', text)
        self.assertEqual(cursor.executed[0], 'SELECT ID_CODE FROM V$VERSION')

    def test_dm8_early_version_uses_id_code(self):
        cursor = _FakeCursor([[('1-0-147-2019.03.28-123456-ENT',)]])
        text, parsed = dm.fetch_server_version(cursor)
        self.assertEqual(parsed.family, 'dm8.1.0')
        self.assertIn('8.1.0.147', text)

    def test_dm7_falls_back_to_banner(self):
        cursor = _FakeCursor([
            Exception('invalid column'),
            Exception('invalid column'),
            Exception('invalid column'),
            [('DM Database Server x64 V7.6.0.145-Build(2019.03.20-104220)ENT',)],
        ])
        text, parsed = dm.fetch_server_version(cursor)
        self.assertEqual(parsed.family, 'dm7')
        self.assertIn('V7.6.0.145', text)

    def test_dm8_generic_banner(self):
        cursor = _FakeCursor([
            Exception('invalid column'),
            Exception('invalid column'),
            Exception('invalid column'),
            [('DM Database Server 64 V8',)],
        ])
        text, parsed = dm.fetch_server_version(cursor)
        self.assertEqual(parsed.family, 'dm8')
        self.assertIn('V8', text)


class DmSqlAdaptationTest(unittest.TestCase):
    def test_session_state_filter_handles_all_state_names(self):
        sqls = [
            "SELECT * FROM V$SESSIONS WHERE STATE <> 'IDLE'",
            "SELECT * FROM V$SESSIONS WHERE STATE != 'IDLE'",
        ]
        for sql in sqls:
            for version in ('V7.6.0.145', '1-0-147', '1-1-48', '1-3-100',
                            'DM Database Server 64 V8'):
                with self.subTest(sql=sql, version=version):
                    adapted = dm.adapt_dm_sql(sql, version)
                    self.assertIn("UPPER(STATE) NOT IN", adapted)
                    self.assertNotIn("'IDLE'", adapted.replace(
                        "UPPER(STATE) NOT IN ('IDLE'", ''))

    def test_parameter_type_filter_uses_read_only(self):
        sql = ('SELECT NAME, TYPE, VALUE FROM V$PARAMETER '
               "WHERE TYPE != 'R' ORDER BY NAME")
        adapted = dm.adapt_dm_sql(sql, '1-3-100')
        self.assertIn("TYPE <> 'READ ONLY'", adapted)
        self.assertNotIn("TYPE != 'R'", adapted)

    def test_limit_is_kept_for_dm7_and_dm8(self):
        sql = 'SELECT * FROM DBA_TABLES ORDER BY NUM_ROWS DESC LIMIT 20'
        for version in ('V7.6.0.145', '1-1-48', '1-3-100'):
            with self.subTest(version=version):
                self.assertIn('LIMIT 20', dm.adapt_dm_sql(sql, version))

    def test_sql_history_falls_back_to_long_exec_sqls(self):
        sql = ('SELECT SQL_ID, TOP_SQL_TEXT, TIME_USED FROM V$SQL_HISTORY '
               'ORDER BY TIME_USED DESC LIMIT 10')
        candidates = dm.dm_query_candidates(sql, '1-1-48', 'top_sql_elapsed')
        self.assertGreaterEqual(len(candidates), 2)
        self.assertTrue(any('V$LONG_EXEC_SQLS' in item for item in candidates))
        self.assertTrue(any('V$SQL_HISTORY' in item for item in candidates))

    def test_simple_query_gets_select_star_fallback(self):
        sql = 'SELECT USERNAME, PROFILE FROM DBA_USERS ORDER BY USERNAME'
        candidates = dm.dm_query_candidates(sql, '1-0-147', 'dba_users')
        self.assertTrue(any(item.strip() == 'SELECT * FROM DBA_USERS'
                            for item in candidates))

    def test_optional_views_have_clear_reasons(self):
        for view in ('V$DSC_EP_INFO', 'V$DMWATCHER', 'V$MAL_INFO',
                     'V$MAL_LINK_STATUS', 'V$ARCH_STATUS', 'V$RAPPLY_STAT'):
            with self.subTest(view=view):
                reason = dm.optional_view_reason(
                    'SELECT * FROM %s' % view, 'cluster')
                self.assertTrue(reason)
                self.assertIn('已跳过', reason)

    def test_compatibility_error_markers(self):
        for error in (
            '第1行附近出现错误: 无效的列名[BUILD_VERSION]',
            '无效的表或视图名[DBA_REGISTRY_SQLPATCH]',
            'invalid column name',
            'table or view does not exist',
            'syntax error near LIMIT',
            '-2116: invalid column',
        ):
            with self.subTest(error=error):
                self.assertTrue(dm.is_compatibility_error(error))
        for error in ('权限不足', 'permission denied',
                      'Connection reset by peer', '登录失败'):
            with self.subTest(error=error):
                self.assertFalse(dm.is_compatibility_error(error))


class DmEngineIntegrationTest(unittest.TestCase):
    def test_engine_rewrites_dm_template_queries(self):
        engine = object.__new__(BaseInspectionEngine)
        engine.db_type = 'dm8'
        engine.context = {'version': [{'VERSION': 'DM8 8.1.2.46'}]}
        engine._server_version_cache = 'DM8 8.1.2.46'
        queries = {
            'active_sessions': (
                "SELECT * FROM V$SESSIONS WHERE STATE <> 'IDLE'"),
            'dm_params': "SELECT * FROM V$PARAMETER WHERE TYPE != 'R'",
        }
        BaseInspectionEngine._adapt_dm_queries(engine, queries)
        self.assertIn('UPPER(STATE) NOT IN', queries['active_sessions'])
        self.assertIn("TYPE <> 'READ ONLY'", queries['dm_params'])


class _RetryCursor:
    def __init__(self):
        self.executed = []
        self._rows = []

    def execute(self, sql):
        self.executed.append(sql)
        if 'V$SQL_HISTORY' in sql.upper():
            raise Exception('第1行附近出现错误: 无效的列名[TOP_SQL_TEXT]')
        self._rows = [(1, 'ok')]

    def fetchall(self):
        return self._rows


class DmEngineRetryTest(unittest.TestCase):
    def test_structural_error_retries_known_view_fallback(self):
        engine = object.__new__(BaseInspectionEngine)
        engine.db_type = 'dm8'
        engine.context = {'version': [{'VERSION': 'DM8 8.1.1.48'}]}
        engine._server_version_cache = 'DM8 8.1.1.48'
        engine.conn = None
        cursor = _RetryCursor()
        sql = ('SELECT SQL_ID, TOP_SQL_TEXT FROM V$SQL_HISTORY '
               'ORDER BY TIME_USED DESC LIMIT 10')
        used = BaseInspectionEngine._retry_dm_sql(
            engine, cursor, sql, 'top_sql_elapsed',
            Exception('无效的列名[TOP_SQL_TEXT]'))
        self.assertIn('V$LONG_EXEC_SQLS', used)
        self.assertTrue(cursor.executed)

    def test_permission_error_is_not_retried(self):
        engine = object.__new__(BaseInspectionEngine)
        engine.db_type = 'dm8'
        engine.context = {'version': [{'VERSION': 'DM8 8.1.1.48'}]}
        engine._server_version_cache = 'DM8 8.1.1.48'
        engine.conn = None
        cursor = _RetryCursor()
        used = BaseInspectionEngine._retry_dm_sql(
            engine, cursor, 'SELECT * FROM V$DMWATCHER', 'dm_watcher',
            Exception('权限不足'))
        self.assertEqual(used, '')
        self.assertFalse(cursor.executed)


class DmTemplateSqlTest(unittest.TestCase):
    """直接检查 inspection.db 中的达梦模板，防止静态 SQL 回归。"""

    @classmethod
    def setUpClass(cls):
        if not _DB_PATH.exists():
            raise unittest.SkipTest('inspection.db not found')

    def _queries(self):
        conn = sqlite3.connect(str(_DB_PATH))
        try:
            return [row[0] for row in conn.execute(
                "SELECT DISTINCT q.query_sql FROM inspection_query q "
                "JOIN inspection_chapter c ON q.chapter_id=c.id "
                "JOIN inspection_template t ON c.template_id=t.id "
                "WHERE t.db_type='dm8' AND q.enabled=1")]
        finally:
            conn.close()

    def test_all_templates_are_safe_for_all_version_families(self):
        queries = self._queries()
        self.assertGreaterEqual(len(queries), 30)
        for version in ('V7.6.0.145', '1-0-147', '1-1-48',
                        '1-2-46', '1-3-100', 'DM Database Server 64 V8'):
            for sql in queries:
                adapted = dm.adapt_dm_sql(sql, version)
                with self.subTest(version=version, sql=sql[:80]):
                    self.assertNotIn('FETCH FIRST', adapted.upper())
                    self.assertNotIn('FETCH NEXT', adapted.upper())
                    self.assertNotIn('TYPE != \'R\'', adapted)
                    self.assertNotIn("STATE <> 'IDLE'", adapted)

    def test_all_templates_have_fallback_chain(self):
        queries = self._queries()
        for sql in queries:
            with self.subTest(sql=sql[:80]):
                candidates = dm.dm_query_candidates(sql, 'V7.6.0.145')
                self.assertTrue(candidates)
                self.assertEqual(candidates[0],
                                 dm.adapt_dm_sql(sql, 'V7.6.0.145'))


if __name__ == '__main__':
    unittest.main()
