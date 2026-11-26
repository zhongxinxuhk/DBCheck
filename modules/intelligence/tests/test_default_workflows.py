# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Default workflow installation, upgrade, retry and deletion regressions."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.intelligence import workflow_store as store
from modules.intelligence.market_templates import builtin_templates


class DefaultWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'nested' / 'workflows.db'
        for attribute, value in [('_DB_PATH', str(self.path)), ('_MIGRATED', False)]:
            replacement = patch.object(store, attribute, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_new_database_installs_all_eight_without_duplicates(self):
        catalog = builtin_templates()
        rows = store.list_workflows()
        self.assertEqual(len(rows), 8)
        self.assertEqual({row['name'] for row in rows}, {row['name'] for row in catalog})
        for template in catalog:
            row = next(row for row in rows if row['name'] == template['name'])
            self.assertEqual(row['steps'], template['steps'])
            self.assertEqual(row['edges'], template['edges'])
            self.assertEqual(row['description'], template['description'])
            self.assertEqual(row['source'], 'builtin')
        store._MIGRATED = False
        self.assertEqual(store.list_workflows(), rows)
        self.assertEqual(len(store.market_list()), 8)

    def test_reimport_reuses_identical_workflow_and_keeps_different_graph(self):
        template = builtin_templates()[0]
        first = store.import_workflow(template)["workflow"]
        custom = {**template, "steps": [*template["steps"], {"id": "extra", "kind": "end"}]}
        second = store.import_workflow(custom)["workflow"]

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(store.import_workflow(custom)["workflow"]["id"], second["id"])
        self.assertEqual(store.import_workflow(template)["workflow"]["id"], first["id"])
        self.assertEqual(len(store.list_workflows()), 9)

    def test_legacy_database_preserves_existing_ids_and_edits(self):
        self.path.parent.mkdir(parents=True)
        name = builtin_templates()[3]['name']
        steps = [{'id': 'custom', 'kind': 'start', 'x': 71}]
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE workflows (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,'
                       'steps TEXT,edges TEXT,created_at TEXT,updated_at TEXT)')
            db.execute('INSERT INTO workflows VALUES(41,?,?,?,?,?)',
                       (name, json.dumps(steps), '[]', 'original', 'original'))
        rows = store.list_workflows()
        self.assertEqual(len(rows), 8)
        existing = store.get_workflow(41)
        self.assertEqual(existing['name'], name)
        self.assertEqual(existing['steps'], steps)
        self.assertEqual(existing['updated_at'], 'original')
        self.assertEqual(existing['source'], 'user')

    def test_deleted_or_renamed_default_is_not_recreated_on_restart(self):
        rows = store.list_workflows()
        removed, renamed = rows[:2]
        self.assertTrue(store.delete_workflow(removed['id']))
        store.save_workflow('renamed by user', renamed['steps'], renamed['edges'], wf_id=renamed['id'])
        store._MIGRATED = False
        rows = store.list_workflows()
        self.assertEqual(len(rows), 7)
        self.assertNotIn(removed['name'], {row['name'] for row in rows})
        self.assertNotIn(renamed['name'], {row['name'] for row in rows})
        self.assertIn('renamed by user', {row['name'] for row in rows})

    def test_failed_initialization_rolls_back_and_can_retry(self):
        original = store._seed_default_workflows
        def fail_after_seed(connection):
            original(connection)
            raise RuntimeError('fixture failure')
        with patch.object(store, '_seed_default_workflows', side_effect=fail_after_seed):
            with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
                store.list_workflows()
        self.assertFalse(store._MIGRATED)
        self.assertEqual(len(store.list_workflows()), 8)

    def test_multiple_processes_initialize_one_catalog(self):
        program = ('import sys; from modules.intelligence import workflow_store as s; '
                   's._DB_PATH=sys.argv[1]; assert len(s.list_workflows())==8')
        workers = [subprocess.Popen([sys.executable, '-c', program, str(self.path)],
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
        for worker in workers:
            output, error = worker.communicate(timeout=30)
            self.assertEqual(worker.returncode, 0, error.decode())
        self.assertEqual(len(store.list_workflows()), 8)

    def test_service_defaults_have_portable_acyclic_graphs(self):
        for template in builtin_templates():
            steps = template['steps']
            ids = {step['id'] for step in steps}
            self.assertEqual(len(ids), len(steps))
            incoming = {key: set() for key in ids}
            for source, target in template['edges']:
                self.assertIn(source, ids)
                self.assertIn(target, ids)
                incoming[target].add(source)
            visited = set()
            while incoming:
                ready = {key for key, parents in incoming.items() if parents <= visited}
                self.assertTrue(ready, template['name'])
                visited.update(ready)
                incoming = {key: parents for key, parents in incoming.items() if key not in ready}
            for step in steps:
                self.assertNotIn('instance_id', step.get('args', {}))
                self.assertFalse(step.get('args', {}).get('to'))

    def test_legacy_titles_resolve_to_current_chapters(self):
        from modules.intelligence import inspect_components as components
        titles = ['SQL 与计划', '逻辑存储空间', '数据文件与容量', '锁与阻塞', '会话与连接']
        catalog = [{'id': title, 'title': title, 'is_default': 0} for title in titles]
        with patch.object(components, 'list_components', return_value=catalog):
            for keyword, expected in zip(['慢查询', '表空间', '磁盘', '锁', '会话'], titles):
                self.assertEqual(components.resolve_component('gbase8a', keyword)['title'], expected)
            self.assertIsNone(components.resolve_component('gbase8a', 'unknown chapter'))

    def test_exact_custom_title_keeps_priority_over_alias(self):
        from modules.intelligence import inspect_components as components
        catalog = [{'id': 'alias', 'title': '锁与阻塞', 'is_default': 1},
                   {'id': 'exact', 'title': '锁', 'is_default': 0}]
        with patch.object(components, 'list_components', return_value=catalog):
            self.assertEqual(components.resolve_component('oracle', '锁')['id'], 'exact')


if __name__ == '__main__':
    unittest.main()
