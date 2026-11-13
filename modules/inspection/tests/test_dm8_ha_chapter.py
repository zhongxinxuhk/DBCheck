# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""DM8 单机模板的数据守护/HA 章节播种与老库补齐回归。"""

import tempfile
import unittest
from pathlib import Path

from modules.inspection.dal import (
    ensure_dm8_ha_chapters,
    get_db_connection,
    get_template_catalog,
    init_database,
)


class DM8HAChapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='icfg_dm8_ha_')
        cls.db_path = str(Path(cls._tmp.name) / 'inspection.db')
        init_database(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _template(self):
        rows = get_template_catalog(db_path=self.db_path, include_chapter_summary=False)
        return next(t for t in rows if t['db_type'] == 'dm8' and t['version'] == 'v1')

    def test_fresh_template_includes_ha_domain(self):
        template = self._template()
        self.assertEqual(template['standard_coverage'], '11/11')
        self.assertEqual(template['missing_domain_keys'], [])
        self.assertEqual(template['chapter_count'], 19)

    def test_removed_ha_chapter_is_restored_once(self):
        template = self._template()
        template_id = template['id']
        conn = get_db_connection(self.db_path)
        try:
            conn.execute(
                "DELETE FROM inspection_query WHERE chapter_id IN "
                "(SELECT id FROM inspection_chapter WHERE template_id=? AND chapter_number=19)",
                (template_id,),
            )
            conn.execute(
                "DELETE FROM inspection_chapter WHERE template_id=? AND chapter_number=19",
                (template_id,),
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(ensure_dm8_ha_chapters(self.db_path), 1)
        self.assertEqual(self._template()['standard_coverage'], '11/11')
        conn = get_db_connection(self.db_path)
        try:
            rows = conn.execute(
                "SELECT q.query_key, q.query_sql FROM inspection_query q "
                "JOIN inspection_chapter c ON c.id=q.chapter_id "
                "WHERE c.template_id=? AND c.chapter_number=19 ORDER BY q.query_key",
                (template_id,),
            ).fetchall()
        finally:
            conn.close()
        keys = {row['query_key'] for row in rows}
        self.assertEqual(keys, {'dm_arch_status', 'dm_mal_info', 'dm_mal_link', 'dm_watcher'})
        self.assertIn('V$DMWATCHER', next(r['query_sql'] for r in rows if r['query_key'] == 'dm_watcher'))
        self.assertEqual(ensure_dm8_ha_chapters(self.db_path), 0)


if __name__ == '__main__':
    unittest.main()
