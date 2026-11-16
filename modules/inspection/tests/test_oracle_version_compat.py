# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Oracle 10g-23ai 巡检 SQL 版本适配回归。"""

import sqlite3
import unittest
from pathlib import Path

from modules.inspection import oracle_compat as ora


_REPO_ROOT = Path(__file__).resolve().parents[3]
_DB_PATH = _REPO_ROOT / 'data' / 'inspection.db'


class OracleVersionParseTest(unittest.TestCase):
    def test_version_matrix(self):
        cases = {
            '10.2.0.5.0': '10g',
            '11.2.0.4.0': '11g',
            '12.1.0.2.0': '12c',
            '18.0.0.0.0': '18c',
            '19.0.0.0.0': '19c',
            '21.0.0.0.0': '21c',
            '23.0.0.0.0': '23ai',
            'Oracle Database 19c Enterprise Edition Release 19.0.0.0.0': '19c',
            '23ai': '23ai',
            '11': '11g',
        }
        for version, family in cases.items():
            with self.subTest(version=version):
                self.assertEqual(ora.parse_version(version).family, family)
                self.assertEqual(ora.oracle_family(version), family)

    def test_unknown_version_is_conservative(self):
        parsed = ora.parse_version('')
        self.assertEqual(parsed.major, 0)
        self.assertEqual(parsed.family, 'unknown')
        self.assertFalse(parsed.supports_fetch_first)
        self.assertIsNone(ora.oracle_version_major(''))


class OracleSqlAdaptationTest(unittest.TestCase):
    def test_fetch_first_rewritten_for_10g_11g(self):
        sql = ('SELECT sequence#, first_time FROM v$archived_log '
               'ORDER BY sequence# DESC FETCH FIRST 20 ROWS ONLY;')
        for version in ('10.2.0.5.0', '11.2.0.4.0', ''):
            with self.subTest(version=version):
                adapted = ora.adapt_oracle_sql(sql, version)
                self.assertNotIn('FETCH FIRST', adapted.upper())
                self.assertIn('ROWNUM <= 20', adapted)
                self.assertIn('v$archived_log', adapted)

    def test_fetch_first_kept_for_12c_and_newer(self):
        sql = ('SELECT sequence# FROM v$archived_log '
               'ORDER BY sequence# DESC FETCH FIRST 20 ROWS ONLY')
        for version in ('12.1.0.2.0', '18.0.0.0.0', '19.0.0.0.0',
                        '21.0.0.0.0', '23.0.0.0.0'):
            with self.subTest(version=version):
                self.assertIn('FETCH FIRST 20 ROWS ONLY',
                              ora.adapt_oracle_sql(sql, version))

    def test_fetch_with_offset_is_left_for_candidate_fallback(self):
        sql = ('SELECT sequence# FROM v$archived_log ORDER BY sequence# '
               'OFFSET 5 ROWS FETCH NEXT 10 ROWS ONLY')
        adapted = ora.adapt_oracle_sql(sql, '11.2.0.4.0')
        self.assertIn('FETCH NEXT 10 ROWS ONLY', adapted)

    def test_registry_sqlpatch_switched_to_history_before_12c(self):
        sql = ('SELECT patch_id, patch_uid, action, action_time, '
               'description, status FROM dba_registry_sqlpatch '
               'ORDER BY patch_id DESC;')
        for version in ('10.2.0.5.0', '11.2.0.4.0'):
            with self.subTest(version=version):
                adapted = ora.adapt_oracle_sql(sql, version)
                self.assertNotIn('dba_registry_sqlpatch', adapted.lower())
                self.assertIn('registry$history', adapted.lower())
                self.assertIn('id AS patch_id', adapted)

        for version in ('12.1.0.2.0', '19.0.0.0.0', '23.0.0.0.0'):
            with self.subTest(version=version):
                self.assertIn('dba_registry_sqlpatch',
                              ora.adapt_oracle_sql(sql, version).lower())

    def test_10g_authentication_type_uses_external_name(self):
        sql = ('SELECT username, account_status, authentication_type '
               'FROM dba_users ORDER BY username;')
        adapted = ora.adapt_oracle_sql(sql, '10.2.0.5.0')
        self.assertIn('external_name', adapted.lower())
        self.assertIn('AS authentication_type', adapted)
        self.assertEqual(adapted.lower().count('authentication_type'), 1)

        for version in ('11.2.0.4.0',):
            with self.subTest(version=version):
                adapted = ora.adapt_oracle_sql(sql, version)
                self.assertIn('external_name', adapted.lower())

        for version in ('12.1.0.2.0', '19.0.0.0.0', '23.0.0.0.0'):
            with self.subTest(version=version):
                self.assertIn('authentication_type',
                              ora.adapt_oracle_sql(sql, version))

    def test_10g_awr_baseline_uses_start_snap_time(self):
        sql = ('SELECT baseline_id, baseline_name, start_snap_id, end_snap_id, '
               "TO_CHAR(creation_time, 'YYYY-MM-DD HH24:MI') AS created "
               'FROM dba_hist_baseline ORDER BY baseline_id')
        adapted = ora.adapt_oracle_sql(sql, '10.2.0.5.0')
        self.assertIn('start_snap_time', adapted)
        self.assertNotIn('creation_time', adapted)
        for version in ('11.2.0.4.0', '19.0.0.0.0'):
            with self.subTest(version=version):
                self.assertIn('creation_time',
                              ora.adapt_oracle_sql(sql, version))

    def test_pluggable_columns_removed_for_all_versions(self):
        sql = ('SELECT DBID, NAME, CDB, PLUGGABLE_DB, PLUGGABLE_NDB '
               'FROM v$database')
        for version in ('11.2.0.4.0', '19.0.0.0.0', '23.0.0.0.0'):
            with self.subTest(version=version):
                adapted = ora.adapt_oracle_sql(sql, version)
                self.assertNotIn('PLUGGABLE_DB', adapted)
                self.assertNotIn('PLUGGABLE_NDB', adapted)
                self.assertIn('CDB', adapted)

    def test_patch_candidates_prefer_version_specific_view(self):
        legacy = ora.oracle_patch_query_candidates('11.2.0.4.0')
        self.assertIn('registry$history', legacy[0])
        self.assertTrue(any('dba_registry_history' in sql for sql in legacy))

        modern = ora.oracle_patch_query_candidates('19.0.0.0.0')
        self.assertIn('dba_registry_sqlpatch', modern[0])
        self.assertTrue(any('registry$history' in sql for sql in modern))

    def test_candidates_include_compatible_fallbacks(self):
        sql = ('SELECT patch_id, patch_uid, action, action_time, '
               'description, status FROM dba_registry_sqlpatch '
               'ORDER BY patch_id DESC')
        candidates = ora.oracle_query_candidates(
            sql, '19.0.0.0.0', 'registry_sqlpatch', failed_sql=sql)
        self.assertTrue(candidates)
        self.assertTrue(any('registry$history' in item for item in candidates))
        self.assertTrue(any('dba_registry_history' in item for item in candidates))

        legacy = ora.oracle_query_candidates(
            'SELECT 1 FROM dual FETCH FIRST 1 ROWS ONLY', '11.2.0.4.0')
        self.assertTrue(any('ROWNUM' in item for item in legacy))

        history_sql = ora.adapt_oracle_sql(sql, '11.2.0.4.0')
        history_candidates = ora.oracle_query_candidates(
            history_sql, '11.2.0.4.0', 'registry_sqlpatch')
        self.assertTrue(any('dba_registry_history' in item
                            for item in history_candidates))
        self.assertTrue(any('v$system_patch' in item
                            for item in history_candidates))

    def test_compatibility_error_markers(self):
        self.assertTrue(ora.is_compatibility_error(
            'ORA-00904: "PLUGGABLE_DB": invalid identifier'))
        self.assertTrue(ora.is_compatibility_error(
            'ORA-00942: table or view does not exist'))
        self.assertTrue(ora.is_compatibility_error('ORA-00933'))
        self.assertFalse(ora.is_compatibility_error('ORA-01031: insufficient privileges'))
        self.assertFalse(ora.is_compatibility_error('Connection reset'))


