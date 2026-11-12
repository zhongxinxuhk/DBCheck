# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""运行时容量参数的读取、覆盖与钳制规则。"""


import json
import os
import tempfile
import unittest
from pathlib import Path

from modules.core import runtime_tuning as tuning


_ENV_KEYS = [name for name in tuning._ENV.values()]


class RuntimeTuningTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_file = tuning.CONFIG_FILE
        tuning.CONFIG_FILE = Path(self._tmp.name) / 'runtime_tuning.json'
        self._saved_env = {k: os.environ.pop(k, None) for k in _ENV_KEYS}
        tuning.reload()

    def tearDown(self):
        tuning.CONFIG_FILE = self._old_file
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        tuning.reload()
        self._tmp.cleanup()

    def test_defaults(self):
        cfg = tuning.snapshot()
        self.assertEqual(cfg['inspection']['max_concurrent'], 1)
        self.assertEqual(cfg['inspection']['jvm_concurrent'], 1)
        self.assertEqual(cfg['monitor']['max_workers'], 8)
        self.assertEqual(cfg['monitor']['interval'], 10)

    def test_env_overrides_defaults(self):
        os.environ['DBPULSE_MONITOR_WORKERS'] = '16'
        os.environ['DBPULSE_INSPECTION_MAX_CONCURRENT'] = '4'
        tuning.reload()
        self.assertEqual(tuning.get('monitor', 'max_workers'), 16)
        self.assertEqual(tuning.get('inspection', 'max_concurrent'), 4)

    def test_env_values_are_clamped(self):
        os.environ['DBPULSE_MONITOR_WORKERS'] = '999'
        os.environ['DBPULSE_INSPECTION_MAX_CONCURRENT'] = '0'
        os.environ['DBPULSE_MONITOR_INTERVAL'] = '1'
        tuning.reload()
        self.assertEqual(tuning.get('monitor', 'max_workers'), 64)
        self.assertEqual(tuning.get('inspection', 'max_concurrent'), 1)
        self.assertEqual(tuning.get('monitor', 'interval'), 5)

    def test_invalid_env_falls_back_to_default(self):
        os.environ['DBPULSE_MONITOR_WORKERS'] = 'abc'
        tuning.reload()
        self.assertEqual(tuning.get('monitor', 'max_workers'), 8)

    def test_file_config_is_loaded(self):
        tuning.CONFIG_FILE.write_text(json.dumps({
            'inspection': {'max_concurrent': 3, 'watchdog_grace': 120},
            'monitor': {'max_workers': 12, 'interval': 30},
        }), encoding='utf-8')
        tuning.reload()
        self.assertEqual(tuning.get('inspection', 'max_concurrent'), 3)
        self.assertEqual(tuning.get('inspection', 'watchdog_grace'), 120)
        self.assertEqual(tuning.get('monitor', 'max_workers'), 12)
        self.assertEqual(tuning.get('monitor', 'interval'), 30)

    def test_env_wins_over_file(self):
        tuning.CONFIG_FILE.write_text(json.dumps({
            'monitor': {'max_workers': 12},
        }), encoding='utf-8')
        os.environ['DBPULSE_MONITOR_WORKERS'] = '6'
        tuning.reload()
        self.assertEqual(tuning.get('monitor', 'max_workers'), 6)

    def test_broken_file_does_not_break_startup(self):
        tuning.CONFIG_FILE.write_text('{not json', encoding='utf-8')
        tuning.reload()
        cfg = tuning.snapshot()
        self.assertEqual(cfg['monitor']['max_workers'], 8)

    def test_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            tuning.get('monitor', 'no_such_key')

    def test_describe_mentions_effective_values(self):
        text = tuning.describe()
        self.assertIn('巡检并发=1', text)
        self.assertIn('监控并发=8', text)


if __name__ == '__main__':
    unittest.main()
