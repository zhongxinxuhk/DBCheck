#!/usr/bin/env python3
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""Offline regression checks using disposable Git repositories only."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from sync_upstream import STATE_PATH, eligible, sync


class SyncChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dbpulse-sync-check-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Sync verification')
        self.write('modules/service.py', 'limit = 1\n\n\ndef answer():\n    return 1\n')
        self.write('web_templates/index.html', '<h1>original UI</h1>\n')
        self.base = self.commit()
        self.git('branch', 'upstream')
        self.git('checkout', '-qb', 'fork')
        self.write(STATE_PATH, json.dumps({'last_commit': self.base, 'deferred': {}}) + '\n')
        self.commit()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.PIPE).decode().strip()

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self):
        # Disposable fixtures contain no user files.
        self.git('add', '--all')
        self.git('commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def upstream(self, files):
        self.git('checkout', '-q', 'upstream')
        for path, content in files.items():
            if content is None:
                (self.root / path).unlink()
            else:
                self.write(path, content)
        head = self.commit()
        self.git('checkout', '-q', 'fork')
        return head

    def test_frontend_branding_and_workflows_are_protected(self):
        self.upstream({'web_templates/index.html': '<h1>new upstream UI</h1>\n',
                       'assets/web/app.js': 'upstream()', 'i18n/zh.py': 'title = "Other"\n',
                       'modules/core/brand.py': 'PRODUCT_NAME_EN = "Other"\n',
                       '.github/workflows/sync-upstream.yml': 'untrusted: workflow\n',
                       'templates/new.docx': 'template'})
        result = sync(self.root, 'upstream', True)
        self.assertEqual(len(result['protected']), 6)
        self.assertEqual((self.root / 'web_templates/index.html').read_text(), '<h1>original UI</h1>\n')
        self.assertFalse((self.root / 'modules/core/brand.py').exists())
        self.assertFalse(eligible('../modules/escape.py'))
        self.assertFalse(eligible('/modules/escape.py'))
        self.assertFalse(eligible('modules/web/static/test.py'))

    def test_nonoverlapping_local_changes_survive_and_repeat_is_noop(self):
        self.write('modules/service.py', 'limit = 99\n\n\ndef answer():\n    return 1\n')
        self.commit()
        self.upstream({'modules/service.py': 'limit = 1\n\n\ndef answer():\n    return 2\n',
                       'modules/new.py': 'enabled = True\n'})
        preview = sync(self.root, 'upstream')
        self.assertEqual(len(preview['applied']), 2)
        self.assertFalse(self.git('status', '--porcelain'))
        result = sync(self.root, 'upstream', True)
        self.assertFalse(result['deferred'])
        self.assertIn('limit = 99', (self.root / 'modules/service.py').read_text())
        self.assertIn('return 2', (self.root / 'modules/service.py').read_text())
        self.commit()
        self.assertEqual(sync(self.root, 'upstream', True)['applied'], [])
        self.assertFalse(self.git('status', '--porcelain'))

    def test_conflict_is_preserved_and_retried_after_checkpoint_advances(self):
        local = 'limit = 9\n\n\ndef answer():\n    return 1\n'
        self.write('modules/service.py', local)
        self.commit()
        self.upstream({'modules/service.py': 'limit = 8\n\n\ndef answer():\n    return 1\n',
                       'modules/new.py': 'feature = 1\n'})
        result = sync(self.root, 'upstream', True)
        self.assertIn('modules/service.py', result['deferred'])
        self.assertEqual((self.root / 'modules/service.py').read_text(), local)
        state = json.loads((self.root / STATE_PATH).read_text())
        self.assertEqual(state['deferred']['modules/service.py'], self.base)
        self.commit()
        self.assertIn('modules/service.py', sync(self.root, 'upstream')['deferred'])
        self.write('modules/service.py', 'limit = 8\n\n\ndef answer():\n    return 1\n')
        self.commit()
        self.assertFalse(sync(self.root, 'upstream', True)['deferred'])

    def test_deletions_and_add_add_conflicts(self):
        self.write('modules/service.py', 'custom = True\n')
        self.write('modules/new.py', 'custom = True\n')
        self.commit()
        self.upstream({'modules/service.py': None, 'modules/new.py': 'upstream = True\n'})
        result = sync(self.root, 'upstream', True)
        self.assertEqual(len(result['deferred']), 2)
        self.assertEqual((self.root / 'modules/service.py').read_text(), 'custom = True\n')

    def test_unmodified_upstream_deletion(self):
        self.upstream({'modules/service.py': None})
        result = sync(self.root, 'upstream', True)
        self.assertEqual(result['applied'], ['modules/service.py'])
        self.assertFalse((self.root / 'modules/service.py').exists())

    def test_mixed_presentation_invalid_python_and_symlinks_are_deferred(self):
        self.git('checkout', '-q', 'upstream')
        self.write('modules/service.py', 'template = "<h1>new layout</h1>"\n')
        self.write('modules/broken.py', 'def broken(:\n')
        (self.root / 'modules/link.py').symlink_to('/tmp/never-follow-this')
        self.commit()
        self.git('checkout', '-q', 'fork')
        result = sync(self.root, 'upstream', True)
        self.assertEqual(len(result['deferred']), 3)
        self.assertFalse((self.root / 'modules/link.py').exists())
        self.assertFalse((self.root / 'modules/broken.py').exists())

    def test_dirty_checkout_and_rewritten_upstream_fail_before_writing(self):
        self.write('modules/service.py', 'uncommitted = True\n')
        with self.assertRaises(ValueError):
            sync(self.root, 'upstream', True)
        self.git('restore', 'modules/service.py')
        self.git('checkout', '--orphan', 'rewritten')
        self.git('rm', '-rf', '.')
        self.write('modules/new.py', 'rewritten = True\n')
        self.commit()
        self.git('checkout', '-q', 'fork')
        before = (self.root / STATE_PATH).read_bytes()
        with self.assertRaises(subprocess.CalledProcessError):
            sync(self.root, 'rewritten', True)
        self.assertEqual((self.root / STATE_PATH).read_bytes(), before)
        self.assertFalse(self.git('status', '--porcelain'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