class OracleTemplateSqlTest(unittest.TestCase):
    """直接检查 inspection.db 中的 Oracle 模板，防止静态 SQL 回归。"""

    @classmethod
    def setUpClass(cls):
        if not _DB_PATH.exists():
            raise unittest.SkipTest('inspection.db not found')

    def _queries(self, template_id):
        conn = sqlite3.connect(str(_DB_PATH))
        try:
            return [row[0] for row in conn.execute(
                'SELECT q.query_sql FROM inspection_query q '
                'JOIN inspection_chapter c ON q.chapter_id=c.id '
                'WHERE c.template_id=? AND q.enabled=1', (template_id,))]
        finally:
            conn.close()

    def test_default_template_is_legacy_safe_after_adaptation(self):
        queries = self._queries(5)  # Oracle 默认模板（12c+ 基线）
        self.assertGreaterEqual(len(queries), 50)
        for version in ('10.2.0.5.0', '11.2.0.4.0'):
            for sql in queries:
                adapted = ora.adapt_oracle_sql(sql, version)
                with self.subTest(version=version, sql=sql[:80]):
                    self.assertNotIn('FETCH FIRST', adapted.upper())
                    self.assertNotIn('FETCH NEXT', adapted.upper())
                    self.assertNotIn('DBA_REGISTRY_SQLPATCH', adapted.upper())
                    self.assertNotIn('AUTHENTICATION_TYPE',
                                     adapted.upper().replace(
                                         'AS AUTHENTICATION_TYPE', ''))

    def test_default_template_keeps_12c_features(self):
        queries = self._queries(5)
        fetch_queries = [sql for sql in queries
                         if 'FETCH FIRST' in sql.upper()]
        self.assertTrue(fetch_queries)
        for version in ('12.1.0.2.0', '18.0.0.0.0', '19.0.0.0.0',
                        '21.0.0.0.0', '23.0.0.0.0'):
            for sql in fetch_queries:
                self.assertIn('FETCH FIRST',
                              ora.adapt_oracle_sql(sql, version).upper())

    def test_11g_template_is_10g_safe(self):
        queries = self._queries(6)  # Oracle 11g 模板
        self.assertGreaterEqual(len(queries), 50)
        for sql in queries:
            adapted = ora.adapt_oracle_sql(sql, '10.2.0.5.0')
            with self.subTest(sql=sql[:80]):
                self.assertNotIn('FETCH FIRST', adapted.upper())
                self.assertNotIn('DBA_REGISTRY_SQLPATCH', adapted.upper())
                self.assertNotIn('AUTHENTICATION_TYPE',
                                 adapted.upper().replace(
                                     'AS AUTHENTICATION_TYPE', ''))


if __name__ == '__main__':
    unittest.main()
