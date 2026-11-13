# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""MongoDB currentOp/getLog 采集与日志解析回归。"""

import importlib.util
import json
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[3] / 'plugins/available/mongodb/main_plugin.py'
_spec = importlib.util.spec_from_file_location('mongodb_plugin_test', _MODULE_PATH)
_mongo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mongo)


class _FakeDB:
    def command(self, name, *args, **kwargs):
        if name == 'currentOp':
            return {'inprog': [{
                'opid': 7, 'op': 'query', 'ns': 'app.orders', 'secs_running': 3,
                'desc': 'conn7', 'client': '127.0.0.1:5000',
                'command': {'find': 'orders'},
            }]}
        if name == 'getLog' and args and args[0] == 'global':
            return {'log': [json.dumps({
                't': {'$date': '2026-11-13T00:00:00Z'}, 's': 'E',
                'c': 'STORAGE', 'msg': 'test error',
            }), 'plain log line']}
        if name == 'getLog' and args and args[0] == 'startupWarnings':
            return {'log': [json.dumps({
                't': {'$date': '2026-11-13T00:00:01Z'}, 's': 'W',
                'c': 'CONTROL', 'msg': 'startup warning',
            })]}
        raise AssertionError(name)


class MongodbJobCollectorTest(unittest.TestCase):
    def test_current_ops_and_logs_are_normalized(self):
        inspector = object.__new__(_mongo.MongodbInspector)
        inspector.db = _FakeDB()
        inspector.context = {}
        inspector._collect_jobs()

        op = inspector.context['mongodb_current_ops'][0]
        self.assertEqual(op['OP'], 'query')
        self.assertEqual(op['NS'], 'app.orders')
        self.assertEqual(op['SECONDS_RUNNING'], 3)
        self.assertIn('find', op['COMMAND'])

        logs = inspector.context['mongodb_log_tail']
        self.assertEqual(logs[0]['MESSAGE'], 'plain log line')
        self.assertEqual(logs[1]['SEVERITY'], 'E')
        self.assertEqual(logs[1]['COMPONENT'], 'STORAGE')

        warning = inspector.context['mongodb_startup_warnings'][0]
        self.assertEqual(warning['SEVERITY'], 'W')
        self.assertEqual(warning['MESSAGE'], 'startup warning')


if __name__ == '__main__':
    unittest.main()
