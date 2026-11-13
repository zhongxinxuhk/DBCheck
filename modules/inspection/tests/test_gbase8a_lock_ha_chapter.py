# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""GBase 8a MPP 默认模板的锁/高可用章节与业务模板隔离回归。"""

import tempfile
import unittest
from pathlib import Path

from modules.inspection.dal import (
    ensure_gbase8a_lock_ha_chapters,
    get_db_connection,
    get_template_catalog,
    init_database,
)


class GBase8ALockHAChapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='icfg_gbase8a_ha_')
        cls.db_path = str(Path(cls._tmp.name) / 'inspection.db')
        init_database(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _template(self):
        rows = get_template_catalog(db_path=self.db_path, include_chapter_summary=False)
        return next(t for t in rows
                    if t['db_type'] == 'gbase8a' and t['template_name'] == 'GBase 8a MPP集群巡检模板')

    def test_default_template_has_lock_and_ha(self):
        template = self._template()
        self.assertEqual(template['standard_coverage'], '11/11')
        self.assertEqual(template['missing_domain_keys'], [])
        self.assertEqual(template['chapter_count'], 16)

    def test_business_templates_are_not_modified(self):
        template = self._template()
        conn = get_db_connection(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM inspection_query q "
                "JOIN inspection_chapter c ON c.id=q.chapter_id "
                "JOIN inspection_template t ON t.id=c.template_id "
                "WHERE t.db_type='gbase8a' AND t.id != ? "
                "AND q.query_key IN ('gbase8a_lock','gbase8a_ha')",
                (template['id'],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_removed_chapters_are_restored_once(self):
        template = self._template()
        template_id = template['id']
        conn = get_db_connection(self.db_path)
        try:
            conn.execute(
                "DELETE FROM inspection_query WHERE chapter_id IN "
                "(SELECT id FROM inspection_chapter WHERE template_id=? "
                "AND chapter_title_zh IN ('锁与阻塞','高可用与集群状态'))",
                (template_id,),
            )
            conn.execute(
                "DELETE FROM inspection_chapter WHERE template_id=? "
                "AND chapter_title_zh IN ('锁与阻塞','高可用与集群状态')",
                (template_id,),
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(ensure_gbase8a_lock_ha_chapters(self.db_path), 2)
        template = self._template()
        self.assertEqual(template['standard_coverage'], '11/11')
        self.assertEqual(template['chapter_count'], 16)
        self.assertEqual(ensure_gbase8a_lock_ha_chapters(self.db_path), 0)


if __name__ == '__main__':
    unittest.main()
