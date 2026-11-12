# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""日志生命周期：轮转、保留、总预算与紧急清理的行为约束。"""


import json
import logging
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from modules.core import log_manager as lm


MB = 1024 * 1024


class LogManagerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._saved = (lm.LOG_DIR, lm.ARCHIVE_ROOT, lm.MONITOR_LOG, lm.POLICY_FILE)
        lm.LOG_DIR = self.root
        lm.ARCHIVE_ROOT = self.root / '_archive'
        lm.MONITOR_LOG = self.root / 'monitor.log'
        lm.POLICY_FILE = self.root / 'log_policy.json'
        self._limits = dict(lm.LIMITS)
        lm.reload_policy()

    def tearDown(self):
        lm.LOG_DIR, lm.ARCHIVE_ROOT, lm.MONITOR_LOG, lm.POLICY_FILE = self._saved
        lm.LIMITS.clear()
        lm.LIMITS.update(self._limits)
        lm.reload_policy()
        self._tmp.cleanup()

    def _write_policy(self, data):
        lm.POLICY_FILE.write_text(json.dumps(data), encoding='utf-8')
        return lm.reload_policy()

    def _make_backup(self, family, name, size_kb, age_days=0):
        d = lm.ARCHIVE_ROOT / family
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_bytes(b'x' * (size_kb * 1024))
        if age_days:
            ts = (datetime.now() - timedelta(days=age_days)).timestamp()
            os.utime(f, (ts, ts))
        return f

    def test_family_mapping(self):
        self.assertEqual(lm._family_of(self.root / 'scheduler.log'), 'scheduler')
        self.assertEqual(lm._family_of(self.root / 'inspection-20261110.log'), 'inspection')
        self.assertEqual(lm._family_of(self.root / 'service.out.log'), 'service')
        self.assertEqual(lm._family_of(self.root / 'unknown.txt'), 'misc')

    def test_rotate_respects_threshold(self):
        f = self.root / 'scheduler.log'
        f.write_bytes(b'a' * 2048)
        self.assertIsNone(lm.rotate_file(f, 'scheduler'))
        self.assertTrue(f.exists())

    def test_rotate_moves_and_compresses(self):
        f = self.root / 'scheduler.log'
        f.write_bytes(b'a' * 4096)
        dest = lm.rotate_file(f, 'scheduler', force=True)
        self.assertIsNotNone(dest)
        self.assertFalse(f.exists())
        self.assertEqual(dest.suffix, '.gz')
        self.assertTrue(dest.is_file())

    def test_keep_count_prunes_old_backups(self):
        self._write_policy({'families': {'scheduler': {'keep': 2, 'keep_days': 30}}})
        for i in range(4):
            self._make_backup('scheduler', 'scheduler.2026010%d-000000.log' % i, 1)
        lm._prune_family('scheduler')
        left = list((lm.ARCHIVE_ROOT / 'scheduler').iterdir())
        self.assertEqual(len(left), 2)

    def test_keep_days_prunes_expired_backups(self):
        self._write_policy({'families': {'scheduler': {'keep': 10, 'keep_days': 3}}})
        old = self._make_backup('scheduler', 'scheduler.old.log', 1, age_days=10)
        fresh = self._make_backup('scheduler', 'scheduler.fresh.log', 1)
        lm._prune_family('scheduler')
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_sweep_deletes_expired_daily_file(self):
        old = self.root / 'inspection-20200101.log'
        old.write_text('old\n', encoding='utf-8')
        today = self.root / ('inspection-%s.log' % datetime.now().strftime('%Y%m%d'))
        today.write_text('today\n', encoding='utf-8')
        stats = lm.sweep()
        self.assertFalse(old.exists())
        self.assertTrue(today.exists())
        self.assertGreaterEqual(stats['removed'], 1)

    def test_total_budget_removes_oldest_archives(self):
        lm.LIMITS['total_budget_mb'] = (1, 102400)
        self._write_policy({'total_budget_mb': 1, 'families': {}})
        oldest = self._make_backup('scheduler', 'scheduler.a.log', 700, age_days=2)
        newer = self._make_backup('scheduler', 'scheduler.b.log', 700, age_days=1)
        stats = lm.sweep()
        self.assertFalse(oldest.exists())
        self.assertTrue(newer.exists())
        self.assertLessEqual(lm._managed_size(), MB)

    def test_emergency_cleanup_on_low_disk(self):
        self._write_policy({'min_free_mb': 100})
        a = self._make_backup('scheduler', 'scheduler.a.log', 10, age_days=2)
        b = self._make_backup('scheduler', 'scheduler.b.log', 10, age_days=1)
        stats = lm.sweep(free_mb=0)
        self.assertTrue(stats['emergency'])
        self.assertFalse(a.exists() or b.exists())

    def test_append_line_rotates_when_oversized(self):
        self._write_policy({'families': {'inspection': {'max_mb': 1, 'keep': 3}}})
        f = self.root / 'inspection-20261110.log'
        lm.append_line(f, 'a' * (int(MB * 1.1)), family='inspection')
        self.assertTrue(f.exists())
        lm.append_line(f, 'after-rotate', family='inspection')
        backups = list((lm.ARCHIVE_ROOT / 'inspection').glob('*'))
        self.assertEqual(len(backups), 1)
        self.assertLess(f.stat().st_size, MB)

    def test_rotating_handler_rolls_over(self):
        self._write_policy({'families': {'scheduler': {'max_mb': 1, 'keep': 5}}})
        h = lm.RotatingLogHandler(self.root / 'scheduler.log', family='scheduler')
        try:
            rec = logging.LogRecord('scheduler', logging.INFO, __file__, 1, 'x' * 100000, None, None)
            for _ in range(12):
                h.emit(rec)
        finally:
            h.close()
        backups = list((lm.ARCHIVE_ROOT / 'scheduler').glob('*'))
        self.assertGreaterEqual(len(backups), 1)
        self.assertLess((self.root / 'scheduler.log').stat().st_size, MB)

    def test_status_reports_usage(self):
        (self.root / 'scheduler.log').write_text('x' * 1024, encoding='utf-8')
        st = lm.status()
        self.assertIn('scheduler', st['families'])
        self.assertGreaterEqual(st['total_bytes'], 1024)
        self.assertIn('budget_bytes', st)
        self.assertIn('disk_free_mb', st)


if __name__ == '__main__':
    unittest.main()
