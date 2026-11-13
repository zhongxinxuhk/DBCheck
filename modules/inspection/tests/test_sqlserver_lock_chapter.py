# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""SQL Server 兼容模板的锁等待章节播种与老库补齐回归。"""

import tempfile
import unittest
from pathlib import Path

from modules.inspection.dal import (
    ensure_sqlserver_lock_chapters,
    get_db_connection,
    get_template_catalog,
    init_database,
)


class SQLServerLockChapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='icfg_sqlserver_lock_')
        cls.db_path = str(Path(cls._tmp.name) / 'inspection.db')
        init_database(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _template(self, version):
        rows = get_template_catalog(db_path=self.db_path, include_chapter_summary=False)
        return next(t for t in rows
                    if t['db_type'] == 'sqlserver' and t['version'] == version)

    def test_fresh_templates_include_lock_domain(self):
        for version in ('2005', '2008', '2014'):
            template = self._template(version)
            self.assertEqual(template['standard_coverage'], '11/11')
            self.assertEqual(template['missing_domain_keys'], [])
            self.assertEqual(template['chapter_count'], 18)

    def test_removed_lock_chapter_is_restored_once(self):
        template = self._template('2008')
        template_id = template['id']
        conn = get_db_connection(self.db_path)
        try:
            conn.execute(
                "DELETE FROM inspection_query WHERE chapter_id IN "
                "(SELECT id FROM inspection_chapter WHERE template_id=? AND chapter_number=18)",
                (template_id,),
            )
            conn.execute(
                "DELETE FROM inspection_chapter WHERE template_id=? AND chapter_number=18",
                (template_id,),
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(ensure_sqlserver_lock_chapters(self.db_path), 1)
        template = self._template('2008')
        self.assertEqual(template['standard_coverage'], '11/11')
        conn = get_db_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT q.query_sql FROM inspection_query q "
                "JOIN inspection_chapter c ON c.id=q.chapter_id "
                "WHERE c.template_id=? AND q.query_key='sqlserver_lock'",
                (template_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIn('sys.dm_exec_requests', row['query_sql'])
        self.assertIn('blocking_session_id', row['query_sql'])
        self.assertEqual(ensure_sqlserver_lock_chapters(self.db_path), 0)


if __name__ == '__main__':
    unittest.main()
