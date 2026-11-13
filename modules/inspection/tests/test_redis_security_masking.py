# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Redis 安全采集不得把口令或 ACL 哈希写进报告上下文。"""

import importlib.util
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[3] / 'plugins/available/redis/redis_common.py'
_spec = importlib.util.spec_from_file_location('redis_common_test', _MODULE_PATH)
_redis_common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_redis_common)


class _FakeRedis:
    def config_get(self, pattern):
        return {
            'requirepass': 'super-secret-password',
            'protected-mode': 'yes',
            'bind': '127.0.0.1',
            'aclfile': '',
        }

    def execute_command(self, *args):
        return [
            b'user default on #deadbeef ~* +@all',
            b'user readonly on nopass ~* +@read',
        ]


class RedisSecurityMaskingTest(unittest.TestCase):
    def test_password_and_acl_hash_are_never_exposed(self):
        collector = _redis_common.RedisCommonMixin()
        collector.client = _FakeRedis()
        collector.context = {}
        collector._tag = 'test'
        collector._collect_security()
        rows = collector.context['redis_security']
        blob = repr(rows)
        self.assertIn('******', blob)
        self.assertNotIn('super-secret-password', blob)
        self.assertNotIn('deadbeef', blob)
        self.assertTrue(any('ACL 用户 readonly' in row['ITEM'] and '免密' in row['VALUE']
                            for row in rows))


if __name__ == '__main__':
    unittest.main()
