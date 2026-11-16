# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""SQL Server 2005–2025 巡检 SQL 版本适配回归。"""

import unittest

from modules.core.data_security import assert_readonly_sql
from modules.inspection import sqlserver_compat as ss
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


class SqlServerVersionTest(unittest.TestCase):
    def test_version_matrix(self):
        cases = {
            '9.00.5000.00': ('sqlserver-2005', 9, 0),
            '10.0.6000.29': ('sqlserver-2008', 10, 0),
            '10.50.6000.34': ('sqlserver-2008r2', 10, 50),
            '11.0.7001.0': ('sqlserver-2012', 11, 0),
            '12.0.2000.8': ('sqlserver-2014', 12, 0),
            '13.0.5026.0': ('sqlserver-2016', 13, 0),
            '14.0.1000.169': ('sqlserver-2017', 14, 0),
            '15.0.4322.2': ('sqlserver-2019', 15, 0),
            '16.0.1050.5': ('sqlserver-2022', 16, 0),
            '17.0.1000.7': ('sqlserver-2025', 17, 0),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                parsed = ss.parse_sqlserver_version(raw)
                self.assertEqual(
                    (parsed.family, parsed.major, parsed.minor), expected)
                self.assertIn('SQL Server', parsed.display)

    def test_full_banner_and_level(self):
        raw = ('Microsoft SQL Server 2019 (RTM-CU22) (KB5030333) - '
               '15.0.4322.2 (X64)')
        parsed = ss.parse_sqlserver_version(raw)
        self.assertEqual(parsed.major, 15)
        self.assertEqual(parsed.product_level, 'RTM-CU22')
        self.assertEqual(parsed.family, 'sqlserver-2019')

    def test_template_mapping(self):
        self.assertEqual(ss.template_version_for_major(9), '2005')
        self.assertEqual(ss.template_version_for_major(10), '2008')
        self.assertEqual(ss.template_version_for_major(12), '2014')
        self.assertIsNone(ss.template_version_for_major(11))
        self.assertIsNone(ss.template_version_for_major(17))
        self.assertIsNone(ss.template_version_for_major(None))

    def test_unknown_version(self):
        self.assertEqual(ss.parse_sqlserver_version('').family, 'unknown')
        self.assertEqual(ss.parse_sqlserver_version('unknown').major, 0)


class SqlServerVersionDetectionTest(unittest.TestCase):
    def test_fetch_prefers_full_version(self):
        cursor = _FakeCursor([
            [('15.0.4322.2', 'RTM-CU22', 'Enterprise Edition',
              'Microsoft SQL Server 2019 (RTM-CU22) - 15.0.4322.2')],
        ])
        text, parsed = ss.fetch_sqlserver_version(cursor)
        self.assertIn('Microsoft SQL Server 2019', text)
        self.assertEqual(parsed.family, 'sqlserver-2019')
        self.assertIn("SERVERPROPERTY('ProductVersion')", cursor.executed[0])

    def test_fetch_falls_back_to_version(self):
        cursor = _FakeCursor([
            Exception('permission denied'),
            [('Microsoft SQL Server 2008 R2 (SP3) - 10.50.6000.34',)],
        ])
        text, parsed = ss.fetch_sqlserver_version(cursor)
        self.assertIn('2008 R2', text)
        self.assertEqual(parsed.family, 'sqlserver-2008r2')
        self.assertIn('@@VERSION', cursor.executed[1])

    def test_fetch_all_fail(self):
        text, parsed = ss.fetch_sqlserver_version(_FakeCursor([
            Exception('no access'), Exception('no access'), Exception('no access')
        ]))
        self.assertEqual(text, '')
        self.assertEqual(parsed.major, 0)


class SqlServerSqlAdaptationTest(unittest.TestCase):
    def test_memory_clerks_on_2005(self):
        sql = ('SELECT TOP 20 type, SUM(pages_kb)/1024.0 AS mb '
               'FROM sys.dm_os_memory_clerks GROUP BY type')
        adapted = ss.adapt_sqlserver_sql(sql, '9.00.5000')
        self.assertIn('SINGLE_PAGES_KB', adapted)
        self.assertIn('MULTI_PAGES_KB', adapted)
        self.assertNotIn('SUM(pages_kb)', adapted)

    def test_memory_clerks_on_2012(self):
        sql = ('SELECT TOP 100 TYPE, SINGLE_PAGES_KB, MULTI_PAGES_KB '
               'FROM SYS.DM_OS_MEMORY_CLERKS')
        adapted = ss.adapt_sqlserver_sql(sql, '11.0.7001')
        self.assertIn('PAGES_KB', adapted)
        self.assertNotIn('SINGLE_PAGES_KB', adapted)
        self.assertNotIn('MULTI_PAGES_KB', adapted)

    def test_uptime_on_2005_uses_tempdb_crdate(self):
        sql = ('SELECT sqlserver_start_time, DATEDIFF(SECOND, '
               'sqlserver_start_time, GETDATE()) FROM sys.dm_os_sys_info')
        adapted = ss.adapt_sqlserver_sql(sql, '9.00.5000')
        self.assertIn("master..sysdatabases", adapted)
        self.assertIn("name = 'tempdb'", adapted)
        self.assertNotIn('sqlserver_start_time', adapted)

    def test_io_stats_on_2005(self):
        sql = ('SELECT DB_NAME(vfs.database_id) AS db_name, mf.physical_name '
               'FROM sys.dm_io_virtual_file_stats(NULL, NULL) vfs '
               'JOIN sys.master_files mf ON vfs.database_id = mf.database_id')
        adapted = ss.adapt_sqlserver_sql(sql, '9.00.5000')
        self.assertIn('::fn_virtualfilestats', adapted)
        self.assertIn('IoStallMS', adapted)
        self.assertNotIn('sys.dm_io_virtual_file_stats', adapted)
        assert_readonly_sql(adapted)

    def test_io_stats_on_modern_version(self):
        sql = ('SELECT * FROM sys.dm_io_virtual_file_stats(NULL, NULL)')
        self.assertEqual(
            ss.adapt_sqlserver_sql(sql, '15.0.4322'),
            sql)

    def test_optional_features_on_old_versions(self):
        cases = [
            ('SELECT * FROM sys.dm_xe_sessions', '9.0.5000', '2005'),
            ('SELECT * FROM sys.resource_governor_resource_pools', '9.0.5000', '2005'),
            ('SELECT * FROM sys.dm_server_services', '10.0.6000', '2008'),
            ('SELECT * FROM sys.availability_groups', '10.50.6000', '2008'),
            ('SELECT * FROM sys.query_store_runtime_stats', '12.0.2000', '2016'),
        ]
        for sql, version, word in cases:
            with self.subTest(sql=sql):
                reason = ss.optional_feature_reason(sql, version)
                self.assertTrue(reason)
                self.assertIn('不适用', reason)
                self.assertIn(word, reason)

    def test_default_2005_unsupported_features_are_marked(self):
        from modules.inspection.init_db import SQLSERVER_DEFAULT_CHAPTERS
        for key in ('error_log_config', 'ag_status', 'resource_pools', 'xe_sessions'):
            sql = next(q['sql'] for chapter in SQLSERVER_DEFAULT_CHAPTERS
                       for q in chapter['queries'] if q['key'] == key)
            with self.subTest(key=key):
                reason = ss.optional_feature_reason(sql, '9.00.5000')
                self.assertTrue(reason)
                self.assertIn('不适用', reason)

    def test_uptime_on_2005_template_is_replaced(self):
        from modules.inspection.init_db import SQLSERVER_DEFAULT_CHAPTERS
        sql = next(q['sql'] for chapter in SQLSERVER_DEFAULT_CHAPTERS
                   for q in chapter['queries'] if q['key'] == 'server_uptime')
        adapted = ss.adapt_sqlserver_sql(sql, '9.00.5000', 'server_uptime')
        self.assertIn('master..sysdatabases', adapted)
        self.assertNotIn('sqlserver_start_time', adapted)

    def test_optional_features_on_supported_versions(self):
        for sql in ('SELECT * FROM sys.dm_xe_sessions',
                    'SELECT * FROM sys.resource_governor_resource_pools',
                    'SELECT * FROM sys.dm_server_services',
                    'SELECT * FROM sys.availability_groups',
                    'SELECT * FROM sys.query_store_runtime_stats'):
            with self.subTest(sql=sql):
                self.assertIsNone(ss.optional_feature_reason(sql, '15.0.4322'))

    def test_distribution_db_absence_is_optional(self):
        cursor = _FakeCursor([[(None,)]])
        reason = ss.optional_feature_reason(
            'SELECT publisher_db FROM distribution.dbo.MSpublications',
            '15.0.4322', cursor)
        self.assertIn('distribution', reason)

    def test_error_classification(self):
        for error in ('Invalid column name \'PAGES_KB\'.',
                      "Invalid object name 'sys.dm_xe_sessions'.",
                      'Incorrect syntax near ...',
                      "The multi-part identifier 'vfs.database_id' could not be bound.",
                      '列名无效', '对象名不存在'):
            with self.subTest(error=error):
                self.assertTrue(ss.is_sqlserver_compatibility_error(error))
        for error in ('Login failed for user', 'permission denied',
                      'connection timeout', '登录失败', '权限不足'):
            with self.subTest(error=error):
                self.assertFalse(ss.is_sqlserver_compatibility_error(error))


class SqlServerTemplateMatrixTest(unittest.TestCase):
    def test_all_default_queries_are_safe_for_supported_versions(self):
        from modules.inspection.init_db import SQLSERVER_DEFAULT_CHAPTERS
        queries = [q['sql'] for c in SQLSERVER_DEFAULT_CHAPTERS
                   for q in c['queries']]
        self.assertGreaterEqual(len(queries), 30)
        for version in ('9.0.5000', '10.0.6000', '10.50.6000',
                        '11.0.7001', '12.0.2000', '13.0.5026',
                        '14.0.1000', '15.0.4322', '16.0.1050', '17.0.1000'):
            for sql in queries:
                adapted = ss.adapt_sqlserver_sql(sql, version)
                with self.subTest(version=version, sql=sql[:80]):
                    assert_readonly_sql(adapted)

    def test_compatibility_templates_are_safe(self):
        from modules.inspection.init_db import compatibility_chapters
        for version in ('2005', '2008', '2014'):
            for chapter in compatibility_chapters('sqlserver', version):
                for query in chapter['queries']:
                    with self.subTest(version=version, query=query['key']):
                        adapted = ss.adapt_sqlserver_sql(
                            query['sql'], version)
                        assert_readonly_sql(adapted)

    def test_business_old_version_sql_gets_candidates(self):
        from modules.inspection.business_templates import SQLSERVER
        for key in ('H12', 'H20'):
            sql = SQLSERVER[key][2]
            adapted = ss.adapt_sqlserver_sql(sql, '9.0.5000')
            with self.subTest(key=key):
                self.assertIn('fn_virtualfilestats', adapted)
                assert_readonly_sql(adapted)


class SqlServerEngineIntegrationTest(unittest.TestCase):
    def test_engine_rewrites_memory_clerks(self):
        engine = object.__new__(BaseInspectionEngine)
        engine.db_type = 'sqlserver'
        engine.context = {'version': [{
            'VERSION': 'Microsoft SQL Server 2005 - 9.00.5000.00'}]}
        engine._server_version_cache = 'Microsoft SQL Server 2005 - 9.00.5000.00'
        queries = {'H22b_memory_clerk': (
            'SELECT TOP 20 type, SUM(pages_kb)/1024.0 AS mb '
            'FROM sys.dm_os_memory_clerks GROUP BY type')}
        BaseInspectionEngine._adapt_sqlserver_queries(engine, queries)
        self.assertIn('SINGLE_PAGES_KB', queries['H22b_memory_clerk'])

    def test_engine_retries_invalid_memory_column(self):
        class Cursor(_FakeCursor):
            def execute(self, sql):
                self.executed.append(sql)
                if 'PAGES_KB' in sql.upper() and 'SINGLE_PAGES_KB' not in sql.upper():
                    raise Exception("Invalid column name 'PAGES_KB'.")
                self._rows = [('MEMORYCLERK_SQLBUFFERPOOL', 128)]

        engine = object.__new__(BaseInspectionEngine)
        engine.db_type = 'sqlserver'
        engine.context = {'version': [{'VERSION': '9.00.5000'}]}
        engine._server_version_cache = '9.00.5000'
        engine.conn = None
        cursor = Cursor([])
        used = BaseInspectionEngine._retry_sqlserver_sql(
            engine, cursor,
            'SELECT TOP 20 type, SUM(pages_kb)/1024.0 AS mb '
            'FROM sys.dm_os_memory_clerks GROUP BY type',
            'H22b_memory_clerk',
            Exception("Invalid column name 'PAGES_KB'."))
        self.assertIn('SINGLE_PAGES_KB', used)
        self.assertEqual(len(cursor.executed), 1)


if __name__ == '__main__':
    unittest.main()
